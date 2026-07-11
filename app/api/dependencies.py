from uuid import uuid4

from fastapi import Header
from redis.asyncio import Redis

from app.core.config import Settings, get_settings
from app.core.database import get_redis
from app.services.files.file_manager import FileManager
from app.services.tasks.task_manager import TaskManager


def get_app_settings() -> Settings:
    return get_settings()


def get_session_id(x_session_id: str | None = Header(default=None)) -> str:
    return x_session_id or f"session-{uuid4().hex}"


def get_redis_client() -> Redis | None:
    return get_redis()


_FILE_MANAGERS: dict[str, FileManager] = {}
_TASK_MANAGERS: dict[str, TaskManager] = {}


def get_file_manager(settings: Settings | None = None) -> FileManager:
    app_settings = settings or get_settings()
    key = str(app_settings.upload_dir)
    if key not in _FILE_MANAGERS:
        _FILE_MANAGERS[key] = FileManager(app_settings)
    return _FILE_MANAGERS[key]


def get_task_manager(
    settings: Settings | None = None,
    redis_client: Redis | None = None,
) -> TaskManager:
    app_settings = settings or get_settings()
    key = str(app_settings.upload_dir)
    if key not in _TASK_MANAGERS:
        _TASK_MANAGERS[key] = TaskManager(
            redis_client if redis_client is not None else get_redis(),
            app_settings,
        )
    return _TASK_MANAGERS[key]
