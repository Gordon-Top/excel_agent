from fastapi import APIRouter, Depends

from app.api.dependencies import get_app_settings, get_task_manager
from app.core.config import Settings
from app.schemas.payloads import TaskDetailResponse, TaskListResponse, TaskRecord

router = APIRouter()


@router.get("", response_model=TaskListResponse)
async def list_tasks(
    settings: Settings = Depends(get_app_settings),
) -> TaskListResponse:
    # 任务查询优先读取 Redis 最新状态，保证接口能反映实时进度。
    task_manager = get_task_manager(settings)
    tasks = [TaskRecord(**task) for task in await task_manager.list_task_snapshots()]
    return TaskListResponse(tasks=tasks)


@router.get("/{task_id}", response_model=TaskDetailResponse)
async def get_task(
    task_id: str,
    settings: Settings = Depends(get_app_settings),
) -> TaskDetailResponse:
    # 单任务详情同样优先读 Redis，未命中时再回退本地状态。
    task_manager = get_task_manager(settings)
    task = TaskRecord(**(await task_manager.get_task_snapshot(task_id)))
    return TaskDetailResponse(task=task)
