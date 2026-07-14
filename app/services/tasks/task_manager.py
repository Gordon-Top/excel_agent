from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import suppress
from collections.abc import AsyncIterator
from datetime import datetime
from uuid import uuid4

from redis.asyncio import Redis

from app.constants.enums import ExecutionMode, TaskStatus, TaskType
from app.core.config import Settings, get_settings
from app.core.exceptions import AppError
from app.models.task import AgentTask


class TaskManager:
    """统一封装任务状态、事件流、会话记忆、锁和幂等。"""

    def __init__(
        self,
        redis_client: Redis | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.redis_client = redis_client
        self.settings = settings or get_settings()
        self._tasks: dict[str, dict] = {}
        self._session_contexts: dict[str, dict] = {}
        self._invalidated_session_contexts: set[str] = set()
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._session_lock_owners: dict[str, str] = {}
        self._session_lock_backends: dict[str, str] = {}

    # Redis key 统一从这里生成，避免业务层散落拼接字符串。
    def build_task_status_key(self, task_id: str) -> str:
        return f"task:status:{task_id}"

    def build_task_events_key(self, task_id: str) -> str:
        return f"task:events:{task_id}"

    def build_session_memory_key(self, session_id: str) -> str:
        return f"session:memory:{session_id}"

    def build_session_context_key(self, session_id: str) -> str:
        return f"session:context:v2:{session_id}"

    def build_session_lock_key(self, session_id: str) -> str:
        return f"lock:session:{session_id}"

    def build_idempotency_key(self, fingerprint: str) -> str:
        return f"idempotency:task:{fingerprint}"

    def generate_task_id(self) -> str:
        return f"task-{uuid4().hex}"

    async def create_task(
        self,
        *,
        session_id: str,
        task_type: TaskType,
        instruction: str,
        execution_mode: ExecutionMode,
        template_name: str | None = None,
        input_file_id: str | None = None,
        task_id: str | None = None,
    ) -> dict:
        now = datetime.utcnow()
        task = {
            "task_id": task_id or self.generate_task_id(),
            "session_id": session_id,
            "task_type": task_type,
            "status": TaskStatus.PENDING,
            "instruction": instruction,
            "execution_mode": execution_mode,
            "template_name": template_name,
            "input_file_id": input_file_id,
            "output_file_id": None,
            "result_summary": None,
            "error_message": None,
            "created_at": now,
            "updated_at": now,
            "metadata": {},
        }
        self._tasks[task["task_id"]] = task
        await self._sync(task)
        await self._persist_task(task)
        return task

    async def update_task(self, task_id: str, **changes) -> dict:
        task = self.get_task(task_id)
        task.update(changes)
        task["updated_at"] = datetime.utcnow()
        await self._sync(task)
        await self._persist_task(task)
        return task

    def get_task(self, task_id: str) -> dict:
        if task_id not in self._tasks:
            raise AppError(
                "任务不存在",
                error_code="task_not_found",
                detail={"task_id": task_id},
            )
        return self._tasks[task_id]

    def list_tasks(self) -> list[dict]:
        return list(self._tasks.values())

    # 任务状态 Hash：保存任务最新状态，适合接口直接查询。
    async def _sync(self, task: dict) -> None:
        if self.redis_client is None:
            return

        payload = {
            "task_id": task["task_id"],
            "session_id": task["session_id"],
            "task_type": str(task["task_type"]),
            "status": str(task["status"]),
            "instruction": task["instruction"],
            "execution_mode": str(task["execution_mode"]),
            "template_name": task["template_name"] or "",
            "input_file_id": task["input_file_id"] or "",
            "output_file_id": task["output_file_id"] or "",
            "result_summary": task["result_summary"] or "",
            "error_message": task["error_message"] or "",
            "created_at": task["created_at"].isoformat(),
            "updated_at": task["updated_at"].isoformat(),
            "metadata": json.dumps(task.get("metadata", {}), ensure_ascii=False, default=str),
        }
        key = self.build_task_status_key(task["task_id"])
        try:
            await self.redis_client.hset(key, mapping=payload)
            await self.redis_client.expire(key, self._get_status_ttl(task["status"]))
        except Exception:
            # Redis is a cache/status layer; MySQL or process memory remains authoritative.
            return

    # 任务事件 List：保存标准事件，用于 SSE 重连恢复和短期追踪。
    async def append_task_event(
        self,
        task_id: str,
        event_name: str,
        payload: dict | list | str,
    ) -> None:
        if self.redis_client is None:
            return

        serialized = json.dumps(
            {
                "event": str(event_name),
                "data": payload,
                "created_at": datetime.utcnow().isoformat(),
            },
            ensure_ascii=False,
            default=str,
        )
        key = self.build_task_events_key(task_id)
        try:
            await self.redis_client.rpush(key, serialized)
            await self.redis_client.ltrim(
                key,
                -self.settings.redis_task_events_max_length,
                -1,
            )
            await self.redis_client.expire(key, self.settings.redis_task_events_ttl_seconds)

            if isinstance(payload, dict) and "message" in payload:
                await self.redis_client.hset(
                    self.build_task_status_key(task_id),
                    mapping={
                        "progress_message": str(payload["message"]),
                        "updated_at": datetime.utcnow().isoformat(),
                    },
                )
        except Exception:
            return

    # Legacy per-message methods remain for compatibility. New code caches full turns.
    async def append_session_memory(
        self, session_id: str, *, role: str, content: str, message_type: str = "message"
    ) -> None:
        context = self._session_contexts.setdefault(
            session_id,
            {"session_id": session_id, "summary": None, "summarized_turn_count": 0,
             "summarized_through_turn_id": None, "recent_turns": [], "legacy_messages": []},
        )
        context.setdefault("legacy_messages", []).append(
            {"role": role, "content": content, "message_type": message_type,
             "created_at": datetime.utcnow().isoformat()}
        )
        context["legacy_messages"] = context["legacy_messages"][-self.settings.redis_session_memory_max_length:]

    async def get_recent_session_memory(self, session_id: str, limit: int = 20) -> list[dict]:
        context = self._session_contexts.get(session_id, {})
        return list(context.get("legacy_messages", []))[-limit:]

    async def read_session_context_cache(
        self, session_id: str
    ) -> tuple[dict | None, str | None, str]:
        """读取会话上下文，并区分 Redis miss、损坏和连接异常。

        Redis 已配置时，普通 miss/过期绝不能回退到进程内旧副本，否则会
        绕过 MySQL 权威数据源。只有 Redis 本身不可用时，调用方才能决定
        是否在无 MySQL 环境中采用 memory 副本。
        """
        if self.redis_client is not None:
            if session_id in self._invalidated_session_contexts:
                return None, None, "redis_invalidated"
            key = self.build_session_context_key(session_id)
            try:
                raw = await self.redis_client.get(key)
            except Exception:
                payload = self._session_contexts.get(session_id)
                return (dict(payload) if payload else None), "memory", "redis_error"
            if not raw:
                return None, None, "redis_miss"
            try:
                payload = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                with suppress(Exception):
                    await self.redis_client.delete(key)
                return None, None, "redis_invalid"
            if not isinstance(payload, dict) or payload.get("session_id") != session_id:
                with suppress(Exception):
                    await self.redis_client.delete(key)
                return None, None, "redis_invalid"
            return payload, "redis", "redis_hit"

        payload = self._session_contexts.get(session_id)
        return (dict(payload) if payload else None), "memory", "memory"

    async def get_session_context_cache(self, session_id: str) -> dict | None:
        payload, _, state = await self.read_session_context_cache(session_id)
        if state == "redis_error":
            return None
        return payload

    async def set_session_context_cache(self, session_id: str, payload: dict) -> None:
        document = dict(payload)
        document["session_id"] = session_id
        self._session_contexts[session_id] = document
        if self.redis_client is not None:
            try:
                await self.redis_client.set(
                    self.build_session_context_key(session_id),
                    json.dumps(document, ensure_ascii=False, default=str),
                    ex=self.settings.redis_session_memory_ttl_seconds,
                )
                self._invalidated_session_contexts.discard(session_id)
            except Exception:
                pass

    async def invalidate_session_context(self, session_id: str) -> None:
        self._session_contexts.pop(session_id, None)
        if self.redis_client is not None:
            self._invalidated_session_contexts.add(session_id)
            try:
                await self.redis_client.delete(self.build_session_context_key(session_id))
            except Exception:
                pass

    async def acquire_session_lock(self, session_id: str, owner: str) -> bool:
        if self.redis_client is not None:
            try:
                acquired = bool(await self.redis_client.set(
                    self.build_session_lock_key(session_id), owner, nx=True,
                    ex=self.settings.redis_lock_ttl_seconds,
                ))
            except Exception:
                acquired = None
            if acquired is not None:
                if acquired:
                    self._session_lock_backends[session_id] = "redis"
                    self._session_lock_owners[session_id] = owner
                return acquired

        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        if lock.locked():
            return False
        await lock.acquire()
        self._session_lock_backends[session_id] = "memory"
        self._session_lock_owners[session_id] = owner
        return True

    async def refresh_session_lock(self, session_id: str, owner: str) -> bool:
        if self._session_lock_owners.get(session_id) != owner:
            return False
        backend = self._session_lock_backends.get(session_id)
        if backend == "memory":
            return True
        if backend != "redis" or self.redis_client is None:
            return False
        try:
            result = await self.redis_client.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end",
                1,
                self.build_session_lock_key(session_id),
                owner,
                self.settings.redis_lock_ttl_seconds,
            )
            return bool(result)
        except Exception:
            return False

    def start_session_lock_heartbeat(self, session_id: str, owner: str) -> asyncio.Task:
        async def _heartbeat() -> None:
            interval = max(1.0, float(self.settings.redis_lock_ttl_seconds) / 3.0)
            while True:
                await asyncio.sleep(interval)
                if not await self.refresh_session_lock(session_id, owner):
                    return

        return asyncio.create_task(_heartbeat())

    @staticmethod
    async def stop_session_lock_heartbeat(task: asyncio.Task | None) -> None:
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def release_session_lock(self, session_id: str, owner: str) -> bool:
        if self._session_lock_owners.get(session_id) != owner:
            return False

        backend = self._session_lock_backends.get(session_id)
        if backend == "redis":
            if self.redis_client is None:
                return False
            try:
                result = await self.redis_client.eval(
                    "if redis.call('get', KEYS[1]) == ARGV[1] then "
                    "return redis.call('del', KEYS[1]) else return 0 end",
                    1,
                    self.build_session_lock_key(session_id),
                    owner,
                )
            except Exception:
                return False
            if not result:
                return False
        elif backend == "memory":
            lock = self._session_locks.get(session_id)
            if lock and lock.locked():
                lock.release()
        else:
            return False

        self._session_lock_owners.pop(session_id, None)
        self._session_lock_backends.pop(session_id, None)
        return True

    # Idempotency protection.
    def create_idempotency_fingerprint(
        self,
        *,
        session_id: str,
        task_type: TaskType,
        file_id: str | None,
        instruction: str,
    ) -> str:
        payload = "::".join(
            [
                session_id,
                str(task_type),
                file_id or "",
                instruction.strip(),
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    async def reserve_idempotency(self, fingerprint: str, task_id: str) -> bool:
        if self.redis_client is None:
            return True
        try:
            return bool(
                await self.redis_client.set(
                    self.build_idempotency_key(fingerprint),
                    task_id,
                    nx=True,
                    ex=self.settings.redis_idempotency_ttl_seconds,
                )
            )
        except Exception:
            return True

    async def get_idempotency_task_id(self, fingerprint: str) -> str | None:
        if self.redis_client is None:
            return None
        try:
            return await self.redis_client.get(self.build_idempotency_key(fingerprint))
        except Exception:
            return None

    async def clear_idempotency(self, fingerprint: str) -> None:
        if self.redis_client is None:
            return
        with suppress(Exception):
            await self.redis_client.delete(self.build_idempotency_key(fingerprint))

    # 查询接口优先读 Redis，Redis 不可用或未命中时再回退到本地内存。
    async def get_task_snapshot(self, task_id: str) -> dict:
        if self.redis_client is not None:
            try:
                payload = await self.redis_client.hgetall(self.build_task_status_key(task_id))
            except Exception:
                payload = {}
            if payload:
                return self._deserialize_task_payload(payload)

        if self.settings.mysql_dsn:
            db_task = await AgentTask.get_or_none(task_id=task_id)
            if db_task is not None:
                task = self._deserialize_db_task(db_task)
                self._tasks[task_id] = task
                return task

        return self.get_task(task_id)

    async def list_task_snapshots(self) -> list[dict]:
        if self.redis_client is not None:
            tasks: list[dict] = []
            try:
                async for key in self._iter_task_status_keys():
                    payload = await self.redis_client.hgetall(key)
                    if payload:
                        tasks.append(self._deserialize_task_payload(payload))
            except Exception:
                tasks = []
            if tasks:
                return sorted(tasks, key=lambda item: item["updated_at"], reverse=True)

        if self.settings.mysql_dsn:
            db_tasks = await AgentTask.all().order_by("-updated_at")
            if db_tasks:
                tasks = [self._deserialize_db_task(item) for item in db_tasks]
                for task in tasks:
                    self._tasks[task["task_id"]] = task
                return tasks

        return sorted(
            self.list_tasks(),
            key=lambda item: item["updated_at"],
            reverse=True,
        )

    async def _iter_task_status_keys(self) -> AsyncIterator[str]:
        if self.redis_client is None:
            return

        async for key in self.redis_client.scan_iter(match=self.build_task_status_key("*")):
            yield str(key)

    def _deserialize_task_payload(self, payload: dict[str, str]) -> dict:
        metadata_raw = payload.get("metadata") or "{}"
        try:
            metadata = json.loads(metadata_raw)
        except json.JSONDecodeError:
            metadata = {}

        return {
            "task_id": payload["task_id"],
            "session_id": payload["session_id"],
            "task_type": TaskType(payload["task_type"]),
            "status": TaskStatus(payload["status"]),
            "instruction": payload["instruction"],
            "execution_mode": ExecutionMode(payload["execution_mode"]),
            "template_name": payload.get("template_name") or None,
            "input_file_id": payload.get("input_file_id") or None,
            "output_file_id": payload.get("output_file_id") or None,
            "result_summary": payload.get("result_summary") or None,
            "error_message": payload.get("error_message") or None,
            "created_at": datetime.fromisoformat(payload["created_at"]),
            "updated_at": datetime.fromisoformat(payload["updated_at"]),
            "metadata": metadata,
        }

    async def _persist_task(self, task: dict) -> None:
        if not self.settings.mysql_dsn:
            return

        defaults = {
            "session_id": task["session_id"],
            "task_type": str(task["task_type"]),
            "status": str(task["status"]),
            "instruction": task["instruction"],
            "execution_mode": str(task["execution_mode"]),
            "template_name": task["template_name"],
            "input_file_id": task["input_file_id"],
            "output_file_id": task["output_file_id"],
            "result_summary": task["result_summary"],
            "error_message": task["error_message"],
            "metadata_json": task.get("metadata", {}),
        }
        db_task, created = await AgentTask.get_or_create(
            task_id=task["task_id"],
            defaults=defaults,
        )
        if created:
            return

        await AgentTask.filter(task_id=task["task_id"]).update(**defaults)

    @staticmethod
    def _deserialize_db_task(task: AgentTask) -> dict:
        return {
            "task_id": task.task_id,
            "session_id": task.session_id,
            "task_type": TaskType(task.task_type),
            "status": TaskStatus(task.status),
            "instruction": task.instruction,
            "execution_mode": ExecutionMode(task.execution_mode),
            "template_name": task.template_name,
            "input_file_id": task.input_file_id,
            "output_file_id": task.output_file_id,
            "result_summary": task.result_summary,
            "error_message": task.error_message,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
            "metadata": task.metadata_json or {},
        }

    def _get_status_ttl(self, status: TaskStatus | str) -> int:
        if status == TaskStatus.FAILED:
            return self.settings.redis_task_status_ttl_failed_seconds
        if status == TaskStatus.SUCCEEDED:
            return self.settings.redis_task_status_ttl_success_seconds
        return self.settings.redis_task_status_ttl_active_seconds
