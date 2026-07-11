from __future__ import annotations

import hashlib
import json
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

    # Redis key 统一从这里生成，避免业务层散落拼接字符串。
    def build_task_status_key(self, task_id: str) -> str:
        return f"task:status:{task_id}"

    def build_task_events_key(self, task_id: str) -> str:
        return f"task:events:{task_id}"

    def build_session_memory_key(self, session_id: str) -> str:
        return f"session:memory:{session_id}"

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
        await self.redis_client.hset(key, mapping=payload)
        await self.redis_client.expire(key, self._get_status_ttl(task["status"]))

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

    # 会话短期记忆：只保留最近窗口，不替代 MySQL 历史记录。
    async def append_session_memory(
        self,
        session_id: str,
        *,
        role: str,
        content: str,
        message_type: str = "message",
    ) -> None:
        if self.redis_client is None:
            return

        key = self.build_session_memory_key(session_id)
        serialized = json.dumps(
            {
                "role": role,
                "content": content,
                "message_type": message_type,
                "created_at": datetime.utcnow().isoformat(),
            },
            ensure_ascii=False,
        )
        await self.redis_client.rpush(key, serialized)
        await self.redis_client.ltrim(
            key,
            -self.settings.redis_session_memory_max_length,
            -1,
        )
        await self.redis_client.expire(
            key,
            self.settings.redis_session_memory_ttl_seconds,
        )

    async def get_recent_session_memory(self, session_id: str, limit: int = 20) -> list[dict]:
        if self.redis_client is None:
            return []

        raw_messages = await self.redis_client.lrange(
            self.build_session_memory_key(session_id),
            -limit,
            -1,
        )
        return [json.loads(item) for item in raw_messages]

    # 分布式锁：同一会话同一时刻只允许一个重任务运行。
    async def acquire_session_lock(self, session_id: str, owner: str) -> bool:
        if self.redis_client is None:
            return True

        return bool(
            await self.redis_client.set(
                self.build_session_lock_key(session_id),
                owner,
                nx=True,
                ex=self.settings.redis_lock_ttl_seconds,
            )
        )

    async def release_session_lock(self, session_id: str, owner: str) -> bool:
        if self.redis_client is None:
            return True

        key = self.build_session_lock_key(session_id)
        current_owner = await self.redis_client.get(key)
        if current_owner != owner:
            return False

        await self.redis_client.delete(key)
        return True

    # 幂等保护：短时间内相同任务指纹只允许创建一次。
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

        return bool(
            await self.redis_client.set(
                self.build_idempotency_key(fingerprint),
                task_id,
                nx=True,
                ex=self.settings.redis_idempotency_ttl_seconds,
            )
        )

    async def get_idempotency_task_id(self, fingerprint: str) -> str | None:
        if self.redis_client is None:
            return None

        return await self.redis_client.get(self.build_idempotency_key(fingerprint))

    async def clear_idempotency(self, fingerprint: str) -> None:
        if self.redis_client is None:
            return

        await self.redis_client.delete(self.build_idempotency_key(fingerprint))

    # 查询接口优先读 Redis，Redis 不可用或未命中时再回退到本地内存。
    async def get_task_snapshot(self, task_id: str) -> dict:
        if self.redis_client is not None:
            payload = await self.redis_client.hgetall(self.build_task_status_key(task_id))
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
            async for key in self._iter_task_status_keys():
                payload = await self.redis_client.hgetall(key)
                if payload:
                    tasks.append(self._deserialize_task_payload(payload))
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
