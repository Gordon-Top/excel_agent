from collections.abc import AsyncIterator, Iterable
from typing import Any

import orjson

from app.constants.enums import SSEEventType


def format_sse_event(
    event: SSEEventType | str,
    data: dict[str, Any] | list[Any] | str,
) -> str:
    payload = data if isinstance(data, str) else orjson.dumps(data).decode("utf-8")
    return f"event: {event}\ndata: {payload}\n\n"


async def iter_sse(events: AsyncIterator[tuple[SSEEventType | str, Any]]) -> AsyncIterator[str]:
    async for event_name, payload in events:
        yield format_sse_event(event_name, payload)


def build_done_event(payload: dict[str, Any] | None = None) -> str:
    return format_sse_event(SSEEventType.DONE, payload or {"success": True})


def build_error_event(message: str, detail: dict[str, Any] | None = None) -> str:
    return format_sse_event(
        SSEEventType.ERROR,
        {"success": False, "message": message, "detail": detail or {}},
    )


def serialize_event_batch(
    events: Iterable[tuple[SSEEventType | str, dict[str, Any] | str]]
) -> list[str]:
    return [format_sse_event(event_name, payload) for event_name, payload in events]
