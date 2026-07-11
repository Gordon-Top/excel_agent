from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.core.config import Settings
from app.models.session import ChatMessage, ChatSession


async def ensure_chat_session(settings: Settings, session_id: str) -> bool:
    """Ensure a session exists and return whether it still needs a model title."""
    if not settings.mysql_dsn:
        return False

    session, created = await ChatSession.get_or_create(
        session_id=session_id,
        defaults={"title": None},
    )
    if not created:
        await ChatSession.filter(session_id=session_id).update(
            updated_at=datetime.now(timezone.utc)
        )
    return created or not bool(session.title)


async def set_session_title(
    settings: Settings,
    *,
    session_id: str,
    title: str,
) -> None:
    if not settings.mysql_dsn or not title.strip():
        return

    await ChatSession.filter(session_id=session_id).update(
        title=title.strip()[:255],
        updated_at=datetime.now(timezone.utc),
    )


async def append_chat_turn_message(
    settings: Settings,
    *,
    session_id: str,
    task_id: str,
    role: str,
    content: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Append one message to the single chat_messages row owned by a task."""
    if not settings.mysql_dsn:
        return

    message: dict[str, Any] = {"role": role, "content": content}
    if metadata:
        message["metadata"] = metadata

    turn, created = await ChatMessage.get_or_create(
        task_id=task_id,
        defaults={
            "session_id": session_id,
            "message_json": [message],
        },
    )
    if not created:
        messages = list(turn.message_json or [])
        messages.append(message)
        await ChatMessage.filter(id=turn.id).update(
            message_json=messages,
            updated_at=datetime.now(timezone.utc),
        )

    await ChatSession.filter(session_id=session_id).update(
        updated_at=datetime.now(timezone.utc)
    )
