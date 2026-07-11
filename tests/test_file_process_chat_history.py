from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.api.endpoints.upload import process_file, process_file_stream
from app.constants.enums import ExecutionMode, SSEEventType
from app.schemas.payloads import ExcelProcessRequest


class FakeAgentRouter:
    def __init__(self, settings, file_manager) -> None:
        self.settings = settings
        self.file_manager = file_manager

    async def process_excel(self, **kwargs):
        yield SSEEventType.RESULT, {
            "kind": "execution_result",
            "summary": "processed",
            "primary_output": {
                "file_id": "file-output",
                "filename": "processed_output.xlsx",
                "download_url": "/api/files/file-output/download",
            },
            "outputs": [
                {
                    "file_id": "file-output",
                    "filename": "processed_output.xlsx",
                    "download_url": "/api/files/file-output/download",
                    "is_primary": True,
                }
            ],
            "execution": {"mode": "e2b"},
            "output_preview": {"sheets": []},
        }

    async def generate_session_title(self, **kwargs) -> str:
        return "Excel order cleanup"

    @staticmethod
    def _build_excel_answer(result) -> str:
        return "Excel processed: /api/files/file-output/download"


class FileProcessChatHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_sync_file_process_persists_one_complete_chat_turn(self) -> None:
        settings = SimpleNamespace(
            mysql_dsn="mysql://configured",
            e2b_api_key="configured",
            execution_mode=ExecutionMode.LOCAL,
        )
        task_manager = SimpleNamespace(
            create_task=AsyncMock(return_value={"task_id": "task-excel-1"}),
            update_task=AsyncMock(),
            append_task_event=AsyncMock(),
            append_session_memory=AsyncMock(),
        )
        append_message = AsyncMock()
        ensure_session = AsyncMock(return_value=True)
        set_title = AsyncMock()

        with (
            patch("app.api.endpoints.upload.get_file_manager", return_value=object()),
            patch("app.api.endpoints.upload.get_task_manager", return_value=task_manager),
            patch("app.api.endpoints.upload.AgentRouter", FakeAgentRouter),
            patch("app.api.endpoints.upload.ensure_chat_session", ensure_session),
            patch("app.api.endpoints.upload.append_chat_turn_message", append_message),
            patch("app.api.endpoints.upload.set_session_title", set_title),
        ):
            response = await process_file(
                ExcelProcessRequest(
                    file_id="file-input",
                    instruction="clean the order workbook",
                    session_id="session-excel-1",
                    execution_mode=ExecutionMode.E2B,
                ),
                settings=settings,
                session_id_header="session-header",
            )

        self.assertEqual(response.session_id, "session-excel-1")
        self.assertEqual(response.output_file_id, "file-output")
        ensure_session.assert_awaited_once_with(settings, "session-excel-1")
        self.assertEqual(append_message.await_count, 2)

        user_call = append_message.await_args_list[0].kwargs
        self.assertEqual(user_call["task_id"], "task-excel-1")
        self.assertEqual(user_call["role"], "user")
        self.assertEqual(user_call["content"], "clean the order workbook")
        self.assertEqual(user_call["metadata"]["file_id"], "file-input")

        assistant_call = append_message.await_args_list[1].kwargs
        self.assertEqual(assistant_call["role"], "assistant")
        self.assertEqual(assistant_call["metadata"]["output_file_id"], "file-output")
        self.assertEqual(len(assistant_call["metadata"]["outputs"]), 1)

        set_title.assert_awaited_once_with(
            settings,
            session_id="session-excel-1",
            title="Excel order cleanup",
        )
        self.assertEqual(task_manager.append_session_memory.await_count, 2)


    async def test_stream_file_process_persists_one_complete_chat_turn(self) -> None:
        settings = SimpleNamespace(
            mysql_dsn="mysql://configured",
            e2b_api_key="configured",
            execution_mode=ExecutionMode.LOCAL,
        )
        task_manager = SimpleNamespace(
            generate_task_id=MagicMock(return_value="task-stream-1"),
            create_idempotency_fingerprint=MagicMock(return_value="fingerprint-1"),
            acquire_session_lock=AsyncMock(return_value=True),
            reserve_idempotency=AsyncMock(return_value=True),
            release_session_lock=AsyncMock(),
            create_task=AsyncMock(return_value={"task_id": "task-stream-1"}),
            update_task=AsyncMock(),
            append_task_event=AsyncMock(),
            append_session_memory=AsyncMock(),
        )
        append_message = AsyncMock()
        ensure_session = AsyncMock(return_value=False)

        class FakeEventSourceResponse:
            def __init__(self, content) -> None:
                self.content = content

        with (
            patch("app.api.endpoints.upload.get_file_manager", return_value=object()),
            patch("app.api.endpoints.upload.get_task_manager", return_value=task_manager),
            patch("app.api.endpoints.upload.AgentRouter", FakeAgentRouter),
            patch("app.api.endpoints.upload.ensure_chat_session", ensure_session),
            patch("app.api.endpoints.upload.append_chat_turn_message", append_message),
            patch("app.api.endpoints.upload.iter_sse", side_effect=lambda events: events),
            patch("app.api.endpoints.upload.EventSourceResponse", FakeEventSourceResponse),
        ):
            response = await process_file_stream(
                ExcelProcessRequest(
                    file_id="file-input",
                    instruction="stream clean workbook",
                    session_id="session-stream-1",
                    execution_mode=ExecutionMode.E2B,
                ),
                settings=settings,
                session_id_header="session-header",
            )
            events = [event async for event in response.content]

        self.assertTrue(events)
        self.assertEqual(append_message.await_count, 2)
        self.assertEqual(append_message.await_args_list[0].kwargs["role"], "user")
        self.assertEqual(append_message.await_args_list[1].kwargs["role"], "assistant")
        self.assertEqual(
            append_message.await_args_list[1].kwargs["metadata"]["output_file_id"],
            "file-output",
        )
        task_manager.release_session_lock.assert_awaited_with(
            "session-stream-1", "task-stream-1"
        )



if __name__ == "__main__":
    unittest.main()
