from functools import lru_cache
from datetime import timedelta
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.constants.enums import ExecutionMode


BASE_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    # 基础运行配置
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Excel Agent"
    app_env: str = "development"
    debug: bool = True
    api_prefix: str = "/api"
    cors_allow_origins: list[str] = Field(default_factory=lambda: ["*"])

    mysql_dsn: str | None = None
    redis_url: str | None = None

    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    default_model: str = "deepseek-chat"

    execution_mode: ExecutionMode = ExecutionMode.LOCAL
    e2b_api_key: str | None = None

    upload_dir: Path = BASE_DIR / "storage" / "uploads"
    output_dir: Path = BASE_DIR / "storage" / "outputs"
    temp_dir: Path = BASE_DIR / "storage" / "temp"

    search_provider: str = "http"
    tavily_api_key: str | None = None
    search_max_queries: int = 3
    search_max_citations: int = 10
    tavily_max_results_per_query: int = 5

    # 本地执行与上传限制
    local_exec_timeout_seconds: int = 60
    e2b_exec_timeout_seconds: int = 120
    excel_code_repair_attempts: int = 2
    max_upload_size_mb: int = 20

    # Redis 状态层配置
    redis_task_status_ttl_active_seconds: int = 86_400
    redis_task_status_ttl_success_seconds: int = 86_400
    redis_task_status_ttl_failed_seconds: int = 259_200
    redis_task_events_ttl_seconds: int = 86_400
    redis_task_events_max_length: int = 200
    redis_session_memory_ttl_seconds: int = 86_400
    redis_session_memory_max_length: int = 20
    redis_lock_ttl_seconds: int = 120
    redis_idempotency_ttl_seconds: int = 600

    def ensure_storage_dirs(self) -> None:
        for directory in (self.upload_dir, self.output_dir, self.temp_dir):
            directory.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_storage_dirs()
    return settings
