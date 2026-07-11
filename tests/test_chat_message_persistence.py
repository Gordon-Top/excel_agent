from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.chat_history import append_chat_turn_message as _append_chat_turn_message
from app.models.session import ChatMessage


class ChatMessageModelTests(unittest.TestCase):
    def test_turn_payload_replaces_per_message_columns(self) -> None:
        fields = ChatMessage._meta.fields_map

        self.assertIn("task_id", fields)
        self.assertIn("message_json", fields)
        self.assertIn("updated_at", fields)
        self.assertNotIn("role", fields)
        self.assertNotIn("content", fields)
        self.assertNotIn("metadata_json", fields)
        self.assertNotIn("message_type", fields)

        self.assertTrue(fields["task_id"].unique)
        self.assertFalse(fields["task_id"].null)


class ChatMessagePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_creates_one_turn_row_with_first_message(self) -> None:
        settings = SimpleNamespace(mysql_dsn="mysql://configured")
        created_turn = SimpleNamespace(id=1, message_json=[
            {"role": "user", "content": "hello"}
        ])
        session_query = MagicMock()
        session_query.update = AsyncMock()

        with (
            patch(
                "app.services.chat_history.ChatMessage.get_or_create",
                new=AsyncMock(return_value=(created_turn, True)),
            ) as get_or_create,
            patch(
                "app.services.chat_history.ChatMessage.filter",
            ) as message_filter,
            patch(
                "app.services.chat_history.ChatSession.filter",
                return_value=session_query,
            ),
        ):
            await _append_chat_turn_message(
                settings,
                session_id="session-1",
                task_id="task-1",
                role="user",
                content="hello",
            )

        get_or_create.assert_awaited_once_with(
            task_id="task-1",
            defaults={
                "session_id": "session-1",
                "message_json": [{"role": "user", "content": "hello"}],
            },
        )
        message_filter.assert_not_called()
        session_query.update.assert_awaited_once()

    async def test_appends_later_messages_to_the_same_turn_row(self) -> None:
        settings = SimpleNamespace(mysql_dsn="mysql://configured")
        existing_turn = SimpleNamespace(
            id=7,
            message_json=[{"role": "user", "content": "hello"}],
        )
        message_query = MagicMock()
        message_query.update = AsyncMock()
        session_query = MagicMock()
        session_query.update = AsyncMock()

        with (
            patch(
                "app.services.chat_history.ChatMessage.get_or_create",
                new=AsyncMock(return_value=(existing_turn, False)),
            ),
            patch(
                "app.services.chat_history.ChatMessage.filter",
                return_value=message_query,
            ) as message_filter,
            patch(
                "app.services.chat_history.ChatSession.filter",
                return_value=session_query,
            ),
        ):
            await _append_chat_turn_message(
                settings,
                session_id="session-1",
                task_id="task-1",
                role="assistant",
                content="done",
            )

        message_filter.assert_called_once_with(id=7)
        update_kwargs = message_query.update.await_args.kwargs
        self.assertEqual(
            update_kwargs["message_json"],
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "done"},
            ],
        )
        self.assertIn("updated_at", update_kwargs)
        session_query.update.assert_awaited_once()

    async def test_skips_database_when_mysql_is_disabled(self) -> None:
        settings = SimpleNamespace(mysql_dsn=None)

        with patch(
            "app.services.chat_history.ChatMessage.get_or_create",
            new=AsyncMock(),
        ) as get_or_create:
            await _append_chat_turn_message(
                settings,
                session_id="session-1",
                task_id="task-1",
                role="user",
                content="hello",
            )

        get_or_create.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
