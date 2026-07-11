from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from app.api.router import api_router
from app.core.config import get_settings
from app.core.database import close_database, close_redis, init_database, init_redis
from app.core.exceptions import register_exception_handlers
from app.schemas.payloads import HealthResponse


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    await init_database(settings)
    await init_redis(settings)
    yield
    await close_redis()
    await close_database()


settings = get_settings()
app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
register_exception_handlers(app)
app.include_router(api_router, prefix=settings.api_prefix)


@app.get("/health", response_model=HealthResponse, tags=["system"])
async def health_check() -> HealthResponse:
    settings = get_settings()
    return HealthResponse(
        app_name=settings.app_name,
        environment=settings.app_env,
        database_configured=bool(settings.mysql_dsn),
        redis_configured=bool(settings.redis_url),
        model_configured=bool(settings.deepseek_api_key),
    )


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=8001, reload=True)
