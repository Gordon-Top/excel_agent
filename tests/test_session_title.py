from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.chat_history import (
    ensure_chat_session as _ensure_chat_session,
    set_session_title as _set_session_title,
)


class ChatSessionTitlePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_session_is_created_without_copying_user_message(self) -> None:
        settings = SimpleNamespace(mysql_dsn="mysql://configured")
        session = SimpleNamespace(title=None)

        with patch(
            "app.services.chat_history.ChatSession.get_or_create",
            new=AsyncMock(return_value=(session, True)),
        ) as get_or_create:
            needs_title = await _ensure_chat_session(settings, "session-1")

        self.assertTrue(needs_title)
        get_or_create.assert_awaited_once_with(
            session_id="session-1",
            defaults={"title": None},
        )

    async def test_existing_titled_session_does_not_request_new_title(self) -> None:
        settings = SimpleNamespace(mysql_dsn="mysql://configured")
        session = SimpleNamespace(title="Existing title")
        query = MagicMock()
        query.update = AsyncMock()

        with (
            patch(
                "app.services.chat_history.ChatSession.get_or_create",
                new=AsyncMock(return_value=(session, False)),
            ),
            patch(
                "app.services.chat_history.ChatSession.filter",
                return_value=query,
            ),
        ):
            needs_title = await _ensure_chat_session(settings, "session-1")

        self.assertFalse(needs_title)
        query.update.assert_awaited_once()

    async def test_persists_generated_title(self) -> None:
        settings = SimpleNamespace(mysql_dsn="mysql://configured")
        query = MagicMock()
        query.update = AsyncMock()

        with patch(
            "app.services.chat_history.ChatSession.filter",
            return_value=query,
        ) as session_filter:
            await _set_session_title(
                settings,
                session_id="session-1",
                title="Generated title",
            )

        session_filter.assert_called_once_with(session_id="session-1")
        update_kwargs = query.update.await_args.kwargs
        self.assertEqual(update_kwargs["title"], "Generated title")
        self.assertIn("updated_at", update_kwargs)


if __name__ == "__main__":
    unittest.main()
