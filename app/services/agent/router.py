from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
import ast
import json
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

from app.constants.enums import AgentIntent, ExecutionMode, SSEEventType
from app.core.config import Settings
from app.core.exceptions import ModelInvocationError, SandboxExecutionError, serialize_exception
from app.services.agent.deepseek_client import DeepSeekClient
from app.services.agent.prompts import (
    CHAT_SYSTEM_PROMPT,
    CHAT_JSON_OUTPUT_PROMPT,
    compose_system_prompt,
    EXCEL_CODE_SYSTEM_PROMPT,
    EXCEL_GUIDANCE_JSON_OUTPUT_PROMPT,
    SEARCH_SYSTEM_PROMPT,
    SEARCH_JSON_OUTPUT_PROMPT,
    SEARCH_PLANNER_SYSTEM_PROMPT,
    SEARCH_PLANNER_JSON_PROMPT,
    SESSION_TITLE_SYSTEM_PROMPT,
    SESSION_TITLE_JSON_PROMPT,
    build_chat_user_prompt,
    build_excel_codegen_user_prompt,
    build_excel_repair_user_prompt,
    build_excel_guidance_user_prompt,
    build_search_plan_prompt,
    build_search_user_prompt,
    build_session_title_prompt,
)
from app.services.files.file_manager import FileManager
from app.services.sandbox.e2b_client import E2BClient
from app.services.sandbox.local_executor import LocalExecutor
from app.services.tools.excel_parser import ExcelParser
from app.services.tools.web_search import WebSearchService


class AgentRouter:
    def __init__(self, settings: Settings, file_manager: FileManager) -> None:
        self.settings = settings
        self.file_manager = file_manager
        self.llm_client = DeepSeekClient(settings)
        self.search_service = WebSearchService(settings)
        self.excel_parser = ExcelParser()
        self.local_executor = LocalExecutor()
        self.e2b_client = E2BClient(settings)

    def detect_intent(
        self,
        *,
        message: str,
        allow_search: bool,
        file_id: str | None,
    ) -> AgentIntent:
        lower_message = message.lower()
        if not file_id and self._is_local_time_question(lower_message):
            return AgentIntent.CHAT
        if file_id or any(
            keyword in lower_message
            for keyword in ("excel", "表格", "sheet", "订单", "图片链接")
        ):
            return AgentIntent.EXCEL
        if allow_search:
            return AgentIntent.SEARCH
        return AgentIntent.CHAT

    async def plan_and_search(
        self,
        message: str,
        *,
        current_time: datetime | None = None,
    ) -> dict[str, Any]:
        now = current_time or self._system_now()
        search_plan = await self._plan_search(message, now=now)
        return await self.search_service.search(message, search_plan=search_plan)

    async def _plan_search(
        self,
        message: str,
        *,
        now: datetime,
    ) -> dict[str, Any]:
        try:
            completion = await self.llm_client.complete_json(
                system_prompt=compose_system_prompt(
                    SEARCH_PLANNER_SYSTEM_PROMPT,
                    SEARCH_PLANNER_JSON_PROMPT,
                    now=now,
                ),
                user_prompt=build_search_plan_prompt(message, now=now),
                temperature=0.0,
            )
            return self._normalize_search_plan(completion["content"], message)
        except (ModelInvocationError, TypeError, ValueError):
            # 查询规划失败不应阻断联网回答，退回原始问题并取消强制时间过滤。
            return {
                "queries": [" ".join(message.strip().split())],
                "topic": "general",
                "time_range": None,
                "reason": "搜索规划失败，使用原始问题兜底",
            }

    async def generate_session_title(
        self,
        *,
        user_message: str,
        assistant_answer: str,
    ) -> str:
        try:
            completion = await self.llm_client.complete_json(
                system_prompt=compose_system_prompt(
                    SESSION_TITLE_SYSTEM_PROMPT,
                    SESSION_TITLE_JSON_PROMPT,
                ),
                user_prompt=build_session_title_prompt(
                    user_message,
                    assistant_answer,
                ),
                temperature=0.1,
            )
            title = self._normalize_session_title(
                completion["content"].get("title", "")
            )
            return title
        except (ModelInvocationError, TypeError, ValueError):
            # 标题生成属于附加能力，失败时不能影响主回答；保留空标题供下次重试。
            return ""

    async def complete_chat(
        self,
        *,
        message: str,
        allow_search: bool,
        file_id: str | None = None,
        template_name: str | None = None,
        execution_mode: ExecutionMode | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        # 非流式接口统一走这里，内部按 chat / search / excel 三种意图分流。
        current_time = self._system_now()
        intent = self.detect_intent(
            message=message, allow_search=allow_search, file_id=file_id
        )
        if not file_id and self._is_local_time_question(message):
            return self._build_local_time_result(
                message=message,
                current_time=current_time,
                template_name=template_name,
            )

        if intent == AgentIntent.SEARCH:
            search_payload = await self.plan_and_search(
                message,
                current_time=current_time,
            )
            results = search_payload["results"]
            completion = await self.llm_client.complete_json(
                system_prompt=compose_system_prompt(
                    SEARCH_SYSTEM_PROMPT,
                    SEARCH_JSON_OUTPUT_PROMPT,
                    now=current_time,
                ),
                user_prompt=build_search_user_prompt(
                    message,
                    results,
                    search_payload["search_context"],
                    template_name=template_name,
                ),
            )
            parsed = self._normalize_search_json(completion["content"])
            storage = self._build_openai_storage_payload(
                intent=intent,
                current_time=current_time,
                request_payload=completion["request_payload"],
                response_payload=completion["raw_response"],
                template_name=template_name,
                citations=results,
                tool_context={
                    "search": {
                        "provider": search_payload["provider"],
                        "query": search_payload["query"],
                        "queries": search_payload.get("queries", []),
                        "search_plan": search_payload.get("search_plan", {}),
                        "search_context": search_payload["search_context"],
                        "request_payloads": search_payload.get("request_payloads", []),
                        "response_times": search_payload.get("response_times", []),
                        "search_errors": search_payload.get("search_errors", []),
                    }
                },
                structured_output=parsed,
            )
            return {
                "content": parsed["answer"],
                "citations": results,
                "metadata": {
                    "intent": intent,
                    "template_name": template_name,
                    "model": completion["model"],
                    "usage": completion["usage"],
                    "current_time": current_time.isoformat(),
                    "search": {
                        "provider": search_payload["provider"],
                        "query": search_payload["query"],
                        "queries": search_payload.get("queries", []),
                        "search_plan": search_payload.get("search_plan", {}),
                        "search_context": search_payload["search_context"],
                        "request_payloads": search_payload.get("request_payloads", []),
                        "response_times": search_payload.get("response_times", []),
                        "search_errors": search_payload.get("search_errors", []),
                    },
                    "structured_output": parsed,
                },
                "storage": storage,
            }

        if intent == AgentIntent.EXCEL and file_id:
            selected_mode = execution_mode or self._default_excel_execution_mode()
            final_result: dict[str, Any] | None = None
            async for event_name, event_payload in self.process_excel(
                file_id=file_id, instruction=message, execution_mode=selected_mode, task_id=task_id
            ):
                if event_name == SSEEventType.RESULT and isinstance(event_payload, dict) and event_payload.get("kind") == "execution_result":
                    final_result = event_payload
            if final_result is None:
                raise SandboxExecutionError("Excel 任务未返回执行结果")
            answer = self._build_excel_answer(final_result)
            storage = {
                "protocol": "excel_agent_execution", "intent": str(intent),
                "context": {"current_time": current_time.isoformat(), "template_name": template_name, "input_file_id": file_id, "execution_mode": str(selected_mode)},
                "excel": final_result,
            }
            return {
                "content": answer, "citations": [],
                "metadata": {"intent": intent, "template_name": template_name, "current_time": current_time.isoformat(), "execution_mode": selected_mode, "outputs": final_result["outputs"], "primary_output": final_result["primary_output"], "execution": final_result["execution"], "plan": final_result["plan"], "preview": final_result["preview"], "output_preview": final_result.get("output_preview")},
                "storage": storage, "output_file_id": final_result["primary_output"]["file_id"], "outputs": final_result["outputs"],
            }

        completion = await self.llm_client.complete_json(
            system_prompt=compose_system_prompt(
                CHAT_SYSTEM_PROMPT,
                CHAT_JSON_OUTPUT_PROMPT,
                now=current_time,
            ),
            user_prompt=build_chat_user_prompt(message, template_name=template_name),
        )
        parsed = self._normalize_chat_json(completion["content"])
        storage = self._build_openai_storage_payload(
            intent=intent,
            current_time=current_time,
            request_payload=completion["request_payload"],
            response_payload=completion["raw_response"],
            template_name=template_name,
            structured_output=parsed,
        )
        return {
            "content": parsed["answer"],
            "citations": [],
            "metadata": {
                "intent": intent,
                "template_name": template_name,
                "model": completion["model"],
                "usage": completion["usage"],
                "current_time": current_time.isoformat(),
                "structured_output": parsed,
            },
            "storage": storage,
        }

    async def stream_chat(
        self,
        *,
        message: str,
        allow_search: bool,
        file_id: str | None = None,
        template_name: str | None = None,
        execution_mode: ExecutionMode | None = None,
        task_id: str | None = None,
    ) -> AsyncIterator[tuple[SSEEventType, dict[str, Any]]]:
        # 流式接口优先保证用户看到实时内容，Excel 场景则先走结构化建议。
        current_time = self._system_now()
        intent = self.detect_intent(
            message=message,
            allow_search=allow_search,
            file_id=file_id,
        )
        yield SSEEventType.STATUS, {"message": "正在分析用户意图"}

        if not file_id and self._is_local_time_question(message):
            result = self._build_local_time_result(
                message=message,
                current_time=current_time,
                template_name=template_name,
            )
            yield SSEEventType.MESSAGE, {
                "content": result["content"],
                "citations": result["citations"],
                "metadata": result["metadata"],
            }
            yield SSEEventType.DONE, {
                "success": True,
                "content": result["content"],
                "storage": result["storage"],
            }
            return

        if intent == AgentIntent.EXCEL and file_id:
            selected_mode = execution_mode or self._default_excel_execution_mode()
            final_result: dict[str, Any] | None = None
            async for event_name, event_payload in self.process_excel(
                file_id=file_id, instruction=message, execution_mode=selected_mode, task_id=task_id
            ):
                if event_name == SSEEventType.RESULT and isinstance(event_payload, dict) and event_payload.get("kind") == "execution_result":
                    final_result = event_payload
                yield event_name, event_payload
            if final_result is None:
                raise SandboxExecutionError("Excel 任务未返回执行结果")
            answer = self._build_excel_answer(final_result)
            storage = {"protocol": "excel_agent_execution", "intent": str(intent), "context": {"current_time": current_time.isoformat(), "template_name": template_name, "input_file_id": file_id, "execution_mode": str(selected_mode)}, "excel": final_result}
            yield SSEEventType.MESSAGE, {"content": answer, "citations": [], "metadata": {"intent": intent, "outputs": final_result["outputs"], "primary_output": final_result["primary_output"], "execution": final_result["execution"]}}
            yield SSEEventType.DONE, {"success": True, "content": answer, "storage": storage, "output_file_id": final_result["primary_output"]["file_id"], "outputs": final_result["outputs"]}
            return

        citations: list[dict[str, Any]] = []
        metadata: dict[str, Any] = {
            "intent": intent,
            "template_name": template_name,
            "model": self.settings.default_model,
            "current_time": current_time.isoformat(),
        }
        system_prompt = compose_system_prompt(
            CHAT_SYSTEM_PROMPT,
            "请直接回答用户问题。",
            now=current_time,
        )
        user_prompt = build_chat_user_prompt(message, template_name=template_name)
        tool_context: dict[str, Any] = {}

        if intent == AgentIntent.SEARCH:
            yield SSEEventType.STATUS, {"message": "正在执行联网搜索"}
            search_payload = await self.plan_and_search(
                message,
                current_time=current_time,
            )
            citations = search_payload["results"]
            yield SSEEventType.TOOL_CALL, {
                "tool": "web_search",
                "results_count": len(citations),
                "search_context": search_payload["search_context"],
                "request_payloads": search_payload.get("request_payloads", []),
            }
            metadata["search"] = {
                "provider": search_payload["provider"],
                "query": search_payload["query"],
                "queries": search_payload.get("queries", []),
                "search_plan": search_payload.get("search_plan", {}),
                "search_context": search_payload["search_context"],
                "request_payloads": search_payload.get("request_payloads", []),
                "response_times": search_payload.get("response_times", []),
                "search_errors": search_payload.get("search_errors", []),
            }
            tool_context = {"search": metadata["search"]}
            system_prompt = compose_system_prompt(
                SEARCH_SYSTEM_PROMPT,
                "请直接基于给定搜索结果回答用户，不要输出 JSON。",
                now=current_time,
            )
            user_prompt = build_search_user_prompt(
                message,
                citations,
                search_payload["search_context"],
                template_name=template_name,
            )

        yield SSEEventType.STATUS, {"message": "正在调用 DeepSeek 生成回答"}
        chunks: list[str] = []
        request_payload = {
            "model": self.settings.default_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.3,
            "stream": True,
        }
        async for chunk in self.llm_client.stream_text(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        ):
            chunks.append(chunk)
            yield SSEEventType.MESSAGE, {
                "delta": chunk,
                "content": "".join(chunks),
                "citations": citations,
                "metadata": metadata,
            }
        final_content = "".join(chunks)
        storage = self._build_openai_storage_payload(
            intent=intent,
            current_time=current_time,
            request_payload=request_payload,
            response_payload=self._build_synthetic_openai_response(
                model=self.settings.default_model,
                content=final_content,
                current_time=current_time,
            ),
            template_name=template_name,
            citations=citations,
            tool_context=tool_context,
        )
        yield SSEEventType.DONE, {
            "success": True,
            "content": final_content,
            "storage": storage,
        }

    async def process_excel(
        self,
        *,
        file_id: str,
        instruction: str,
        execution_mode: ExecutionMode,
        task_id: str | None = None,
    ) -> AsyncIterator[tuple[SSEEventType, dict[str, Any]]]:
        file_path = await self.file_manager.resolve_path_async(file_id)
        yield SSEEventType.STATUS, {"message": "正在解析 Excel 工作簿结构"}
        preview = self.excel_parser.parse_preview(file_path)
        yield SSEEventType.RESULT, {"kind": "input_preview", "preview": preview}

        yield SSEEventType.STATUS, {"message": "正在生成 Excel 处理方案与可执行代码"}
        completion = await self.llm_client.complete_json(
            system_prompt=compose_system_prompt(EXCEL_CODE_SYSTEM_PROMPT, now=self._system_now()),
            user_prompt=build_excel_codegen_user_prompt(instruction, preview, file_path.suffix.lower()),
            temperature=0.0,
        )
        plan = self._normalize_excel_plan_json(completion["content"])
        generated_code = self._sanitize_python_code(plan["python_code"])
        yield SSEEventType.RESULT, {
            "kind": "execution_plan", "task_analysis": plan["task_analysis"],
            "execution_steps": plan["execution_steps"], "expected_outputs": plan["expected_outputs"],
            "metadata": {"model": completion["model"], "usage": completion["usage"]},
        }
        yield SSEEventType.CODE, {"python_code": generated_code}

        run_id = self._safe_run_id(task_id or f"run-{uuid4().hex}")
        run_root = (self.settings.output_dir / run_id).resolve()
        run_root.mkdir(parents=True, exist_ok=False)
        max_attempts = 1 + max(0, self.settings.excel_code_repair_attempts)
        attempt_history: list[dict[str, Any]] = []
        execution_result: dict[str, Any] | None = None
        generated_paths: list[Path] = []
        spreadsheet_paths: list[Path] = []
        primary_path: Path | None = None
        output_preview: dict[str, Any] | None = None
        output_dir = run_root

        for attempt in range(1, max_attempts + 1):
            output_dir = run_root / f"attempt-{attempt}"
            output_dir.mkdir(parents=True, exist_ok=False)
            yield SSEEventType.STATUS, {
                "message": f"正在使用 {execution_mode.value} 执行器处理 Excel",
                "attempt": attempt,
                "max_attempts": max_attempts,
            }
            try:
                if execution_mode == ExecutionMode.E2B:
                    execution_result = await self.e2b_client.execute(
                        code=generated_code,
                        input_path=file_path,
                        output_dir=output_dir,
                        timeout_seconds=self.settings.e2b_exec_timeout_seconds,
                    )
                else:
                    execution_result = await self.local_executor.execute(
                        code=generated_code,
                        input_path=file_path,
                        output_dir=output_dir,
                        timeout_seconds=self.settings.local_exec_timeout_seconds,
                    )
                generated_paths = self._validate_generated_outputs(
                    output_dir, execution_result.get("generated_files", [])
                )
                spreadsheet_paths = [
                    item
                    for item in generated_paths
                    if item.suffix.lower() in {".xlsx", ".xlsm", ".csv"}
                ]
                if not spreadsheet_paths:
                    raise SandboxExecutionError(
                        "处理代码执行完成，但没有生成可下载的 Excel/CSV 文件",
                        detail={
                            "generated_files": [str(item) for item in generated_paths],
                            "stdout": execution_result.get("stdout", "")[-4000:],
                            "stderr": execution_result.get("stderr", "")[-4000:],
                        },
                    )
                primary_path = self._select_primary_output(spreadsheet_paths)
                try:
                    output_preview = self.excel_parser.parse_preview(primary_path)
                except Exception as exc:
                    raise SandboxExecutionError(
                        "生成的 Excel/CSV 文件无法重新打开验证",
                        detail={
                            "path": str(primary_path),
                            "exception": str(exc),
                            "type": type(exc).__name__,
                        },
                    ) from exc
                execution_result["attempt_count"] = attempt
                break
            except SandboxExecutionError as exc:
                error_payload = serialize_exception(exc)
                attempt_history.append({"attempt": attempt, "error": error_payload})
                if attempt >= max_attempts:
                    raise
                yield SSEEventType.STATUS, {
                    "message": "执行失败，正在根据错误信息自动修复代码",
                    "attempt": attempt,
                    "error": error_payload,
                }
                repair_completion = await self.llm_client.complete_json(
                    system_prompt=compose_system_prompt(
                        EXCEL_CODE_SYSTEM_PROMPT, now=self._system_now()
                    ),
                    user_prompt=build_excel_repair_user_prompt(
                        instruction,
                        preview,
                        file_path.suffix.lower(),
                        generated_code,
                        error_payload,
                    ),
                    temperature=0.0,
                )
                repaired_plan = self._normalize_excel_plan_json(
                    repair_completion["content"]
                )
                generated_code = self._sanitize_python_code(
                    repaired_plan["python_code"]
                )
                yield SSEEventType.CODE, {
                    "python_code": generated_code,
                    "attempt": attempt + 1,
                    "repaired": True,
                }

        if execution_result is None:
            raise SandboxExecutionError("Excel 执行器未返回结果")
        if primary_path is None or output_preview is None:
            raise SandboxExecutionError("Excel 产物验证未返回结果")
        registered_outputs: list[dict[str, Any]] = []
        for generated_path in generated_paths:
            record = await self.file_manager.register_generated_file(generated_path)
            registered_outputs.append({
                "file_id": str(record["file_id"]), "filename": str(record["original_name"]),
                "size_bytes": int(record["size_bytes"]), "mime_type": str(record["mime_type"]),
                "file_type": generated_path.suffix.lower().lstrip("."),
                "download_url": f"/api/files/{record['file_id']}/download",
                "is_primary": generated_path == primary_path,
            })
        primary_output = next(item for item in registered_outputs if item["is_primary"])
        summary = self._read_execution_summary(output_dir, plan["task_analysis"])
        public_execution = {key: value for key, value in execution_result.items() if key not in {"generated_files", "remote_files"}}
        public_execution["generated_file_count"] = len(generated_paths)
        public_execution["repair_history"] = attempt_history
        final_payload = {
            "kind": "execution_result", "success": True, "summary": summary,
            "preview": preview, "output_preview": output_preview,
            "plan": {"task_analysis": plan["task_analysis"], "execution_steps": plan["execution_steps"], "expected_outputs": plan["expected_outputs"]},
            "execution": public_execution, "generated_files": [str(item) for item in generated_paths],
            "outputs": registered_outputs, "primary_output": primary_output,
        }
        yield SSEEventType.RESULT, final_payload
        yield SSEEventType.DONE, {"success": True, "output_file_id": primary_output["file_id"], "outputs": registered_outputs}

    def _default_excel_execution_mode(self) -> ExecutionMode:
        if self.settings.e2b_api_key:
            return ExecutionMode.E2B
        return self.settings.execution_mode

    @staticmethod
    def _safe_run_id(value: str) -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9_-]", "-", value).strip("-")
        return cleaned[:100] or f"run-{uuid4().hex}"

    @staticmethod
    def _validate_generated_outputs(output_dir: Path, paths: list[Any]) -> list[Path]:
        root = output_dir.resolve()
        valid: list[Path] = []
        for raw_path in paths:
            candidate = Path(str(raw_path)).resolve()
            if candidate == root or root not in candidate.parents:
                continue
            if candidate.is_file() and not candidate.name.startswith(".excel_task_"):
                valid.append(candidate)
        return sorted(set(valid), key=lambda item: item.name.lower())

    @staticmethod
    def _select_primary_output(paths: list[Path]) -> Path:
        preferred_names = ("processed_output.xlsx", "processed_output.xlsm", "processed_output.csv")
        by_name = {item.name.lower(): item for item in paths}
        for name in preferred_names:
            if name in by_name:
                return by_name[name]
        xlsx_files = [item for item in paths if item.suffix.lower() in {".xlsx", ".xlsm"}]
        return xlsx_files[0] if xlsx_files else paths[0]

    @staticmethod
    def _read_execution_summary(output_dir: Path, fallback: str) -> str:
        summary_path = output_dir / "summary.txt"
        if summary_path.exists():
            content = summary_path.read_text(encoding="utf-8", errors="replace").strip()
            if content:
                return content[:4000]
        return fallback or "Excel 文件已处理完成。"

    @staticmethod
    def _build_excel_answer(result: dict[str, Any]) -> str:
        primary = result["primary_output"]
        summary = str(result.get("summary") or "Excel 文件已处理完成。")
        return (
            f"Excel 已处理完成。{summary}\n\n"
            f"主结果文件：{primary['filename']}\n"
            f"文件 ID：{primary['file_id']}\n"
            f"下载地址：{primary['download_url']}"
        )

    @staticmethod
    def _normalize_chat_json(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "answer": str(payload.get("answer", "")).strip(),
            "summary": str(payload.get("summary", "")).strip(),
            "suggested_actions": [
                str(item).strip()
                for item in payload.get("suggested_actions", [])
                if str(item).strip()
            ],
            "confidence": str(payload.get("confidence", "medium")).lower(),
        }

    @staticmethod
    def _normalize_search_plan(
        payload: dict[str, Any],
        original_message: str,
    ) -> dict[str, Any]:
        queries: list[str] = []
        raw_queries = payload.get("queries", [])
        if not isinstance(raw_queries, list):
            raw_queries = []
        for item in raw_queries:
            query = " ".join(str(item).strip().split())
            if query and query not in queries:
                queries.append(query)
            if len(queries) >= 3:
                break
        if not queries:
            queries = [" ".join(original_message.strip().split())]

        topic = str(payload.get("topic", "general")).strip().lower()
        if topic not in {"general", "news", "finance"}:
            topic = "general"

        raw_time_range = payload.get("time_range")
        time_range = (
            str(raw_time_range).strip().lower()
            if raw_time_range is not None
            else None
        )
        if time_range not in {"day", "week", "month", "year"}:
            time_range = None

        return {
            "queries": queries,
            "topic": topic,
            "time_range": time_range,
            "reason": str(payload.get("reason", "")).strip(),
        }

    @staticmethod
    def _normalize_session_title(value: Any) -> str:
        title = " ".join(str(value or "").strip().split())
        punctuation = (
            "\u201c\u201d\u2018\u2019\"'"
            "\u300a\u300b\u3010\u3011[]\uff08\uff09()"
            "\uff0c,\u3002.!\uff01?\uff1f:\uff1a;\uff1b-\u2014_ "
        )
        title = title.strip(punctuation)
        about_prefix = "\u5173\u4e8e"
        if title.startswith(about_prefix):
            title = title[len(about_prefix):].strip()
        return title[:30]


    @staticmethod
    def _normalize_search_json(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "answer": str(payload.get("answer", "")).strip(),
            "summary": str(payload.get("summary", "")).strip(),
            "source_highlights": [
                str(item).strip()
                for item in payload.get("source_highlights", [])
                if str(item).strip()
            ],
            "confidence": str(payload.get("confidence", "medium")).lower(),
        }

    @staticmethod
    def _normalize_excel_guidance_json(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "answer": str(payload.get("answer", "")).strip(),
            "task_analysis": str(payload.get("task_analysis", "")).strip(),
            "recommended_steps": [
                str(item).strip()
                for item in payload.get("recommended_steps", [])
                if str(item).strip()
            ],
            "risk_checks": [
                str(item).strip()
                for item in payload.get("risk_checks", [])
                if str(item).strip()
            ],
            "confidence": str(payload.get("confidence", "medium")).lower(),
        }

    def _normalize_excel_plan_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        task_analysis = str(payload.get("task_analysis", "")).strip()
        execution_steps = [
            str(item).strip()
            for item in payload.get("execution_steps", [])
            if str(item).strip()
        ]
        expected_outputs = [
            str(item).strip()
            for item in payload.get("expected_outputs", [])
            if str(item).strip()
        ]
        python_code = str(payload.get("python_code", "")).strip()

        if not python_code:
            raise ModelInvocationError(
                "模型没有返回可执行 Python 代码",
                detail={"payload": payload},
            )

        return {
            "task_analysis": task_analysis,
            "execution_steps": execution_steps,
            "expected_outputs": expected_outputs,
            "python_code": python_code,
        }

    def _sanitize_python_code(self, code: str) -> str:
        cleaned = code.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()
        if "INPUT_PATH" not in cleaned or "OUTPUT_DIR" not in cleaned:
            raise ModelInvocationError(
                "模型生成的代码缺少 INPUT_PATH 或 OUTPUT_DIR 约定变量",
                detail={"code": cleaned[:2000]},
            )
        try:
            tree = ast.parse(cleaned)
        except SyntaxError as exc:
            raise ModelInvocationError(
                "模型生成的 Python 代码存在语法错误",
                detail={"line": exc.lineno, "message": exc.msg},
            ) from exc

        # Models sometimes emit placeholder assignments such as
        # INPUT_PATH = "input.xlsx" or OUTPUT_DIR = Path("output"). The
        # executor owns these values, so strip those statements instead of
        # rejecting an otherwise valid spreadsheet program. Both injected
        # values are Path objects, which also covers self-normalization.
        contract_names = {"INPUT_PATH", "OUTPUT_DIR"}

        def target_uses_contract_name(target: ast.AST) -> bool:
            return any(
                isinstance(child, ast.Name) and child.id in contract_names
                for child in ast.walk(target)
            )

        class ContractAssignmentStripper(ast.NodeTransformer):
            def visit_Assign(self, node: ast.Assign) -> ast.AST:
                if any(target_uses_contract_name(target) for target in node.targets):
                    return ast.copy_location(ast.Pass(), node)
                return self.generic_visit(node)

            def visit_AnnAssign(self, node: ast.AnnAssign) -> ast.AST:
                if target_uses_contract_name(node.target):
                    return ast.copy_location(ast.Pass(), node)
                return self.generic_visit(node)

            def visit_AugAssign(self, node: ast.AugAssign) -> ast.AST:
                if target_uses_contract_name(node.target):
                    return ast.copy_location(ast.Pass(), node)
                return self.generic_visit(node)

        tree = ContractAssignmentStripper().visit(tree)
        ast.fix_missing_locations(tree)

        # Normal spreadsheet code often needs os.path/os.makedirs or shutil.copy.
        # Allow those modules, but reject network/process modules and dangerous calls.
        blocked_modules = {
            "ftplib", "httpx", "paramiko", "requests",
            "socket", "subprocess", "sys", "urllib",
        }
        blocked_calls = {"__import__", "compile", "eval", "exec"}
        blocked_methods = {
            "execv", "execve", "execvp", "execvpe",
            "kill", "killpg", "move", "popen", "remove", "removedirs",
            "renames", "rmdir", "rmtree",
            "spawnl", "spawnle", "spawnlp", "spawnlpe",
            "spawnv", "spawnve", "spawnvp", "spawnvpe",
            "system", "unlink",
        }
        blocked_imported_members = {
            "os": blocked_methods,
            "shutil": {"move", "rmtree"},
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Store)
                and node.id in contract_names
            ):
                raise ModelInvocationError(
                    "生成代码不能在赋值语句之外覆盖 INPUT_PATH 或 OUTPUT_DIR",
                    detail={"variable": node.id, "line": getattr(node, "lineno", None)},
                )

            imported_modules: list[str] = []
            if isinstance(node, ast.Import):
                imported_modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules = [node.module]
                root_module = node.module.split(".", 1)[0]
                blocked_members = blocked_imported_members.get(root_module, set())
                for alias in node.names:
                    if alias.name in blocked_members:
                        raise ModelInvocationError(
                            "生成代码包含不允许的系统或删除操作",
                            detail={"call": alias.name, "line": getattr(node, "lineno", None)},
                        )
            for module_name in imported_modules:
                root_module = module_name.split(".", 1)[0]
                if root_module in blocked_modules:
                    raise ModelInvocationError(
                        "生成代码包含不允许的模块",
                        detail={"module": module_name, "line": getattr(node, "lineno", None)},
                    )

            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in blocked_calls:
                    raise ModelInvocationError(
                        "生成代码包含不允许的动态执行调用",
                        detail={"call": node.func.id, "line": getattr(node, "lineno", None)},
                    )
                if isinstance(node.func, ast.Attribute) and node.func.attr in blocked_methods:
                    raise ModelInvocationError(
                        "生成代码包含不允许的系统或删除操作",
                        detail={"call": node.func.attr, "line": getattr(node, "lineno", None)},
                    )
        return ast.unparse(tree).strip()

    @staticmethod
    def _system_now() -> datetime:
        return datetime.now().astimezone()

    @staticmethod
    def _is_local_time_question(message: str) -> bool:
        normalized = " ".join(message.strip().lower().split())
        patterns = (
            r"今天.*(几月几号|几号|日期|星期几|周几)",
            r"现在.*(几点|时间)",
            r"当前.*(时间|日期|星期几|周几)",
            r"^(今天是几月几号|今天几号|今天星期几|今天周几|现在几点|当前时间|当前日期)[\?？]?$",
            r"^what('?s| is)? the date( today)?\??$",
            r"^what time is it( now)?\??$",
        )
        return any(re.search(pattern, normalized) for pattern in patterns)

    def _build_local_time_result(
        self,
        *,
        message: str,
        current_time: datetime,
        template_name: str | None,
    ) -> dict[str, Any]:
        answer = self._format_local_time_answer(message, current_time)
        structured_output = {
            "answer": answer,
            "summary": "当前问题已直接使用系统本地时间回答，无需联网搜索。",
            "suggested_actions": [],
            "confidence": "high",
        }
        request_payload = {
            "model": "local-system-time",
            "messages": [
                {
                    "role": "system",
                    "content": "你是系统本地时间回答器，直接根据系统当前时间回答。",
                },
                {"role": "user", "content": message},
            ],
            "stream": False,
        }
        response_payload = self._build_synthetic_openai_response(
            model="local-system-time",
            content=answer,
            current_time=current_time,
        )
        return {
            "content": answer,
            "citations": [],
            "metadata": {
                "intent": AgentIntent.CHAT,
                "template_name": template_name,
                "model": "local-system-time",
                "usage": {},
                "current_time": current_time.isoformat(),
                "structured_output": structured_output,
                "local_answer": {"kind": "system_time"},
            },
            "storage": self._build_openai_storage_payload(
                intent=AgentIntent.CHAT,
                current_time=current_time,
                request_payload=request_payload,
                response_payload=response_payload,
                template_name=template_name,
                tool_context={"local_answer": {"kind": "system_time"}},
                structured_output=structured_output,
            ),
        }

    @staticmethod
    def _format_local_time_answer(message: str, current_time: datetime) -> str:
        lower_message = message.lower()
        weekday_map = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        weekday_text = weekday_map[current_time.weekday()]

        if any(keyword in lower_message for keyword in ("星期几", "周几")):
            return (
                f"今天是 {current_time.year} 年 {current_time.month} 月 {current_time.day} 日，"
                f"{weekday_text}。"
            )
        if any(keyword in lower_message for keyword in ("几点", "时间")):
            return (
                f"现在的系统本地时间是 {current_time.year} 年 {current_time.month} 月 {current_time.day} 日 "
                f"{current_time.strftime('%H:%M:%S')}，{weekday_text}。"
            )
        return (
            f"今天是 {current_time.year} 年 {current_time.month} 月 {current_time.day} 日，"
            f"{weekday_text}。"
        )

    @staticmethod
    def _build_synthetic_openai_response(
        *,
        model: str,
        content: str,
        current_time: datetime,
    ) -> dict[str, Any]:
        return {
            "id": f"chatcmpl-{uuid4().hex}",
            "object": "chat.completion",
            "created": int(current_time.timestamp()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        }

    @staticmethod
    def _build_openai_storage_payload(
        *,
        intent: AgentIntent,
        current_time: datetime,
        request_payload: dict[str, Any],
        response_payload: dict[str, Any],
        template_name: str | None,
        citations: list[dict[str, Any]] | None = None,
        tool_context: dict[str, Any] | None = None,
        structured_output: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "protocol": "openai_chat_completions",
            "intent": str(intent),
            "request": request_payload,
            "response": response_payload,
            "context": {
                "current_time": current_time.isoformat(),
                "template_name": template_name,
                "tool_context": tool_context or {},
            },
            "citations": citations or [],
            "structured_output": structured_output or {},
        }
