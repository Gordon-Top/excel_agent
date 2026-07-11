from __future__ import annotations

import tempfile
from pathlib import Path
import unittest
from unittest.mock import AsyncMock

from openpyxl import Workbook, load_workbook

from app.constants.enums import ExecutionMode, SSEEventType
from app.core.config import Settings
from app.core.exceptions import ModelInvocationError
from app.services.agent.router import AgentRouter
from app.services.files.file_manager import FileManager


GENERATED_CODE = r'''
from pathlib import Path
from openpyxl import load_workbook
from openpyxl.styles import Font

workbook = load_workbook(INPUT_PATH)
worksheet = workbook["销售数据"]
rows = list(worksheet.iter_rows(min_row=2, values_only=True))
valid_rows = [row for row in rows if row[1] is not None]
valid_rows.sort(key=lambda row: row[1], reverse=True)
worksheet.delete_rows(2, worksheet.max_row)
worksheet.cell(1, 4, "销售等级")
for cell in worksheet[1]:
    cell.font = Font(bold=True)
for row_index, row in enumerate(valid_rows, start=2):
    worksheet.cell(row_index, 1, row[0])
    worksheet.cell(row_index, 2, row[1])
    worksheet.cell(row_index, 3, row[2])
    worksheet.cell(row_index, 4, "A" if row[1] >= 10000 else "B")
worksheet.freeze_panes = "A2"
output_path = Path(OUTPUT_DIR) / "processed_output.xlsx"
workbook.save(output_path)
load_workbook(output_path).close()
(Path(OUTPUT_DIR) / "summary.txt").write_text(
    f"删除空销售额行并排序，共保留 {len(valid_rows)} 行。", encoding="utf-8"
)
'''


class ExcelWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.settings = Settings(
            mysql_dsn=None,
            redis_url=None,
            deepseek_api_key="test-key",
            e2b_api_key=None,
            execution_mode=ExecutionMode.LOCAL,
            upload_dir=root / "uploads",
            output_dir=root / "outputs",
            temp_dir=root / "temp",
        )
        self.settings.ensure_storage_dirs()
        self.file_manager = FileManager(self.settings)
        self.input_path = self.settings.upload_dir / "input.xlsx"
        self._create_input_workbook(self.input_path)
        self.file_manager._files["file-input"] = {
            "file_id": "file-input",
            "original_name": "sales.xlsx",
            "stored_path": str(self.input_path),
            "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "size_bytes": self.input_path.stat().st_size,
            "file_role": "input",
        }
        self.router = AgentRouter(self.settings, self.file_manager)
        self.router.llm_client.complete_json = AsyncMock(
            return_value={
                "content": {
                    "task_analysis": "清洗销售数据并增加销售等级",
                    "execution_steps": ["删除空值", "排序", "增加等级并格式化"],
                    "python_code": GENERATED_CODE,
                    "expected_outputs": ["processed_output.xlsx", "summary.txt"],
                },
                "model": "test-model",
                "usage": {},
                "request_payload": {},
                "raw_response": {},
            }
        )

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_natural_language_workflow_generates_valid_downloadable_excel(self) -> None:
        events = []
        async for event_name, payload in self.router.process_excel(
            file_id="file-input",
            instruction="删除销售额为空的行，倒序排列并增加销售等级，表头加粗、冻结首行",
            execution_mode=ExecutionMode.LOCAL,
            task_id="task-workflow-test",
        ):
            events.append((event_name, payload))

        final_result = next(
            payload
            for event_name, payload in events
            if event_name == SSEEventType.RESULT
            and payload.get("kind") == "execution_result"
        )
        primary = final_result["primary_output"]
        self.assertTrue(primary["download_url"].endswith("/download"))
        self.assertEqual(primary["filename"], "processed_output.xlsx")
        registered = await self.file_manager.get_async(primary["file_id"])
        output_path = Path(str(registered["stored_path"]))
        self.assertTrue(output_path.exists())

        workbook = load_workbook(output_path)
        worksheet = workbook["销售数据"]
        self.assertEqual(worksheet.max_row, 3)
        self.assertEqual([worksheet.cell(2, 2).value, worksheet.cell(3, 2).value], [15000, 8000])
        self.assertEqual([worksheet.cell(2, 4).value, worksheet.cell(3, 4).value], ["A", "B"])
        self.assertTrue(worksheet.cell(1, 1).font.bold)
        self.assertEqual(worksheet.freeze_panes, "A2")
        workbook.close()

    async def test_complete_chat_executes_excel_instead_of_only_returning_advice(self) -> None:
        result = await self.router.complete_chat(
            message="整理销售表并返回新文件",
            allow_search=False,
            file_id="file-input",
            execution_mode=ExecutionMode.LOCAL,
            task_id="task-chat-excel-test",
        )
        self.assertIsNotNone(result["output_file_id"])
        self.assertIn("下载地址", result["content"])
        self.assertEqual(result["metadata"]["execution"]["mode"], "local")

    async def test_failed_generated_code_is_repaired_and_retried(self) -> None:
        broken_code = r'''
from pathlib import Path
from openpyxl import load_workbook
workbook = load_workbook(INPUT_PATH)
worksheet = workbook["销售数据"]
for row in range(2, worksheet.max_row + 1):
    worksheet.cell(row, 4, "A" if worksheet.cell(row, 2).value >= 10000 else "B")
workbook.save(Path(OUTPUT_DIR) / "processed_output.xlsx")
'''
        good_response = {
            "content": {
                "task_analysis": "修复空值处理后完成任务",
                "execution_steps": ["先过滤空值", "再排序和分级"],
                "python_code": GENERATED_CODE,
                "expected_outputs": ["processed_output.xlsx", "summary.txt"],
            },
            "model": "test-model",
            "usage": {},
            "request_payload": {},
            "raw_response": {},
        }
        self.router.llm_client.complete_json = AsyncMock(
            side_effect=[
                {
                    "content": {
                        "task_analysis": "首次生成的代码存在空值缺陷",
                        "execution_steps": ["直接分级"],
                        "python_code": broken_code,
                        "expected_outputs": ["processed_output.xlsx"],
                    },
                    "model": "test-model",
                    "usage": {},
                    "request_payload": {},
                    "raw_response": {},
                },
                good_response,
            ]
        )

        final_result = None
        async for event_name, payload in self.router.process_excel(
            file_id="file-input",
            instruction="清洗并分级",
            execution_mode=ExecutionMode.LOCAL,
            task_id="task-repair-test",
        ):
            if event_name == SSEEventType.RESULT and payload.get("kind") == "execution_result":
                final_result = payload

        self.assertIsNotNone(final_result)
        assert final_result is not None
        self.assertEqual(final_result["execution"]["attempt_count"], 2)
        self.assertEqual(len(final_result["execution"]["repair_history"]), 1)
        self.assertEqual(self.router.llm_client.complete_json.await_count, 2)

    def test_generated_code_cannot_use_system_or_network_modules(self) -> None:
        with self.assertRaises(ModelInvocationError):
            self.router._sanitize_python_code(
                'import os\nprint(INPUT_PATH, OUTPUT_DIR)\nos.system("whoami")'
            )

    def test_generated_code_can_use_safe_path_helpers(self) -> None:
        code = self.router._sanitize_python_code(
            """import os
import shutil
from pathlib import Path

os.makedirs(OUTPUT_DIR, exist_ok=True)
output_path = os.path.join(OUTPUT_DIR, "processed_output.xlsx")
shutil.copy(INPUT_PATH, output_path)
"""
        )
        self.assertIn("os.path.join", code)
        self.assertIn("shutil.copy", code)

    def test_generated_code_cannot_import_network_modules(self) -> None:
        with self.assertRaises(ModelInvocationError):
            self.router._sanitize_python_code(
                'import requests\nprint(INPUT_PATH, OUTPUT_DIR)\nrequests.get("https://example.com")'
            )

    def test_generated_code_cannot_import_dangerous_os_member(self) -> None:
        with self.assertRaises(ModelInvocationError):
            self.router._sanitize_python_code(
                'from os import system\nprint(INPUT_PATH, OUTPUT_DIR)\nsystem("whoami")'
            )

    def test_generated_code_strips_contract_path_overrides(self) -> None:
        code = self.router._sanitize_python_code(
            '''INPUT_PATH = "other.xlsx"
OUTPUT_DIR = "elsewhere"
print(INPUT_PATH, OUTPUT_DIR)'''
        )
        self.assertNotIn("other.xlsx", code)
        self.assertNotIn("elsewhere", code)
        self.assertIn("print(INPUT_PATH, OUTPUT_DIR)", code)

    def test_generated_code_strips_path_normalization_assignments(self) -> None:
        code = self.router._sanitize_python_code(
            '''from pathlib import Path
INPUT_PATH = Path(INPUT_PATH)
OUTPUT_DIR = Path(OUTPUT_DIR)
output_path = OUTPUT_DIR / "processed_output.xlsx"
print(INPUT_PATH, output_path)'''
        )
        self.assertNotIn("INPUT_PATH =", code)
        self.assertNotIn("OUTPUT_DIR =", code)
        self.assertIn("OUTPUT_DIR / 'processed_output.xlsx'", code)

    @staticmethod
    def _create_input_workbook(path: Path) -> None:
        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = "销售数据"
        worksheet.append(["姓名", "销售额", "日期"])
        worksheet.append(["张三", 8000, "2026-07-01"])
        worksheet.append(["李四", None, "2026-07-02"])
        worksheet.append(["王五", 15000, "2026-07-03"])
        workbook.save(path)


if __name__ == "__main__":
    unittest.main()
