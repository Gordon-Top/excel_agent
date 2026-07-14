from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from uuid import uuid4

from app.constants.enums import TaskStatus, TaskType
from app.core.config import Settings
from app.models.session import ChatMessage, ChatSession
from app.models.task import AgentTask
from app.services.tasks.task_manager import TaskManager

SummaryGenerator = Callable[[str | None, list[dict[str, Any]]], Awaitable[str]]


def _empty_context(session_id: str) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "summary": None,
        "summarized_turn_count": 0,
        "summarized_through_turn_id": None,
        "recent_turns": [],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _clean_messages(messages: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    cleaned: list[dict[str, str]] = []
    for item in messages or []:
        role = str(item.get("role", ""))
        content = str(item.get("content", "")).strip()
        if role in {"user", "assistant"} and content:
            cleaned.append({"role": role, "content": content})
    return cleaned


def _is_complete_turn(messages: list[dict[str, str]]) -> bool:
    return any(m["role"] == "user" for m in messages) and any(
        m["role"] == "assistant" for m in messages
    )


def _valid_context(payload: Any, session_id: str) -> bool:
    if not isinstance(payload, dict) or payload.get("session_id") != session_id:
        return False
    if payload.get("summary") is not None and not isinstance(payload.get("summary"), str):
        return False
    summarized_count = payload.get("summarized_turn_count", 0)
    if not isinstance(summarized_count, int) or summarized_count < 0:
        return False
    pointer = payload.get("summarized_through_turn_id")
    if pointer is not None and (not isinstance(pointer, int) or pointer < 0):
        return False
    turns = payload.get("recent_turns", [])
    if not isinstance(turns, list):
        return False
    for turn in turns:
        if not isinstance(turn, dict) or not isinstance(turn.get("turn_id"), int):
            return False
        if not isinstance(turn.get("task_id"), str) or not turn["task_id"]:
            return False
        messages = turn.get("messages")
        if not isinstance(messages, list) or not messages:
            return False
        for message in messages:
            if not isinstance(message, dict):
                return False
            if message.get("role") not in {"user", "assistant"}:
                return False
            if not isinstance(message.get("content"), str) or not message["content"].strip():
                return False
        if not _is_complete_turn(_clean_messages(messages)):
            return False
    return True


async def create_chat_session(
    settings: Settings, *, title: str | None = None, session_id: str | None = None
) -> dict[str, Any]:
    value = session_id or f"session-{uuid4().hex}"
    now = datetime.now(timezone.utc)
    clean_title = title.strip()[:255] if title and title.strip() else None
    if settings.mysql_dsn:
        session = await ChatSession.create(session_id=value, title=clean_title)
        return {"session_id": session.session_id, "title": session.title, "created_at": session.created_at}
    return {"session_id": value, "title": clean_title, "created_at": now}


async def ensure_chat_session(settings: Settings, session_id: str) -> bool:
    if not settings.mysql_dsn:
        return False
    session, created = await ChatSession.get_or_create(session_id=session_id, defaults={"title": None})
    if not created:
        await ChatSession.filter(session_id=session_id).update(updated_at=datetime.now(timezone.utc))
    return created or not bool(session.title)


async def set_session_title(settings: Settings, *, session_id: str, title: str) -> None:
    if not settings.mysql_dsn or not title.strip():
        return
    await ChatSession.filter(session_id=session_id).update(
        title=title.strip()[:255], updated_at=datetime.now(timezone.utc)
    )


async def append_chat_turn_message(
    settings: Settings, *, session_id: str, task_id: str, role: str, content: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    if not settings.mysql_dsn:
        return
    message: dict[str, Any] = {"role": role, "content": content}
    if metadata:
        message["metadata"] = metadata
    turn, created = await ChatMessage.get_or_create(
        task_id=task_id, defaults={"session_id": session_id, "message_json": [message]}
    )
    if not created:
        messages = list(turn.message_json or [])
        messages.append(message)
        await ChatMessage.filter(id=turn.id).update(
            message_json=messages, updated_at=datetime.now(timezone.utc)
        )
    await ChatSession.filter(session_id=session_id).update(updated_at=datetime.now(timezone.utc))


async def _load_mysql_context(settings: Settings, session_id: str) -> dict[str, Any]:
    session = await ChatSession.get_or_none(session_id=session_id)
    if session is None:
        return _empty_context(session_id)
    pointer = session.summarized_through_turn_id or 0
    successful = await AgentTask.filter(
        session_id=session_id, status=str(TaskStatus.SUCCEEDED)
    ).order_by("created_at")
    task_map = {item.task_id: item for item in successful}
    if not task_map:
        payload = _empty_context(session_id)
        payload.update({
            "summary": session.summary,
            "summarized_turn_count": session.summarized_turn_count or 0,
            "summarized_through_turn_id": session.summarized_through_turn_id,
        })
        return payload
    rows = await ChatMessage.filter(
        session_id=session_id, id__gt=pointer, task_id__in=list(task_map)
    ).order_by("id")
    turns: list[dict[str, Any]] = []
    for row in rows:
        messages = _clean_messages(row.message_json)
        if not _is_complete_turn(messages):
            continue
        task = task_map[row.task_id]
        turns.append({
            "turn_id": row.id,
            "task_id": row.task_id,
            "task_type": task.task_type,
            "messages": messages,
            "input_file_id": task.input_file_id,
            "output_file_id": task.output_file_id,
            "created_at": row.created_at.isoformat(),
        })
    return {
        "session_id": session_id,
        "summary": session.summary,
        "summarized_turn_count": session.summarized_turn_count or 0,
        "summarized_through_turn_id": session.summarized_through_turn_id,
        "recent_turns": turns,
        "updated_at": session.updated_at.isoformat(),
    }


async def load_conversation_context(
    settings: Settings, task_manager: TaskManager, session_id: str, *, bypass_cache: bool = False
) -> tuple[dict[str, Any], str]:
    cached: dict[str, Any] | None = None
    cache_source: str | None = None
    cache_state = "bypassed"
    if not bypass_cache:
        reader = getattr(task_manager, "read_session_context_cache", None)
        if reader is not None:
            cached, cache_source, cache_state = await reader(session_id)
        else:
            cached = await task_manager.get_session_context_cache(session_id)
            cache_source = "redis" if getattr(task_manager, "redis_client", None) is not None else "memory"
            cache_state = "legacy"

        # Redis 连接异常且 MySQL 可用时，memory 只是非权威旧副本，必须忽略。
        may_use_cached = not (settings.mysql_dsn and cache_state == "redis_error")
        if may_use_cached and _valid_context(cached, session_id):
            return cached, cache_source or "memory"
        if cached is not None and cache_state != "redis_error":
            await task_manager.invalidate_session_context(session_id)

    if settings.mysql_dsn:
        payload = await _load_mysql_context(settings, session_id)
        await task_manager.set_session_context_cache(session_id, payload)
        return payload, "mysql"

    # 无 MySQL 时，仅在 Redis 未配置或 Redis 连接异常时使用进程内副本。
    if cache_state in {"memory", "redis_error", "legacy"} and _valid_context(cached, session_id):
        return cached, "memory"
    return _empty_context(session_id), "memory"


def build_model_context(settings: Settings, context: dict[str, Any]) -> dict[str, Any]:
    selected: list[dict[str, Any]] = []
    message_count = 0
    char_count = 0
    truncated = False
    for turn in reversed(context.get("recent_turns", [])):
        messages = _clean_messages(turn.get("messages"))
        turn_chars = sum(len(item["content"]) for item in messages)
        if (
            message_count + len(messages) > settings.conversation_context_max_messages
            or char_count + turn_chars > settings.conversation_context_max_chars
        ):
            truncated = True
            break
        selected.append(turn)
        message_count += len(messages)
        char_count += turn_chars
    selected.reverse()
    flat = [message for turn in selected for message in _clean_messages(turn.get("messages"))]
    return {
        "summary": context.get("summary"),
        "messages": flat,
        "turns": selected,
        "context_message_count": len(flat),
        "context_turn_count": len(selected),
        "context_truncated": truncated or len(selected) < len(context.get("recent_turns", [])),
        "summary_applied": bool(context.get("summary")),
        "summary_turn_count": int(context.get("summarized_turn_count") or 0),
    }


async def cache_completed_turn(
    settings: Settings, task_manager: TaskManager, *, session_id: str, task: dict,
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    if settings.mysql_dsn:
        await task_manager.invalidate_session_context(session_id)
        payload, _ = await load_conversation_context(settings, task_manager, session_id, bypass_cache=True)
        return payload
    payload = await task_manager.get_session_context_cache(session_id) or _empty_context(session_id)
    clean = _clean_messages(messages)
    if _is_complete_turn(clean):
        recent = list(payload.get("recent_turns", []))
        recent.append({
            "turn_id": int(datetime.now(timezone.utc).timestamp() * 1_000_000),
            "task_id": task["task_id"], "task_type": str(task["task_type"]),
            "messages": clean, "input_file_id": task.get("input_file_id"),
            "output_file_id": task.get("output_file_id"),
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        payload["recent_turns"] = recent
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    await task_manager.set_session_context_cache(session_id, payload)
    return payload


async def maybe_compress_conversation(
    settings: Settings, task_manager: TaskManager, session_id: str,
    summary_generator: SummaryGenerator,
) -> dict[str, Any]:
    context, _ = await load_conversation_context(
        settings, task_manager, session_id, bypass_cache=bool(settings.mysql_dsn)
    )
    turns = list(context.get("recent_turns", []))
    if len(turns) <= settings.conversation_summary_trigger_turns:
        return {"compressed": False, "context": context}
    batch = turns[: settings.conversation_summary_batch_turns]
    try:
        summary = (await summary_generator(context.get("summary"), batch)).strip()
    except Exception as exc:
        return {"compressed": False, "context": context, "error": str(exc)}
    if not summary:
        return {"compressed": False, "context": context, "error": "empty summary"}
    summary = summary[: settings.conversation_summary_max_chars]
    last_turn_id = int(batch[-1]["turn_id"])
    count = int(context.get("summarized_turn_count") or 0) + len(batch)
    if settings.mysql_dsn:
        await ChatSession.filter(session_id=session_id).update(
            summary=summary, summarized_turn_count=count,
            summarized_through_turn_id=last_turn_id,
            summary_updated_at=datetime.now(timezone.utc),
        )
        await task_manager.invalidate_session_context(session_id)
        context, _ = await load_conversation_context(settings, task_manager, session_id, bypass_cache=True)
    else:
        context.update({
            "summary": summary, "summarized_turn_count": count,
            "summarized_through_turn_id": last_turn_id,
            "recent_turns": turns[len(batch):],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        await task_manager.set_session_context_cache(session_id, context)
    return {"compressed": True, "context": context, "compressed_turns": len(batch)}


async def list_chat_sessions(settings: Settings, *, offset: int, limit: int) -> tuple[list[dict], int]:
    if not settings.mysql_dsn:
        return [], 0
    total = await ChatSession.all().count()
    sessions = await ChatSession.all().order_by("-updated_at").offset(offset).limit(limit)
    result: list[dict] = []
    for session in sessions:
        tasks = await AgentTask.filter(session_id=session.session_id).order_by("-created_at")
        latest = tasks[0] if tasks else None
        turn_count = await ChatMessage.filter(session_id=session.session_id).count()
        latest_message = await ChatMessage.filter(session_id=session.session_id).order_by("-id").first()
        preview = None
        if latest_message:
            messages = _clean_messages(latest_message.message_json)
            if messages:
                preview = messages[-1]["content"][:200]
        result.append({
            "session_id": session.session_id, "title": session.title,
            "created_at": session.created_at, "updated_at": session.updated_at,
            "last_message_preview": preview,
            "last_task_type": TaskType(latest.task_type) if latest else None,
            "turn_count": turn_count,
        })
    return result, total


async def list_chat_turns(
    settings: Settings, *, session_id: str, offset: int, limit: int
) -> tuple[list[dict], int]:
    if not settings.mysql_dsn:
        return [], 0
    total = await ChatMessage.filter(session_id=session_id).count()
    rows = await ChatMessage.filter(session_id=session_id).order_by("id").offset(offset).limit(limit)
    tasks = await AgentTask.filter(task_id__in=[row.task_id for row in rows]) if rows else []
    task_map = {task.task_id: task for task in tasks}
    result = []
    for row in rows:
        task = task_map.get(row.task_id)
        if task is None:
            continue
        result.append({
            "turn_id": row.id, "task_id": row.task_id,
            "task_type": TaskType(task.task_type), "status": TaskStatus(task.status),
            "messages": list(row.message_json or []),
            "input_file_id": task.input_file_id, "output_file_id": task.output_file_id,
            "created_at": row.created_at, "updated_at": row.updated_at,
        })
    return result, total


async def get_excel_file_candidates(
    settings: Settings, session_id: str, *, context: dict[str, Any] | None = None,
    require_latest_success_excel: bool = False,
) -> list[dict[str, Any]]:
    """Return output/input candidates from the latest successful Excel turn only."""
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    if settings.mysql_dsn:
        if require_latest_success_excel:
            latest_success = await AgentTask.filter(
                session_id=session_id, status=str(TaskStatus.SUCCEEDED)
            ).order_by("-created_at").first()
            if latest_success is None or latest_success.task_type != str(TaskType.EXCEL):
                return []
        latest_excel = await AgentTask.filter(
            session_id=session_id,
            task_type=str(TaskType.EXCEL),
            status=str(TaskStatus.SUCCEEDED),
        ).order_by("-created_at").first()
        if latest_excel is None:
            return []
        for file_id, source in (
            (latest_excel.output_file_id, "previous_output"),
            (latest_excel.input_file_id, "previous_input"),
        ):
            if file_id and file_id not in seen:
                seen.add(file_id)
                candidates.append({
                    "file_id": file_id,
                    "file_source": source,
                    "parent_task_id": latest_excel.task_id,
                })
        return candidates

    turns = list((context or {}).get("recent_turns", []))
    if require_latest_success_excel and (
        not turns or str(turns[-1].get("task_type")) != str(TaskType.EXCEL)
    ):
        return []
    latest_excel = next(
        (turn for turn in reversed(turns) if str(turn.get("task_type")) == str(TaskType.EXCEL)),
        None,
    )
    if latest_excel is None:
        return []
    for field, source in (
        ("output_file_id", "previous_output"),
        ("input_file_id", "previous_input"),
    ):
        file_id = latest_excel.get(field)
        if file_id and file_id not in seen:
            seen.add(file_id)
            candidates.append({
                "file_id": file_id,
                "file_source": source,
                "parent_task_id": latest_excel.get("task_id"),
            })
    return candidates
