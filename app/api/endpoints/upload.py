from uuid import uuid4

from fastapi import APIRouter, Depends, File, UploadFile
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
    ensure_chat_session,
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
    execution_mode = payload.execution_mode or (
        ExecutionMode.E2B if settings.e2b_api_key else settings.execution_mode
    )
    task = await task_manager.create_task(
        session_id=session_id,
        task_type=TaskType.EXCEL,
        instruction=payload.instruction,
        execution_mode=execution_mode,
        template_name=payload.template_name,
        input_file_id=payload.file_id,
    )
    needs_session_title = await ensure_chat_session(settings, session_id)
    await append_chat_turn_message(
        settings,
        session_id=session_id,
        task_id=task["task_id"],
        role="user",
        content=payload.instruction,
        metadata={"task_type": "excel", "file_id": payload.file_id},
    )
    await task_manager.append_session_memory(
        session_id,
        role="user",
        content=payload.instruction,
    )
    await task_manager.update_task(task["task_id"], status=TaskStatus.RUNNING)
    final_result: dict | None = None
    try:
        async for event_name, event_payload in agent_router.process_excel(
            file_id=payload.file_id,
            instruction=payload.instruction,
            execution_mode=execution_mode,
            task_id=task["task_id"],
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
        await task_manager.update_task(
            task["task_id"],
            status=TaskStatus.SUCCEEDED,
            output_file_id=primary["file_id"],
            result_summary=content,
            metadata={"excel": final_result},
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
        if needs_session_title:
            generated_title = await agent_router.generate_session_title(
                user_message=payload.instruction,
                assistant_answer=content,
            )
            await set_session_title(settings, session_id=session_id, title=generated_title)
        await task_manager.append_session_memory(
            session_id,
            role="assistant",
            content=content,
        )
        return ExcelProcessResponse(
            session_id=session_id,
            task_id=task["task_id"],
            content=content,
            output_file_id=primary["file_id"],
            outputs=final_result["outputs"],
            execution=final_result["execution"],
            preview=final_result.get("output_preview"),
        )
    except Exception as exc:
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


@router.post("/process/stream")
async def process_file_stream(
    payload: ExcelProcessRequest,
    settings: Settings = Depends(get_app_settings),
    session_id_header: str = Depends(get_session_id),
) -> EventSourceResponse:
    # Excel 任务入口需要同时处理防重、分布式锁和实时事件。
    file_manager = get_file_manager(settings)
    task_manager = get_task_manager(settings)
    agent_router = AgentRouter(settings, file_manager)

    session_id = payload.session_id or session_id_header or f"session-{uuid4().hex}"
    execution_mode = payload.execution_mode or (
        ExecutionMode.E2B if settings.e2b_api_key else settings.execution_mode
    )
    task_id = task_manager.generate_task_id()
    fingerprint = task_manager.create_idempotency_fingerprint(
        session_id=session_id,
        task_type=TaskType.EXCEL,
        file_id=payload.file_id,
        instruction=payload.instruction,
    )

    try:
        if not await task_manager.acquire_session_lock(session_id, task_id):
            raise AppError(
                "当前会话已有 Excel 任务正在执行，请稍后再试",
                error_code="session_task_running",
                detail={"session_id": session_id},
            )

        if not await task_manager.reserve_idempotency(fingerprint, task_id):
            existing_task_id = await task_manager.get_idempotency_task_id(fingerprint)
            await task_manager.release_session_lock(session_id, task_id)
            raise AppError(
                "相同的 Excel 任务刚刚提交过，请稍后再试",
                error_code="duplicate_excel_task",
                detail={"task_id": existing_task_id},
            )

        task = await task_manager.create_task(
            session_id=session_id,
            task_type=TaskType.EXCEL,
            instruction=payload.instruction,
            execution_mode=execution_mode,
            template_name=payload.template_name,
            input_file_id=payload.file_id,
            task_id=task_id,
        )
        needs_session_title = await ensure_chat_session(settings, session_id)
        await append_chat_turn_message(
            settings,
            session_id=session_id,
            task_id=task["task_id"],
            role="user",
            content=payload.instruction,
            metadata={"task_type": "excel", "file_id": payload.file_id},
        )
        await task_manager.append_session_memory(
            session_id,
            role="user",
            content=payload.instruction,
        )
        await task_manager.update_task(task["task_id"], status=TaskStatus.RUNNING)
        await task_manager.append_task_event(
            task["task_id"],
            SSEEventType.STATUS,
            {"message": "Excel 任务已开始"},
        )
    except Exception:
        await task_manager.release_session_lock(session_id, task_id)
        raise

    async def _event_stream():
        output_file_id: str | None = None
        final_result: dict | None = None
        try:
            async for event_name, event_payload in agent_router.process_excel(
                file_id=payload.file_id,
                instruction=payload.instruction,
                execution_mode=execution_mode,
                task_id=task["task_id"],
            ):
                await task_manager.append_task_event(task["task_id"], event_name, event_payload)
                if (
                    event_name == SSEEventType.RESULT
                    and isinstance(event_payload, dict)
                    and event_payload.get("kind") == "execution_result"
                ):
                    output_file_id = event_payload["primary_output"]["file_id"]
                    final_result = event_payload
                yield event_name, event_payload

            final_content = (
                AgentRouter._build_excel_answer(final_result)
                if final_result
                else "Excel 任务处理完成"
            )
            await task_manager.update_task(
                task["task_id"],
                status=TaskStatus.SUCCEEDED,
                output_file_id=output_file_id,
                result_summary=final_content,
                metadata={"excel": final_result} if final_result else {},
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
                    "outputs": final_result.get("outputs", []) if final_result else [],
                },
            )
            if needs_session_title:
                generated_title = await agent_router.generate_session_title(
                    user_message=payload.instruction,
                    assistant_answer=final_content,
                )
                await set_session_title(
                    settings,
                    session_id=session_id,
                    title=generated_title,
                )
            await task_manager.append_session_memory(
                session_id,
                role="assistant",
                content=final_content,
            )
        except Exception as exc:
            await task_manager.update_task(
                task["task_id"],
                status=TaskStatus.FAILED,
                error_message=str(exc),
                metadata={"excel": {"error": serialize_exception(exc)}},
            )
            await task_manager.append_task_event(
                task["task_id"],
                SSEEventType.ERROR,
                {"message": str(exc)},
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
            await task_manager.release_session_lock(session_id, task["task_id"])

    return EventSourceResponse(iter_sse(_event_stream()))
