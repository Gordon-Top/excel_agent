from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import time
from typing import Any
from uuid import uuid4

from app.core.exceptions import SandboxExecutionError


class LocalExecutor:
    """Development fallback executor using a separate Python process."""

    async def execute(
        self,
        *,
        code: str,
        input_path: Path,
        output_dir: Path,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        output_dir.mkdir(parents=True, exist_ok=True)
        script_path = output_dir / f".excel_task_{uuid4().hex}.py"
        wrapped_code = (
            "from pathlib import Path\n"
            f"INPUT_PATH = Path({str(input_path.resolve())!r})\n"
            f"OUTPUT_DIR = Path({str(output_dir.resolve())!r})\n"
            + code
            + "\n"
        )
        script_path.write_text(wrapped_code, encoding="utf-8")
        started = time.perf_counter()

        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(script_path),
                cwd=str(output_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(), timeout=timeout_seconds
                )
            except TimeoutError as exc:
                process.kill()
                await process.communicate()
                raise SandboxExecutionError(
                    "本地 Excel 执行超时",
                    detail={"timeout_seconds": timeout_seconds},
                ) from exc

            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")
            if process.returncode != 0:
                raise SandboxExecutionError(
                    "本地 Excel 处理代码执行失败",
                    detail={
                        "exit_code": process.returncode,
                        "stdout": stdout[-8000:],
                        "stderr": stderr[-8000:],
                    },
                )

            generated_files = [
                str(item.resolve())
                for item in output_dir.rglob("*")
                if item.is_file() and item != script_path
            ]
            return {
                "mode": "local",
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": process.returncode,
                "generated_files": generated_files,
                "duration_ms": round((time.perf_counter() - started) * 1000),
            }
        finally:
            script_path.unlink(missing_ok=True)
