from redis.asyncio import Redis
from tortoise import Tortoise

from app.core.config import Settings, get_settings


settings = get_settings()


TORTOISE_ORM = {
    "connections": {
        "default": settings.mysql_dsn,
    },
    "apps": {
        "models": {
            "models": [
                "app.models.session",
                "app.models.task",
                "aerich.models",
            ],
            "default_connection": "default",
        },
    },
}


redis_client: Redis | None = None


async def init_database(settings: Settings) -> None:
    if not settings.mysql_dsn:
        return

    # 当前 Tortoise 版本在 FastAPI lifespan 场景下默认不做跨任务上下文回退，
    # 请求处理时会拿不到 ORM context。这里显式开启 global fallback，
    # 让路由里的 ORM 查询能稳定访问到已初始化的连接上下文。
    await Tortoise.init(config=TORTOISE_ORM, _enable_global_fallback=True)


async def close_database() -> None:
    await Tortoise.close_connections()


async def init_redis(settings: Settings) -> Redis | None:
    global redis_client
    if not settings.redis_url:
        redis_client = None
        return None

    redis_client = Redis.from_url(settings.redis_url, decode_responses=True, protocol=2,)
    await redis_client.ping()
    return redis_client


async def close_redis() -> None:
    global redis_client
    if redis_client is not None:
        await redis_client.aclose()
        redis_client = None


def get_redis() -> Redis | None:
    return redis_client
