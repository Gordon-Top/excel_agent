from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, UploadFile, status
from fastapi.responses import FileResponse
from sse_starlette.sse import EventSourceResponse

from app.api.dependencies import (
    get_app_settings,
    get_file_manager,
    get_session_id,
    get_task_manager,
)
from app.constants.enums import ExecutionMode, FileRole, SSEEventType, TaskStatus, TaskType
from app.core.config import Settings
from app.core.exceptions import AppError, serialize_exception
from app.schemas.payloads import (
    ExcelPreviewRequest,
    ExcelPreviewResponse,
    ExcelProcessRequest,
    ExcelProcessResponse,
    FileUploadResponse,
)
from app.services.agent.router import AgentRouter
from app.services.chat_history import (
    append_chat_turn_message,
    build_model_context,
    cache_completed_turn,
    ensure_chat_session,
    get_excel_file_candidates,
    load_conversation_context,
    maybe_compress_conversation,
    set_session_title,
)
from app.services.tools.excel_parser import ExcelParser
from app.utils.sse_formatter import iter_sse

router = APIRouter()


@router.post("/upload", response_model=FileUploadResponse)
async def upload_file(
    file: UploadFile = File(...),
    settings: Settings = Depends(get_app_settings),
) -> FileUploadResponse:
    file_manager = get_file_manager(settings)
    record = await file_manager.save_upload(file, role=FileRole.INPUT)
    return FileUploadResponse(**record)


@router.post("/preview", response_model=ExcelPreviewResponse)
async def preview_file(
    payload: ExcelPreviewRequest,
    settings: Settings = Depends(get_app_settings),
) -> ExcelPreviewResponse:
    file_manager = get_file_manager(settings)
    parser = ExcelParser()
    preview = parser.parse_preview(await file_manager.resolve_path_async(payload.file_id))
    return ExcelPreviewResponse(file_id=payload.file_id, **preview)


@router.get("/{file_id}/download")
async def download_file(
    file_id: str,
    settings: Settings = Depends(get_app_settings),
) -> FileResponse:
    file_manager = get_file_manager(settings)
    record = await file_manager.get_async(file_id)
    path = await file_manager.resolve_path_async(file_id)
    if not path.exists() or not path.is_file():
        raise AppError(
            "文件记录存在，但磁盘文件已丢失",
            error_code="file_missing_on_disk",
            detail={"file_id": file_id},
        )
    return FileResponse(
        path=path,
        filename=str(record["original_name"]),
        media_type=str(record["mime_type"]),
    )


def _empty_window() -> dict:
    return {
        "summary": None,
        "messages": [],
        "turns": [],
        "context_message_count": 0,
        "context_turn_count": 0,
        "context_truncated": False,
        "summary_applied": False,
        "summary_turn_count": 0,
    }


async def _load_context_window(settings, task_manager, session_id: str):
    # 兼容只实现旧 TaskManager 接口的轻量测试替身；生产始终走新上下文服务。
    if not hasattr(task_manager, "get_session_context_cache"):
        return {}, _empty_window(), "memory"
    context, source = await load_conversation_context(settings, task_manager, session_id)
    return context, build_model_context(settings, context), source


async def _file_exists(file_manager, file_id: str) -> bool:
    resolver = getattr(file_manager, "resolve_path_async", None)
    if resolver is not None:
        try:
            return Path(await resolver(file_id)).is_file()
        except Exception:
            return False
    getter = getattr(file_manager, "get_async", None)
    if getter is None:
        return True
    try:
        await getter(file_id)
        return True
    except Exception:
        return False


async def _resolve_excel_file(
    settings: Settings,
    file_manager,
    *,
    session_id: str,
    explicit_file_id: str | None,
    context: dict,
) -> tuple[str, str, str | None]:
    if explicit_file_id:
        if not await _file_exists(file_manager, explicit_file_id):
            # 显式文件优先且不会静默换成历史文件，避免处理错误工作簿。
            await file_manager.get_async(explicit_file_id)
        return explicit_file_id, "explicit", None

    candidates = await get_excel_file_candidates(
        settings, session_id, context=context, require_latest_success_excel=False
    )
    for candidate in candidates:
        if await _file_exists(file_manager, candidate["file_id"]):
            return (
                candidate["file_id"],
                candidate["file_source"],
                candidate.get("parent_task_id"),
            )
    raise AppError(
        "当前会话中没有可继续处理的 Excel 文件",
        error_code="conversation_file_not_found",
    )


async def _invalidate_context_before_commit(
    settings: Settings, task_manager, session_id: str
) -> None:
    if not settings.mysql_dsn:
        return
    invalidator = getattr(task_manager, "invalidate_session_context", None)
    if invalidator is not None:
        await invalidator(session_id)


def _context_metadata(
    window: dict, source: str, file_id: str, file_source: str,
    parent_task_id: str | None,
) -> dict:
    return {
        "context_source": source,
        "context_message_count": window["context_message_count"],
        "context_turn_count": window["context_turn_count"],
        "context_truncated": window["context_truncated"],
        "summary_applied": window["summary_applied"],
        "summary_turn_count": window["summary_turn_count"],
        "resolved_file_id": file_id,
        "file_source": file_source,
        "parent_task_id": parent_task_id,
    }


async def _acquire_session_lock(task_manager, session_id: str, owner: str) -> bool:
    acquire = getattr(task_manager, "acquire_session_lock", None)
    return await acquire(session_id, owner) if acquire else True


async def _release_session_lock(task_manager, session_id: str, owner: str) -> None:
    release = getattr(task_manager, "release_session_lock", None)
    if release:
        await release(session_id, owner)


def _start_lock_heartbeat(task_manager, session_id: str, owner: str):
    starter = getattr(task_manager, "start_session_lock_heartbeat", None)
    return starter(session_id, owner) if starter else None


async def _stop_lock_heartbeat(task_manager, heartbeat) -> None:
    stopper = getattr(task_manager, "stop_session_lock_heartbeat", None)
    if stopper:
        await stopper(heartbeat)


async def _record_summary_error(
    task_manager, task_id: str, storage: dict, error: str | None
) -> None:
    if not error:
        return
    try:
        updated_storage = dict(storage)
        updated_storage["conversation_summary_error"] = error
        await task_manager.update_task(task_id, metadata=updated_storage)
    except Exception:
        return


async def _try_set_title(
    settings: Settings,
    agent_router: AgentRouter,
    *,
    session_id: str,
    needs_title: bool,
    user_message: str,
    assistant_answer: str,
) -> None:
    if not needs_title:
        return
    try:
        generated_title = await agent_router.generate_session_title(
            user_message=user_message,
            assistant_answer=assistant_answer,
        )
        await set_session_title(settings, session_id=session_id, title=generated_title)
    except Exception:
        return


async def _cache_and_compress(
    settings: Settings,
    task_manager,
    agent_router: AgentRouter,
    *,
    session_id: str,
    task: dict,
    user_message: str,
    assistant_answer: str,
) -> dict:
    if not hasattr(task_manager, "set_session_context_cache"):
        # 旧测试替身兼容；生产不会再写旧的逐消息 Redis List。
        appender = getattr(task_manager, "append_session_memory", None)
        if appender:
            await appender(session_id, role="user", content=user_message)
            await appender(session_id, role="assistant", content=assistant_answer)
        return {"compressed": False}
    await cache_completed_turn(
        settings,
        task_manager,
        session_id=session_id,
        task=task,
        messages=[
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": assistant_answer},
        ],
    )
    return await maybe_compress_conversation(
        settings,
        task_manager,
        session_id,
        agent_router.generate_conversation_summary,
    )


@router.post("/process", response_model=ExcelProcessResponse)
async def process_file(
    payload: ExcelProcessRequest,
    settings: Settings = Depends(get_app_settings),
    session_id_header: str = Depends(get_session_id),
) -> ExcelProcessResponse:
    file_manager = get_file_manager(settings)
    task_manager = get_task_manager(settings)
    agent_router = AgentRouter(settings, file_manager)
    session_id = payload.session_id or session_id_header or f"session-{uuid4().hex}"
    owner = f"excel-{uuid4().hex}"
    if not await _acquire_session_lock(task_manager, session_id, owner):
        raise AppError(
            "当前会话正在处理其他请求",
            error_code="session_busy",
            status_code=status.HTTP_409_CONFLICT,
        )
    heartbeat = _start_lock_heartbeat(task_manager, session_id, owner)
    task = None
    try:
        context, window, source = await _load_context_window(
            settings, task_manager, session_id
        )
        resolved_file_id, file_source, parent_task_id = await _resolve_excel_file(
            settings,
            file_manager,
            session_id=session_id,
            explicit_file_id=payload.file_id,
            context=context,
        )
        execution_mode = payload.execution_mode or (
            ExecutionMode.E2B if settings.e2b_api_key else settings.execution_mode
        )
        task = await task_manager.create_task(
            session_id=session_id,
            task_type=TaskType.EXCEL,
            instruction=payload.instruction,
            execution_mode=execution_mode,
            template_name=payload.template_name,
            input_file_id=resolved_file_id,
        )
        needs_session_title = await ensure_chat_session(settings, session_id)
        await append_chat_turn_message(
            settings,
            session_id=session_id,
            task_id=task["task_id"],
            role="user",
            content=payload.instruction,
            metadata={
                "task_type": "excel",
                "file_id": resolved_file_id,
                "file_source": file_source,
                "parent_task_id": parent_task_id,
            },
        )
        await task_manager.update_task(task["task_id"], status=TaskStatus.RUNNING)

        final_result: dict | None = None
        async for event_name, event_payload in agent_router.process_excel(
            file_id=resolved_file_id,
            instruction=payload.instruction,
            execution_mode=execution_mode,
            task_id=task["task_id"],
            conversation_messages=window["messages"],
            conversation_summary=window["summary"],
        ):
            await task_manager.append_task_event(task["task_id"], event_name, event_payload)
            if (
                event_name == SSEEventType.RESULT
                and isinstance(event_payload, dict)
                and event_payload.get("kind") == "execution_result"
            ):
                final_result = event_payload
        if final_result is None:
            raise AppError("Excel 任务未返回执行结果", error_code="excel_result_missing")

        primary = final_result["primary_output"]
        content = AgentRouter._build_excel_answer(final_result)
        storage = {
            "excel": final_result,
            "context": _context_metadata(
                window, source, resolved_file_id, file_source, parent_task_id
            ),
        }
        await _invalidate_context_before_commit(settings, task_manager, session_id)
        updated_task = await task_manager.update_task(
            task["task_id"],
            status=TaskStatus.SUCCEEDED,
            output_file_id=primary["file_id"],
            result_summary=content,
            metadata=storage,
        )
        await append_chat_turn_message(
            settings,
            session_id=session_id,
            task_id=task["task_id"],
            role="assistant",
            content=content,
            metadata={
                "task_type": "excel",
                "output_file_id": primary["file_id"],
                "outputs": final_result["outputs"],
            },
        )
        await _try_set_title(
            settings,
            agent_router,
            session_id=session_id,
            needs_title=needs_session_title,
            user_message=payload.instruction,
            assistant_answer=content,
        )
        compression = await _cache_and_compress(
            settings,
            task_manager,
            agent_router,
            session_id=session_id,
            task=updated_task,
            user_message=payload.instruction,
            assistant_answer=content,
        )
        metadata = _context_metadata(
            window, source, resolved_file_id, file_source, parent_task_id
        )
        if compression.get("error"):
            metadata["summary_error"] = compression["error"]
            await _record_summary_error(
                task_manager, task["task_id"], storage, compression["error"]
            )
        return ExcelProcessResponse(
            session_id=session_id,
            task_id=task["task_id"],
            content=content,
            output_file_id=primary["file_id"],
            outputs=final_result["outputs"],
            execution=final_result["execution"],
            preview=final_result.get("output_preview"),
            metadata=metadata,
        )
    except Exception as exc:
        if task is not None:
            await task_manager.update_task(
                task["task_id"],
                status=TaskStatus.FAILED,
                error_message=str(exc),
                metadata={"excel": {"error": serialize_exception(exc)}},
            )
            await append_chat_turn_message(
                settings,
                session_id=session_id,
                task_id=task["task_id"],
                role="system",
                content=f"Excel 任务处理失败：{exc}",
                metadata={"task_type": "excel", "error": serialize_exception(exc)},
            )
        raise
    finally:
        await _stop_lock_heartbeat(task_manager, heartbeat)
        await _release_session_lock(task_manager, session_id, owner)


@router.post("/process/stream")
async def process_file_stream(
    payload: ExcelProcessRequest,
    settings: Settings = Depends(get_app_settings),
    session_id_header: str = Depends(get_session_id),
) -> EventSourceResponse:
    file_manager = get_file_manager(settings)
    task_manager = get_task_manager(settings)
    agent_router = AgentRouter(settings, file_manager)
    session_id = payload.session_id or session_id_header or f"session-{uuid4().hex}"
    owner = task_manager.generate_task_id()
    if not await _acquire_session_lock(task_manager, session_id, owner):
        raise AppError(
            "当前会话正在处理其他请求",
            error_code="session_busy",
            status_code=status.HTTP_409_CONFLICT,
        )
    heartbeat = _start_lock_heartbeat(task_manager, session_id, owner)

    try:
        context, window, source = await _load_context_window(
            settings, task_manager, session_id
        )
        resolved_file_id, file_source, parent_task_id = await _resolve_excel_file(
            settings,
            file_manager,
            session_id=session_id,
            explicit_file_id=payload.file_id,
            context=context,
        )
        fingerprint = task_manager.create_idempotency_fingerprint(
            session_id=session_id,
            task_type=TaskType.EXCEL,
            file_id=resolved_file_id,
            instruction=payload.instruction,
        )
        if not await task_manager.reserve_idempotency(fingerprint, owner):
            existing_task_id = await task_manager.get_idempotency_task_id(fingerprint)
            raise AppError(
                "相同的 Excel 任务刚刚提交过，请稍后再试",
                error_code="duplicate_excel_task",
                detail={"task_id": existing_task_id},
            )
        execution_mode = payload.execution_mode or (
            ExecutionMode.E2B if settings.e2b_api_key else settings.execution_mode
        )
        task = await task_manager.create_task(
            session_id=session_id,
            task_type=TaskType.EXCEL,
            instruction=payload.instruction,
            execution_mode=execution_mode,
            template_name=payload.template_name,
            input_file_id=resolved_file_id,
            task_id=owner,
        )
        needs_session_title = await ensure_chat_session(settings, session_id)
        await append_chat_turn_message(
            settings,
            session_id=session_id,
            task_id=task["task_id"],
            role="user",
            content=payload.instruction,
            metadata={
                "task_type": "excel",
                "file_id": resolved_file_id,
                "file_source": file_source,
                "parent_task_id": parent_task_id,
            },
        )
        await task_manager.update_task(task["task_id"], status=TaskStatus.RUNNING)
        await task_manager.append_task_event(
            task["task_id"], SSEEventType.STATUS, {"message": "Excel 任务已开始"}
        )
    except Exception:
        await _stop_lock_heartbeat(task_manager, heartbeat)
        await _release_session_lock(task_manager, session_id, owner)
        raise

    async def _event_stream():
        output_file_id: str | None = None
        final_result: dict | None = None
        try:
            async for event_name, event_payload in agent_router.process_excel(
                file_id=resolved_file_id,
                instruction=payload.instruction,
                execution_mode=execution_mode,
                task_id=task["task_id"],
                conversation_messages=window["messages"],
                conversation_summary=window["summary"],
            ):
                await task_manager.append_task_event(
                    task["task_id"], event_name, event_payload
                )
                if (
                    event_name == SSEEventType.RESULT
                    and isinstance(event_payload, dict)
                    and event_payload.get("kind") == "execution_result"
                ):
                    output_file_id = event_payload["primary_output"]["file_id"]
                    final_result = event_payload
                yield event_name, event_payload

            if final_result is None:
                raise AppError("Excel 任务未返回执行结果", error_code="excel_result_missing")
            final_content = AgentRouter._build_excel_answer(final_result)
            storage = {
                "excel": final_result,
                "context": _context_metadata(
                    window, source, resolved_file_id, file_source, parent_task_id
                ),
            }
            await _invalidate_context_before_commit(settings, task_manager, session_id)
            updated_task = await task_manager.update_task(
                task["task_id"],
                status=TaskStatus.SUCCEEDED,
                output_file_id=output_file_id,
                result_summary=final_content,
                metadata=storage,
            )
            await append_chat_turn_message(
                settings,
                session_id=session_id,
                task_id=task["task_id"],
                role="assistant",
                content=final_content,
                metadata={
                    "task_type": "excel",
                    "output_file_id": output_file_id,
                    "outputs": final_result.get("outputs", []),
                },
            )
            await _try_set_title(
                settings,
                agent_router,
                session_id=session_id,
                needs_title=needs_session_title,
                user_message=payload.instruction,
                assistant_answer=final_content,
            )
            compression = await _cache_and_compress(
                settings,
                task_manager,
                agent_router,
                session_id=session_id,
                task=updated_task,
                user_message=payload.instruction,
                assistant_answer=final_content,
            )
            metadata = _context_metadata(
                window, source, resolved_file_id, file_source, parent_task_id
            )
            if compression.get("error"):
                metadata["summary_error"] = compression["error"]
                await _record_summary_error(
                    task_manager, task["task_id"], storage, compression["error"]
                )
            yield SSEEventType.DONE, {
                "success": True,
                "content": final_content,
                "output_file_id": output_file_id,
                "outputs": final_result.get("outputs", []),
                "summary_compressed": bool(compression.get("compressed")),
                "metadata": metadata,
            }
        except Exception as exc:
            await task_manager.update_task(
                task["task_id"],
                status=TaskStatus.FAILED,
                error_message=str(exc),
                metadata={"excel": {"error": serialize_exception(exc)}},
            )
            await task_manager.append_task_event(
                task["task_id"], SSEEventType.ERROR, {"message": str(exc)}
            )
            await append_chat_turn_message(
                settings,
                session_id=session_id,
                task_id=task["task_id"],
                role="system",
                content=f"Excel 任务处理失败：{exc}",
                metadata={"task_type": "excel", "error": serialize_exception(exc)},
            )
            yield SSEEventType.ERROR, {"message": str(exc)}
        finally:
            await _stop_lock_heartbeat(task_manager, heartbeat)
            await _release_session_lock(task_manager, session_id, owner)

    return EventSourceResponse(iter_sse(_event_stream()))
