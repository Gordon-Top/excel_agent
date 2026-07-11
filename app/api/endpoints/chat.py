from fastapi import APIRouter, Depends
from sse_starlette.sse import EventSourceResponse

from app.api.dependencies import (
    get_app_settings,
    get_file_manager,
    get_session_id,
    get_task_manager,
)
from app.constants.enums import (
    AgentIntent,
    ExecutionMode,
    SSEEventType,
    TaskStatus,
    TaskType,
)
from app.core.config import Settings
from app.core.exceptions import serialize_exception
from app.schemas.payloads import ChatRequest, ChatResponse
from app.services.agent.router import AgentRouter
from app.services.chat_history import (
    append_chat_turn_message as _append_chat_turn_message,
    ensure_chat_session as _ensure_chat_session,
    set_session_title as _set_session_title,
)
from app.utils.sse_formatter import iter_sse

router = APIRouter()


@router.post("/complete", response_model=ChatResponse)
async def complete_chat(
    payload: ChatRequest,
    settings: Settings = Depends(get_app_settings),
    session_id_header: str = Depends(get_session_id),
) -> ChatResponse:
    # 基础依赖与任务初始化
    file_manager = get_file_manager(settings)
    task_manager = get_task_manager(settings)
    agent_router = AgentRouter(settings, file_manager)
    session_id = payload.session_id or session_id_header
    intent = agent_router.detect_intent(
        message=payload.message,
        allow_search=payload.allow_search,
        file_id=payload.file_id,
    )

    needs_session_title = await _ensure_chat_session(settings, session_id)
    execution_mode = (
        payload.execution_mode
        or (ExecutionMode.E2B if intent == AgentIntent.EXCEL and settings.e2b_api_key else settings.execution_mode)
    )

    task = await task_manager.create_task(
        session_id=session_id,
        task_type=_map_intent_to_task_type(intent),
        instruction=payload.message,
        execution_mode=execution_mode,
        template_name=payload.template_name,
        input_file_id=payload.file_id,
    )
    await _append_chat_turn_message(
        settings,
        session_id=session_id,
        task_id=task["task_id"],
        role="user",
        content=payload.message,
    )
    await task_manager.append_session_memory(
        session_id,
        role="user",
        content=payload.message,
    )

    # 非流式接口也保留一份 Redis 任务事件，便于统一追踪。
    await task_manager.update_task(task["task_id"], status=TaskStatus.RUNNING)
    try:
        result = await agent_router.complete_chat(
            message=payload.message,
            allow_search=payload.allow_search,
            file_id=payload.file_id,
            template_name=payload.template_name,
            execution_mode=execution_mode,
            task_id=task["task_id"],
        )
        await task_manager.append_task_event(
            task["task_id"],
            SSEEventType.MESSAGE,
            {
                "content": result["content"],
                "citations": result["citations"],
                "metadata": result["metadata"],
            },
        )
        await task_manager.update_task(
            task["task_id"],
            status=TaskStatus.SUCCEEDED,
            result_summary=result["content"],
            metadata=result["storage"],
            output_file_id=result.get("output_file_id"),
        )
        await _append_chat_turn_message(
            settings,
            session_id=session_id,
            task_id=task["task_id"],
            role="assistant",
            content=result["content"],
        )
        if needs_session_title:
            generated_title = await agent_router.generate_session_title(
                user_message=payload.message,
                assistant_answer=result["content"],
            )
            await _set_session_title(
                settings,
                session_id=session_id,
                title=generated_title,
            )
        await task_manager.append_session_memory(
            session_id,
            role="assistant",
            content=result["content"],
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
            metadata=result["metadata"],
            output_file_id=result.get("output_file_id"),
            outputs=result.get("outputs", []),
        )
    except Exception as exc:
        error = serialize_exception(exc)
        await task_manager.update_task(
            task["task_id"],
            status=TaskStatus.FAILED,
            error_message=str(exc),
            metadata={
                "protocol": "excel_agent_execution" if intent == AgentIntent.EXCEL else "openai_chat_completions",
                "intent": str(intent),
                "context": {
                    "allow_search": payload.allow_search,
                    "template_name": payload.template_name,
                    "file_id": payload.file_id,
                    "execution_mode": str(execution_mode),
                },
                "error": error,
            },
        )
        await task_manager.append_task_event(
            task["task_id"], SSEEventType.ERROR, error
        )
        await _append_chat_turn_message(
            settings,
            session_id=session_id,
            task_id=task["task_id"],
            role="system",
            content=f"\u804a\u5929\u4efb\u52a1\u5931\u8d25\uff1a{exc}",
        )
        raise


@router.post("/stream")
async def stream_chat(
    payload: ChatRequest,
    settings: Settings = Depends(get_app_settings),
    session_id_header: str = Depends(get_session_id),
) -> EventSourceResponse:
    # 流式聊天入口会同时维护 MySQL 最终状态和 Redis 实时态。
    file_manager = get_file_manager(settings)
    task_manager = get_task_manager(settings)
    agent_router = AgentRouter(settings, file_manager)
    session_id = payload.session_id or session_id_header
    intent = agent_router.detect_intent(
        message=payload.message,
        allow_search=payload.allow_search,
        file_id=payload.file_id,
    )

    needs_session_title = await _ensure_chat_session(settings, session_id)
    execution_mode = (
        payload.execution_mode
        or (ExecutionMode.E2B if intent == AgentIntent.EXCEL and settings.e2b_api_key else settings.execution_mode)
    )

    task = await task_manager.create_task(
        session_id=session_id,
        task_type=_map_intent_to_task_type(intent),
        instruction=payload.message,
        execution_mode=execution_mode,
        template_name=payload.template_name,
        input_file_id=payload.file_id,
    )
    await task_manager.update_task(task["task_id"], status=TaskStatus.RUNNING)
    await _append_chat_turn_message(
        settings,
        session_id=session_id,
        task_id=task["task_id"],
        role="user",
        content=payload.message,
    )
    await task_manager.append_session_memory(
        session_id,
        role="user",
        content=payload.message,
    )
    await task_manager.append_task_event(
        task["task_id"],
        SSEEventType.STATUS,
        {"message": "聊天任务已开始"},
    )

    async def _event_stream():
        final_content = ""
        final_citations: list = []
        final_storage: dict = {}
        final_output_file_id: str | None = None
        pending_done_event: tuple[SSEEventType, dict] | None = None
        try:
            async for event_name, event_payload in agent_router.stream_chat(
                message=payload.message,
                allow_search=payload.allow_search,
                file_id=payload.file_id,
                template_name=payload.template_name,
                execution_mode=execution_mode,
                task_id=task["task_id"],
            ):
                await task_manager.append_task_event(
                    task["task_id"], event_name, event_payload
                )
                if event_name == SSEEventType.MESSAGE and isinstance(
                    event_payload, dict
                ):
                    final_content = str(event_payload.get("content", final_content))
                    final_citations = list(event_payload.get("citations", final_citations))
                if event_name == SSEEventType.DONE and isinstance(event_payload, dict):
                    final_storage = dict(event_payload.get("storage", final_storage))
                    final_output_file_id = event_payload.get("output_file_id")
                    pending_done_event = (event_name, event_payload)
                    continue
                yield event_name, event_payload

            await task_manager.update_task(
                task["task_id"],
                status=TaskStatus.SUCCEEDED,
                result_summary=final_content,
                output_file_id=final_output_file_id,
                metadata=final_storage
                or {
                    "protocol": "openai_chat_completions",
                    "intent": str(intent),
                    "context": {
                        "allow_search": payload.allow_search,
                        "template_name": payload.template_name,
                    },
                    "citations": final_citations,
                },
            )
            await _append_chat_turn_message(
                settings,
                session_id=session_id,
                task_id=task["task_id"],
                role="assistant",
                content=final_content,
            )
            if needs_session_title:
                generated_title = await agent_router.generate_session_title(
                    user_message=payload.message,
                    assistant_answer=final_content,
                )
                await _set_session_title(
                    settings,
                    session_id=session_id,
                    title=generated_title,
                )
            await task_manager.append_session_memory(
                session_id,
                role="assistant",
                content=final_content,
            )
            if pending_done_event is not None:
                yield pending_done_event
        except Exception as exc:
            await task_manager.update_task(
                task["task_id"],
                status=TaskStatus.FAILED,
                error_message=str(exc),
                metadata={
                    "protocol": "openai_chat_completions",
                    "intent": str(intent),
                    "context": {
                        "allow_search": payload.allow_search,
                        "template_name": payload.template_name,
                        "file_id": payload.file_id,
                    },
                    "error": serialize_exception(exc),
                },
            )
            await task_manager.append_task_event(
                task["task_id"],
                SSEEventType.ERROR,
                {"message": str(exc)},
            )
            await _append_chat_turn_message(
                settings,
                session_id=session_id,
                task_id=task["task_id"],
                role="system",
                content=f"聊天任务失败：{exc}",
            )
            raise

    return EventSourceResponse(iter_sse(_event_stream()))


def _map_intent_to_task_type(intent: AgentIntent) -> TaskType:
    if intent == AgentIntent.SEARCH:
        return TaskType.SEARCH
    if intent == AgentIntent.EXCEL:
        return TaskType.EXCEL
    return TaskType.CHAT
