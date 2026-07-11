from __future__ import annotations

import asyncio
from pathlib import Path, PurePosixPath
import shlex
import time
from typing import Any

from app.core.config import Settings
from app.core.exceptions import SandboxExecutionError


class E2BClient:
    """Execute generated Excel code in an isolated E2B sandbox."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def execute(
        self,
        *,
        code: str,
        input_path: Path,
        output_dir: Path,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        if not self.settings.e2b_api_key:
            raise SandboxExecutionError("E2B API Key 未配置")

        try:
            from e2b_code_interpreter import AsyncSandbox
        except ImportError as exc:  # pragma: no cover - deployment guard
            raise SandboxExecutionError(
                "缺少 E2B Python SDK，请安装 e2b-code-interpreter"
            ) from exc

        timeout = timeout_seconds or self.settings.e2b_exec_timeout_seconds
        # E2B templates do not all expose the same home directory. /tmp is writable
        # across the official code-interpreter templates.
        remote_root = PurePosixPath("/tmp/excel-agent")
        remote_input = remote_root / f"input{input_path.suffix.lower()}"
        remote_output = remote_root / "outputs"
        remote_script = remote_root / "run_excel_task.py"
        output_dir.mkdir(parents=True, exist_ok=True)
        started = time.perf_counter()
        sandbox = None

        try:
            sandbox = await AsyncSandbox.create(
                api_key=self.settings.e2b_api_key,
                timeout=max(timeout + 30, 60),
                metadata={"service": "excel-agent"},
            )
            await sandbox.commands.run(
                f"mkdir -p {shlex.quote(str(remote_output))}",
                timeout=15,
            )
            await sandbox.files.write(str(remote_input), input_path.read_bytes())

            wrapped_code = (
                "from pathlib import Path\n"
                f"INPUT_PATH = Path({str(remote_input)!r})\n"
                f"OUTPUT_DIR = Path({str(remote_output)!r})\n"
                + code
                + "\n"
            )
            await sandbox.files.write(str(remote_script), wrapped_code)
            try:
                command_result = await sandbox.commands.run(
                    f"python {shlex.quote(str(remote_script))}",
                    timeout=timeout,
                )
            except Exception as exc:
                if all(
                    hasattr(exc, name)
                    for name in ("exit_code", "stdout", "stderr")
                ):
                    raise SandboxExecutionError(
                        "E2B 中的 Excel 处理代码执行失败",
                        detail={
                            "exit_code": getattr(exc, "exit_code", None),
                            "stdout": str(getattr(exc, "stdout", ""))[-8000:],
                            "stderr": str(getattr(exc, "stderr", ""))[-8000:],
                            "error": getattr(exc, "error", None),
                        },
                    ) from exc
                raise

            entries = await sandbox.files.list(str(remote_output), depth=20)
            generated_files: list[str] = []
            remote_files: list[str] = []
            for entry in entries:
                entry_type = getattr(getattr(entry, "type", None), "value", None)
                if entry_type != "file":
                    continue
                remote_path = PurePosixPath(entry.path)
                try:
                    relative = remote_path.relative_to(remote_output)
                except ValueError:
                    continue
                local_path = output_dir.joinpath(*relative.parts).resolve()
                if output_dir.resolve() not in local_path.parents:
                    continue
                local_path.parent.mkdir(parents=True, exist_ok=True)
                content = await sandbox.files.read(entry.path, format="bytes")
                local_path.write_bytes(content)
                generated_files.append(str(local_path))
                remote_files.append(entry.path)

            return {
                "mode": "e2b",
                "sandbox_id": getattr(sandbox, "sandbox_id", None),
                "stdout": command_result.stdout,
                "stderr": command_result.stderr,
                "exit_code": command_result.exit_code,
                "generated_files": generated_files,
                "remote_files": remote_files,
                "duration_ms": round((time.perf_counter() - started) * 1000),
            }
        except SandboxExecutionError:
            raise
        except asyncio.TimeoutError as exc:
            raise SandboxExecutionError(
                "E2B 执行超时", detail={"timeout_seconds": timeout}
            ) from exc
        except Exception as exc:
            raise SandboxExecutionError(
                "E2B 执行器调用失败",
                detail={"exception": str(exc), "type": type(exc).__name__},
            ) from exc
        finally:
            if sandbox is not None:
                try:
                    await sandbox.kill()
                except Exception:
                    pass
