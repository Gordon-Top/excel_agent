from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from app.api.endpoints.upload import _resolve_excel_file
from app.constants.enums import ExecutionMode, TaskType
from app.core.config import Settings
from app.services.agent.router import AgentRouter
from app.services.chat_history import (
    build_model_context,
    create_chat_session,
    get_excel_file_candidates,
    load_conversation_context,
    maybe_compress_conversation,
)
from app.services.tasks.task_manager import TaskManager


class FakeRedis:
    def __init__(self, value=None, error: Exception | None = None) -> None:
        self.value = value
        self.error = error
        self.deleted: list[str] = []
        self.set_calls: list[tuple] = []

    async def get(self, key):
        if self.error:
            raise self.error
        return self.value

    async def delete(self, key):
        self.deleted.append(key)
        self.value = None
        return 1

    async def set(self, key, value, **kwargs):
        self.set_calls.append((key, value, kwargs))
        self.value = value
        return True


class RecoveringLockRedis(FakeRedis):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next_set = True
        self.eval_calls: list[tuple] = []

    async def set(self, key, value, **kwargs):
        if self.fail_next_set:
            self.fail_next_set = False
            raise ConnectionError("redis unavailable")
        return await super().set(key, value, **kwargs)

    async def eval(self, script, *args):
        self.eval_calls.append((script, args))
        if "del" in script:
            self.value = None
        return 1


class ConversationContextTests(unittest.IsolatedAsyncioTestCase):
    def make_settings(self, **overrides) -> Settings:
        values = {
            "mysql_dsn": None,
            "redis_url": None,
            "deepseek_api_key": "test",
            "conversation_context_max_messages": 20,
            "conversation_context_max_chars": 24000,
            "conversation_summary_trigger_turns": 20,
            "conversation_summary_batch_turns": 10,
            "conversation_summary_max_chars": 4000,
        }
        values.update(overrides)
        return Settings(**values)

    @staticmethod
    def context(turn_count: int, session_id: str = "session-1") -> dict:
        return {
            "session_id": session_id,
            "summary": None,
            "summarized_turn_count": 0,
            "summarized_through_turn_id": None,
            "recent_turns": [
                {
                    "turn_id": index,
                    "task_id": f"task-{index}",
                    "task_type": "chat",
                    "messages": [
                        {"role": "user", "content": f"question-{index}"},
                        {"role": "assistant", "content": f"answer-{index}"},
                    ],
                }
                for index in range(1, turn_count + 1)
            ],
            "updated_at": "2026-07-13T00:00:00+00:00",
        }

    async def test_redis_miss_does_not_return_process_memory_copy(self) -> None:
        settings = self.make_settings()
        redis = FakeRedis(value=None)
        manager = TaskManager(redis, settings)
        manager._session_contexts["session-1"] = self.context(1)

        payload = await manager.get_session_context_cache("session-1")
        detailed, source, state = await manager.read_session_context_cache("session-1")

        self.assertIsNone(payload)
        self.assertIsNone(detailed)
        self.assertIsNone(source)
        self.assertEqual(state, "redis_miss")

    async def test_corrupted_redis_document_is_deleted(self) -> None:
        settings = self.make_settings()
        redis = FakeRedis(value="{broken-json")
        manager = TaskManager(redis, settings)

        payload, source, state = await manager.read_session_context_cache("session-1")

        self.assertIsNone(payload)
        self.assertIsNone(source)
        self.assertEqual(state, "redis_invalid")
        self.assertEqual(redis.deleted, ["session:context:v2:session-1"])

    async def test_redis_error_with_mysql_ignores_memory_and_recovers_from_mysql(self) -> None:
        settings = SimpleNamespace(mysql_dsn="mysql://configured")
        old = self.context(1)
        mysql_payload = self.context(2)
        manager = SimpleNamespace(
            read_session_context_cache=AsyncMock(return_value=(old, "memory", "redis_error")),
            set_session_context_cache=AsyncMock(),
            invalidate_session_context=AsyncMock(),
        )

        with patch(
            "app.services.chat_history._load_mysql_context",
            new=AsyncMock(return_value=mysql_payload),
        ) as load_mysql:
            payload, source = await load_conversation_context(
                settings, manager, "session-1"
            )

        self.assertEqual(payload, mysql_payload)
        self.assertEqual(source, "mysql")
        load_mysql.assert_awaited_once_with(settings, "session-1")
        manager.set_session_context_cache.assert_awaited_once_with(
            "session-1", mysql_payload
        )

    def test_window_selects_complete_turns_and_never_splits_pair(self) -> None:
        settings = self.make_settings(conversation_context_max_messages=3)
        window = build_model_context(settings, self.context(3))

        self.assertEqual(window["context_turn_count"], 1)
        self.assertEqual(window["context_message_count"], 2)
        self.assertEqual(
            [item["content"] for item in window["messages"]],
            ["question-3", "answer-3"],
        )
        self.assertTrue(window["context_truncated"])

    async def test_twenty_turns_do_not_compress_but_twenty_one_compress_oldest_ten(self) -> None:
        settings = self.make_settings()
        manager = TaskManager(None, settings)
        await manager.set_session_context_cache("session-1", self.context(20))
        generator = AsyncMock(return_value="rolling summary")

        first = await maybe_compress_conversation(
            settings, manager, "session-1", generator
        )
        self.assertFalse(first["compressed"])
        generator.assert_not_awaited()

        await manager.set_session_context_cache("session-1", self.context(21))
        second = await maybe_compress_conversation(
            settings, manager, "session-1", generator
        )

        self.assertTrue(second["compressed"])
        self.assertEqual(second["compressed_turns"], 10)
        self.assertEqual(second["context"]["summarized_turn_count"], 10)
        self.assertEqual(second["context"]["summarized_through_turn_id"], 10)
        self.assertEqual(len(second["context"]["recent_turns"]), 11)
        self.assertEqual(second["context"]["recent_turns"][0]["turn_id"], 11)

    async def test_summary_failure_keeps_cursor_and_original_turns(self) -> None:
        settings = self.make_settings()
        manager = TaskManager(None, settings)
        original = self.context(21)
        await manager.set_session_context_cache("session-1", deepcopy(original))
        generator = AsyncMock(side_effect=RuntimeError("summary unavailable"))

        result = await maybe_compress_conversation(
            settings, manager, "session-1", generator
        )
        cached = await manager.get_session_context_cache("session-1")

        self.assertFalse(result["compressed"])
        self.assertIn("summary unavailable", result["error"])
        self.assertEqual(cached["summarized_through_turn_id"], None)
        self.assertEqual(len(cached["recent_turns"]), 21)

    async def test_memory_excel_candidates_require_latest_turn_to_be_excel(self) -> None:
        settings = self.make_settings()
        context = self.context(0)
        context["recent_turns"] = [
            {
                "turn_id": 1,
                "task_id": "excel-1",
                "task_type": str(TaskType.EXCEL),
                "input_file_id": "file-input",
                "output_file_id": "file-output",
                "messages": [
                    {"role": "user", "content": "clean"},
                    {"role": "assistant", "content": "done"},
                ],
            },
            {
                "turn_id": 2,
                "task_id": "chat-2",
                "task_type": str(TaskType.CHAT),
                "messages": [
                    {"role": "user", "content": "tell a joke"},
                    {"role": "assistant", "content": "joke"},
                ],
            },
        ]

        guarded = await get_excel_file_candidates(
            settings,
            "session-1",
            context=context,
            require_latest_success_excel=True,
        )
        unguarded = await get_excel_file_candidates(
            settings,
            "session-1",
            context=context,
            require_latest_success_excel=False,
        )

        self.assertEqual(guarded, [])
        self.assertEqual(
            [item["file_id"] for item in unguarded],
            ["file-output", "file-input"],
        )

    async def test_memory_lock_is_released_after_redis_recovers(self) -> None:
        settings = self.make_settings()
        redis = RecoveringLockRedis()
        manager = TaskManager(redis, settings)

        acquired = await manager.acquire_session_lock("session-1", "memory-owner")

        self.assertTrue(acquired)
        self.assertEqual(manager._session_lock_backends["session-1"], "memory")
        self.assertTrue(
            await manager.refresh_session_lock("session-1", "memory-owner")
        )
        self.assertEqual(redis.eval_calls, [])

        released = await manager.release_session_lock("session-1", "memory-owner")

        self.assertTrue(released)
        self.assertFalse(manager._session_locks["session-1"].locked())
        self.assertNotIn("session-1", manager._session_lock_backends)
        self.assertEqual(redis.eval_calls, [])

        self.assertTrue(
            await manager.acquire_session_lock("session-1", "redis-owner")
        )
        self.assertEqual(manager._session_lock_backends["session-1"], "redis")
        self.assertTrue(
            await manager.release_session_lock("session-1", "redis-owner")
        )
        self.assertEqual(len(redis.eval_calls), 1)

    async def test_next_summary_merges_previous_summary_and_next_ten_turns(self) -> None:
        settings = self.make_settings()
        manager = TaskManager(None, settings)
        await manager.set_session_context_cache("session-1", self.context(21))
        generator = AsyncMock(side_effect=["summary-v1", "summary-v2"])

        first = await maybe_compress_conversation(
            settings, manager, "session-1", generator
        )
        context = deepcopy(first["context"])
        context["recent_turns"].extend(
            self.context(10)["recent_turns"][index] | {
                "turn_id": index + 22,
                "task_id": f"task-{index + 22}",
            }
            for index in range(10)
        )
        await manager.set_session_context_cache("session-1", context)

        second = await maybe_compress_conversation(
            settings, manager, "session-1", generator
        )

        self.assertTrue(second["compressed"])
        self.assertEqual(second["context"]["summary"], "summary-v2")
        self.assertEqual(second["context"]["summarized_turn_count"], 20)
        self.assertEqual(len(second["context"]["recent_turns"]), 11)
        self.assertEqual(generator.await_args_list[1].args[0], "summary-v1")
        self.assertEqual(
            [turn["turn_id"] for turn in generator.await_args_list[1].args[1]],
            list(range(11, 21)),
        )

    async def test_excel_candidates_only_use_latest_successful_excel_turn(self) -> None:
        settings = self.make_settings()
        context = self.context(0)
        context["recent_turns"] = [
            {
                "turn_id": 1,
                "task_id": "excel-old",
                "task_type": str(TaskType.EXCEL),
                "input_file_id": "old-input",
                "output_file_id": "old-output",
                "messages": [
                    {"role": "user", "content": "old"},
                    {"role": "assistant", "content": "done"},
                ],
            },
            {
                "turn_id": 2,
                "task_id": "excel-new",
                "task_type": str(TaskType.EXCEL),
                "input_file_id": "new-input",
                "output_file_id": "new-output",
                "messages": [
                    {"role": "user", "content": "new"},
                    {"role": "assistant", "content": "done"},
                ],
            },
        ]

        candidates = await get_excel_file_candidates(
            settings, "session-1", context=context
        )

        self.assertEqual(
            [item["file_id"] for item in candidates],
            ["new-output", "new-input"],
        )
        self.assertTrue(all(item["parent_task_id"] == "excel-new" for item in candidates))

    async def test_missing_inherited_output_falls_back_to_latest_input(self) -> None:
        settings = self.make_settings()
        with TemporaryDirectory() as directory:
            valid_path = Path(directory) / "input.xlsx"
            valid_path.write_bytes(b"xlsx")
            missing_path = Path(directory) / "missing.xlsx"
            file_manager = SimpleNamespace(
                resolve_path_async=AsyncMock(
                    side_effect=lambda file_id: {
                        "missing-output": missing_path,
                        "valid-input": valid_path,
                    }[file_id]
                )
            )
            candidates = [
                {
                    "file_id": "missing-output",
                    "file_source": "previous_output",
                    "parent_task_id": "excel-1",
                },
                {
                    "file_id": "valid-input",
                    "file_source": "previous_input",
                    "parent_task_id": "excel-1",
                },
            ]
            with patch(
                "app.api.endpoints.upload.get_excel_file_candidates",
                new=AsyncMock(return_value=candidates),
            ):
                resolved = await _resolve_excel_file(
                    settings,
                    file_manager,
                    session_id="session-1",
                    explicit_file_id=None,
                    context={},
                )

        self.assertEqual(resolved, ("valid-input", "previous_input", "excel-1"))

    async def test_redis_status_failures_do_not_block_in_memory_tasks(self) -> None:
        settings = self.make_settings()
        redis = SimpleNamespace(
            hset=AsyncMock(side_effect=ConnectionError("offline")),
            rpush=AsyncMock(side_effect=ConnectionError("offline")),
            set=AsyncMock(side_effect=ConnectionError("offline")),
        )
        manager = TaskManager(redis, settings)

        task = await manager.create_task(
            session_id="session-1",
            task_type=TaskType.CHAT,
            instruction="hello",
            execution_mode=ExecutionMode.LOCAL,
        )
        await manager.append_task_event(task["task_id"], "status", {"message": "ok"})
        reserved = await manager.reserve_idempotency("fingerprint", task["task_id"])

        self.assertEqual(manager.get_task(task["task_id"])["instruction"], "hello")
        self.assertTrue(reserved)

    def test_conversation_message_builder_adds_current_user_once(self) -> None:
        messages = AgentRouter._build_conversation_messages(
            "system",
            "current-user",
            [
                {"role": "user", "content": "previous-user"},
                {"role": "assistant", "content": "previous-assistant"},
            ],
        )

        self.assertEqual(
            [item["role"] for item in messages],
            ["system", "user", "assistant", "user"],
        )
        self.assertEqual(
            sum(item["content"] == "current-user" for item in messages),
            1,
        )

    async def test_creating_new_sessions_returns_isolated_ids(self) -> None:
        settings = self.make_settings()
        first = await create_chat_session(settings)
        second = await create_chat_session(settings)

        self.assertNotEqual(first["session_id"], second["session_id"])
        self.assertTrue(first["session_id"].startswith("session-"))
        self.assertTrue(second["session_id"].startswith("session-"))


if __name__ == "__main__":
    unittest.main()
