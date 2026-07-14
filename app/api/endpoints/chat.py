from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, status
from sse_starlette.sse import EventSourceResponse

from app.api.dependencies import get_app_settings, get_file_manager, get_session_id, get_task_manager
from app.constants.enums import AgentIntent, ExecutionMode, SSEEventType, TaskStatus, TaskType
from app.core.config import Settings
from app.core.exceptions import AppError, serialize_exception
from app.schemas.payloads import (
    ChatRequest,
    ChatResponse,
    ChatSessionCreateRequest,
    ChatSessionCreateResponse,
    ChatSessionListResponse,
    ChatTurnListResponse,
)
from app.services.agent.router import AgentRouter
from app.services.chat_history import (
    append_chat_turn_message,
    build_model_context,
    cache_completed_turn,
    create_chat_session,
    ensure_chat_session,
    get_excel_file_candidates,
    list_chat_sessions,
    list_chat_turns,
    load_conversation_context,
    maybe_compress_conversation,
    set_session_title,
)
from app.utils.sse_formatter import iter_sse

router = APIRouter()


@router.post("/sessions", response_model=ChatSessionCreateResponse)
async def new_chat_session(
    payload: ChatSessionCreateRequest,
    settings: Settings = Depends(get_app_settings),
) -> ChatSessionCreateResponse:
    created = await create_chat_session(settings, title=payload.title)
    return ChatSessionCreateResponse(**created)


@router.get("/sessions", response_model=ChatSessionListResponse)
async def get_chat_sessions(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    settings: Settings = Depends(get_app_settings),
) -> ChatSessionListResponse:
    sessions, total = await list_chat_sessions(settings, offset=offset, limit=limit)
    return ChatSessionListResponse(sessions=sessions, total=total, offset=offset, limit=limit)


@router.get("/sessions/{session_id}/turns", response_model=ChatTurnListResponse)
async def get_chat_session_turns(
    session_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    settings: Settings = Depends(get_app_settings),
) -> ChatTurnListResponse:
    turns, total = await list_chat_turns(
        settings, session_id=session_id, offset=offset, limit=limit
    )
    return ChatTurnListResponse(
        session_id=session_id, turns=turns, total=total, offset=offset, limit=limit
    )


def _looks_like_excel_followup(message: str) -> bool:
    text = message.lower()
    continuation = ("继续", "再", "然后", "接着", "上一份", "刚才", "修改", "调整")
    operations = (
        "excel", "表格", "工作簿", "sheet", "行", "列", "排序", "筛选", "合并",
        "格式", "公式", "图表", "单元格", "小数", "日期", "删除", "新增",
    )
    return any(item in text for item in operations) or (
        any(item in text for item in continuation)
        and any(item in text for item in ("加", "删", "改", "排", "填", "导出", "保存"))
    )


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


async def _resolve_chat_file(
    settings: Settings,
    file_manager,
    session_id: str,
    explicit_file_id: str | None,
    message: str,
    context: dict,
) -> tuple[str | None, str, str | None]:
    if explicit_file_id:
        await file_manager.get_async(explicit_file_id)
        return explicit_file_id, "explicit", None
    if not _looks_like_excel_followup(message):
        return None, "none", None

    candidates = await get_excel_file_candidates(
        settings,
        session_id,
        context=context,
        require_latest_success_excel=True,
    )
    for candidate in candidates:
        if await _file_exists(file_manager, candidate["file_id"]):
            return (
                candidate["file_id"],
                candidate["file_source"],
                candidate.get("parent_task_id"),
            )
    if candidates:
        raise AppError(
            "当前会话中没有可继续处理的 Excel 文件",
            error_code="conversation_file_not_found",
        )
    return None, "none", None


async def _invalidate_context_before_commit(
    settings: Settings, task_manager, session_id: str
) -> None:
    if not settings.mysql_dsn:
        return
    invalidator = getattr(task_manager, "invalidate_session_context", None)
    if invalidator is not None:
        await invalidator(session_id)


def _context_metadata(
    window: dict, source: str, file_id: str | None, file_source: str,
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
        title = await agent_router.generate_session_title(
            user_message=user_message, assistant_answer=assistant_answer
        )
        await set_session_title(settings, session_id=session_id, title=title)
    except Exception:
        # 标题属于非关键增强功能，失败不能反向把已成功的聊天任务标记为失败。
        return


@router.post("/complete", response_model=ChatResponse)
async def complete_chat(
    payload: ChatRequest,
    settings: Settings = Depends(get_app_settings),
    session_id_header: str = Depends(get_session_id),
) -> ChatResponse:
    file_manager = get_file_manager(settings)
    task_manager = get_task_manager(settings)
    agent_router = AgentRouter(settings, file_manager)
    session_id = payload.session_id or session_id_header
    owner = f"chat-{uuid4().hex}"
    if not await task_manager.acquire_session_lock(session_id, owner):
        raise AppError(
            "当前会话正在处理其他请求",
            error_code="session_busy",
            status_code=status.HTTP_409_CONFLICT,
        )
    heartbeat = _start_lock_heartbeat(task_manager, session_id, owner)

    task = None
    try:
        needs_title = await ensure_chat_session(settings, session_id)
        context, source = await load_conversation_context(settings, task_manager, session_id)
        window = build_model_context(settings, context)
        resolved_file_id, file_source, parent_task_id = await _resolve_chat_file(
            settings, file_manager, session_id, payload.file_id, payload.message, context
        )
        intent = agent_router.detect_intent(
            message=payload.message,
            allow_search=payload.allow_search,
            file_id=resolved_file_id,
        )
        execution_mode = payload.execution_mode or (
            ExecutionMode.E2B
            if intent == AgentIntent.EXCEL and settings.e2b_api_key
            else settings.execution_mode
        )
        task = await task_manager.create_task(
            session_id=session_id,
            task_type=_map_intent_to_task_type(intent),
            instruction=payload.message,
            execution_mode=execution_mode,
            template_name=payload.template_name,
            input_file_id=resolved_file_id,
        )
        await append_chat_turn_message(
            settings,
            session_id=session_id,
            task_id=task["task_id"],
            role="user",
            content=payload.message,
            metadata={
                "file_id": resolved_file_id,
                "file_source": file_source,
                "parent_task_id": parent_task_id,
            },
        )
        await task_manager.update_task(task["task_id"], status=TaskStatus.RUNNING)
        result = await agent_router.complete_chat(
            message=payload.message,
            allow_search=payload.allow_search,
            file_id=resolved_file_id,
            template_name=payload.template_name,
            execution_mode=execution_mode,
            task_id=task["task_id"],
            conversation_messages=window["messages"],
            conversation_summary=window["summary"],
        )
        metadata = {
            **result["metadata"],
            **_context_metadata(
                window, source, resolved_file_id, file_source, parent_task_id
            ),
        }
        await _invalidate_context_before_commit(settings, task_manager, session_id)
        updated_task = await task_manager.update_task(
            task["task_id"],
            status=TaskStatus.SUCCEEDED,
            result_summary=result["content"],
            metadata=result["storage"],
            output_file_id=result.get("output_file_id"),
        )
        await append_chat_turn_message(
            settings,
            session_id=session_id,
            task_id=task["task_id"],
            role="assistant",
            content=result["content"],
            metadata={
                "output_file_id": result.get("output_file_id"),
                "outputs": result.get("outputs", []),
            },
        )
        await _try_set_title(
            settings,
            agent_router,
            session_id=session_id,
            needs_title=needs_title,
            user_message=payload.message,
            assistant_answer=result["content"],
        )
        await cache_completed_turn(
            settings,
            task_manager,
            session_id=session_id,
            task=updated_task,
            messages=[
                {"role": "user", "content": payload.message},
                {"role": "assistant", "content": result["content"]},
            ],
        )
        compression = await maybe_compress_conversation(
            settings,
            task_manager,
            session_id,
            agent_router.generate_conversation_summary,
        )
        if compression.get("error"):
            metadata["summary_error"] = compression["error"]
            await _record_summary_error(
                task_manager, task["task_id"], result["storage"], compression["error"]
            )
        await task_manager.append_task_event(
            task["task_id"],
            SSEEventType.DONE,
            {
                "success": True,
                "output_file_id": result.get("output_file_id"),
                "outputs": result.get("outputs", []),
            },
        )
        return ChatResponse(
            session_id=session_id,
            task_id=task["task_id"],
            task_type=_map_intent_to_task_type(intent),
            content=result["content"],
            citations=result["citations"],
            metadata=metadata,
            output_file_id=result.get("output_file_id"),
            outputs=result.get("outputs", []),
        )
    except Exception as exc:
        if task is not None:
            await task_manager.update_task(
                task["task_id"],
                status=TaskStatus.FAILED,
                error_message=str(exc),
                metadata={"error": serialize_exception(exc)},
            )
            await append_chat_turn_message(
                settings,
                session_id=session_id,
                task_id=task["task_id"],
                role="system",
                content=f"聊天任务失败：{exc}",
            )
        raise
    finally:
        await _stop_lock_heartbeat(task_manager, heartbeat)
        await task_manager.release_session_lock(session_id, owner)


@router.post("/stream")
async def stream_chat(
    payload: ChatRequest,
    settings: Settings = Depends(get_app_settings),
    session_id_header: str = Depends(get_session_id),
) -> EventSourceResponse:
    file_manager = get_file_manager(settings)
    task_manager = get_task_manager(settings)
    agent_router = AgentRouter(settings, file_manager)
    session_id = payload.session_id or session_id_header
    owner = f"chat-{uuid4().hex}"
    if not await task_manager.acquire_session_lock(session_id, owner):
        raise AppError(
            "当前会话正在处理其他请求",
            error_code="session_busy",
            status_code=status.HTTP_409_CONFLICT,
        )
    heartbeat = _start_lock_heartbeat(task_manager, session_id, owner)

    try:
        needs_title = await ensure_chat_session(settings, session_id)
        context, source = await load_conversation_context(settings, task_manager, session_id)
        window = build_model_context(settings, context)
        resolved_file_id, file_source, parent_task_id = await _resolve_chat_file(
            settings, file_manager, session_id, payload.file_id, payload.message, context
        )
        intent = agent_router.detect_intent(
            message=payload.message,
            allow_search=payload.allow_search,
            file_id=resolved_file_id,
        )
        execution_mode = payload.execution_mode or (
            ExecutionMode.E2B
            if intent == AgentIntent.EXCEL and settings.e2b_api_key
            else settings.execution_mode
        )
        task = await task_manager.create_task(
            session_id=session_id,
            task_type=_map_intent_to_task_type(intent),
            instruction=payload.message,
            execution_mode=execution_mode,
            template_name=payload.template_name,
            input_file_id=resolved_file_id,
        )
        await task_manager.update_task(task["task_id"], status=TaskStatus.RUNNING)
        await append_chat_turn_message(
            settings,
            session_id=session_id,
            task_id=task["task_id"],
            role="user",
            content=payload.message,
            metadata={
                "file_id": resolved_file_id,
                "file_source": file_source,
                "parent_task_id": parent_task_id,
            },
        )
    except Exception:
        await _stop_lock_heartbeat(task_manager, heartbeat)
        await task_manager.release_session_lock(session_id, owner)
        raise

    async def _event_stream():
        final_content = ""
        final_storage: dict = {}
        final_output_file_id = None
        final_outputs: list[dict] = []
        try:
            yield SSEEventType.STATUS, {
                "message": "聊天任务已开始",
                "session_id": session_id,
                "task_id": task["task_id"],
            }
            async for event_name, event_payload in agent_router.stream_chat(
                message=payload.message,
                allow_search=payload.allow_search,
                file_id=resolved_file_id,
                template_name=payload.template_name,
                execution_mode=execution_mode,
                task_id=task["task_id"],
                conversation_messages=window["messages"],
                conversation_summary=window["summary"],
            ):
                await task_manager.append_task_event(
                    task["task_id"], event_name, event_payload
                )
                if event_name == SSEEventType.MESSAGE and isinstance(event_payload, dict):
                    final_content = str(event_payload.get("content", final_content))
                    event_payload.setdefault("metadata", {}).update(
                        _context_metadata(
                            window, source, resolved_file_id, file_source, parent_task_id
                        )
                    )
                if event_name == SSEEventType.DONE and isinstance(event_payload, dict):
                    final_storage = dict(event_payload.get("storage", {}))
                    final_output_file_id = event_payload.get("output_file_id")
                    final_outputs = list(event_payload.get("outputs", []))
                    continue
                yield event_name, event_payload

            await _invalidate_context_before_commit(settings, task_manager, session_id)
            updated_task = await task_manager.update_task(
                task["task_id"],
                status=TaskStatus.SUCCEEDED,
                result_summary=final_content,
                output_file_id=final_output_file_id,
                metadata=final_storage,
            )
            await append_chat_turn_message(
                settings,
                session_id=session_id,
                task_id=task["task_id"],
                role="assistant",
                content=final_content,
                metadata={
                    "output_file_id": final_output_file_id,
                    "outputs": final_outputs,
                },
            )
            await _try_set_title(
                settings,
                agent_router,
                session_id=session_id,
                needs_title=needs_title,
                user_message=payload.message,
                assistant_answer=final_content,
            )
            await cache_completed_turn(
                settings,
                task_manager,
                session_id=session_id,
                task=updated_task,
                messages=[
                    {"role": "user", "content": payload.message},
                    {"role": "assistant", "content": final_content},
                ],
            )
            yield SSEEventType.STATUS, {"message": "正在整理会话记忆"}
            compression = await maybe_compress_conversation(
                settings,
                task_manager,
                session_id,
                agent_router.generate_conversation_summary,
            )
            done_metadata = _context_metadata(
                window, source, resolved_file_id, file_source, parent_task_id
            )
            if compression.get("error"):
                done_metadata["summary_error"] = compression["error"]
                await _record_summary_error(
                    task_manager, task["task_id"], final_storage, compression["error"]
                )
            yield SSEEventType.DONE, {
                "success": True,
                "content": final_content,
                "storage": final_storage,
                "output_file_id": final_output_file_id,
                "outputs": final_outputs,
                "summary_compressed": bool(compression.get("compressed")),
                "metadata": done_metadata,
            }
        except Exception as exc:
            await task_manager.update_task(
                task["task_id"],
                status=TaskStatus.FAILED,
                error_message=str(exc),
                metadata={"error": serialize_exception(exc)},
            )
            await append_chat_turn_message(
                settings,
                session_id=session_id,
                task_id=task["task_id"],
                role="system",
                content=f"聊天任务失败：{exc}",
            )
            yield SSEEventType.ERROR, {"message": str(exc)}
        finally:
            await _stop_lock_heartbeat(task_manager, heartbeat)
            await task_manager.release_session_lock(session_id, owner)

    return EventSourceResponse(iter_sse(_event_stream()))


def _map_intent_to_task_type(intent: AgentIntent) -> TaskType:
    if intent == AgentIntent.SEARCH:
        return TaskType.SEARCH
    if intent == AgentIntent.EXCEL:
        return TaskType.EXCEL
    return TaskType.CHAT
