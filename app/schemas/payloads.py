from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.constants.enums import ExecutionMode, FileRole, SSEEventType, TaskStatus, TaskType


class BaseResponse(BaseModel):
    success: bool = True
    message: str = "ok"


class HealthResponse(BaseResponse):
    app_name: str
    environment: str
    database_configured: bool
    redis_configured: bool
    model_configured: bool


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=10_000)
    session_id: str | None = None
    allow_search: bool = False
    template_name: str | None = None
    file_id: str | None = None
    execution_mode: ExecutionMode | None = None


class ChatResponse(BaseResponse):
    session_id: str
    task_id: str
    task_type: TaskType
    content: str
    citations: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    output_file_id: str | None = None
    outputs: list[dict[str, Any]] = Field(default_factory=list)


class FileUploadResponse(BaseResponse):
    file_id: str
    original_name: str
    stored_path: str
    size_bytes: int
    file_role: FileRole = FileRole.INPUT


class ExcelPreviewRequest(BaseModel):
    file_id: str


class ExcelPreviewResponse(BaseResponse):
    file_id: str
    file_summary: dict[str, Any]
    sheets: list[dict[str, Any]]
    detected_capabilities: list[str]


class ExcelProcessRequest(BaseModel):
    file_id: str
    instruction: str = Field(min_length=1, max_length=10_000)
    session_id: str | None = None
    template_name: str | None = None
    execution_mode: ExecutionMode | None = None
    allow_search: bool = False


class ExcelProcessResponse(BaseResponse):
    session_id: str
    task_id: str
    content: str
    output_file_id: str
    outputs: list[dict[str, Any]] = Field(default_factory=list)
    execution: dict[str, Any] = Field(default_factory=dict)
    preview: dict[str, Any] | None = None


class TaskRecord(BaseModel):
    task_id: str
    session_id: str
    task_type: TaskType
    status: TaskStatus
    instruction: str
    execution_mode: ExecutionMode
    template_name: str | None = None
    input_file_id: str | None = None
    output_file_id: str | None = None
    result_summary: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskDetailResponse(BaseResponse):
    task: TaskRecord


class TaskListResponse(BaseResponse):
    tasks: list[TaskRecord]


class SSEEvent(BaseModel):
    event: SSEEventType
    data: dict[str, Any] | list[Any] | str
