from __future__ import annotations

from pathlib import Path
import mimetypes
from uuid import uuid4

import aiofiles
from fastapi import UploadFile

from app.constants.enums import FileRole
from app.core.config import Settings
from app.core.exceptions import FileProcessingError
from app.models.task import StoredFile


class FileManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._files: dict[str, dict[str, object]] = {}

    async def save_upload(
        self, upload: UploadFile, role: FileRole = FileRole.INPUT
    ) -> dict[str, object]:
        suffix = Path(upload.filename or "").suffix.lower()
        if suffix not in {".xlsx", ".xlsm", ".csv"}:
            raise FileProcessingError("仅支持 .xlsx、.xlsm、.csv 文件")

        target_dir = (
            self.settings.upload_dir
            if role == FileRole.INPUT
            else self.settings.output_dir
        )
        file_id = f"file-{uuid4().hex}"
        target_path = target_dir / f"{file_id}{suffix}"

        content = await upload.read()
        max_size = self.settings.max_upload_size_mb * 1024 * 1024
        if len(content) > max_size:
            raise FileProcessingError(
                "上传文件超出大小限制",
                detail={"max_size_mb": self.settings.max_upload_size_mb},
            )

        async with aiofiles.open(target_path, "wb") as f:
            await f.write(content)

        record = {
            "file_id": file_id,
            "original_name": upload.filename or target_path.name,
            "stored_path": str(target_path),
            "mime_type": upload.content_type or "application/octet-stream",
            "size_bytes": len(content),
            "file_role": role,
        }
        self._files[file_id] = record
        await self._persist_file_record(record)
        return record

    async def register_generated_file(
        self, path: Path, original_name: str | None = None
    ) -> dict[str, object]:
        resolved_path = path.resolve()
        if not resolved_path.exists() or not resolved_path.is_file():
            raise FileProcessingError(
                "生成文件不存在",
                detail={"path": str(resolved_path)},
            )

        file_id = f"file-{uuid4().hex}"
        mime_type = mimetypes.guess_type(resolved_path.name)[0] or "application/octet-stream"
        record = {
            "file_id": file_id,
            "original_name": original_name or resolved_path.name,
            "stored_path": str(resolved_path),
            "mime_type": mime_type,
            "size_bytes": resolved_path.stat().st_size,
            "file_role": FileRole.OUTPUT,
        }
        self._files[file_id] = record
        await self._persist_file_record(record)
        return record

    def get(self, file_id: str) -> dict[str, object]:
        if file_id not in self._files:
            raise FileProcessingError("文件不存在或未上传", detail={"file_id": file_id})
        return self._files[file_id]

    async def get_async(self, file_id: str) -> dict[str, object]:
        if file_id in self._files:
            return self._files[file_id]

        if not self.settings.mysql_dsn:
            raise FileProcessingError("文件不存在或未上传", detail={"file_id": file_id})

        db_record = await StoredFile.get_or_none(file_id=file_id)
        if db_record is None:
            raise FileProcessingError("文件不存在或未上传", detail={"file_id": file_id})

        record = self._deserialize_file_record(db_record)
        self._files[file_id] = record
        return record

    def resolve_path(self, file_id: str) -> Path:
        record = self.get(file_id)
        return Path(str(record["stored_path"]))

    async def resolve_path_async(self, file_id: str) -> Path:
        record = await self.get_async(file_id)
        return Path(str(record["stored_path"]))

    # 文件元数据既保留在内存，也会落到 MySQL，便于后续任务和下载回溯。
    async def _persist_file_record(self, record: dict[str, object]) -> None:
        if not self.settings.mysql_dsn:
            return

        defaults = {
            "original_name": str(record["original_name"]),
            "stored_path": str(record["stored_path"]),
            "mime_type": str(record["mime_type"]),
            "size_bytes": int(record["size_bytes"]),
            "file_role": str(record["file_role"]),
        }
        stored_file, created = await StoredFile.get_or_create(
            file_id=str(record["file_id"]),
            defaults=defaults,
        )
        if created:
            return

        await StoredFile.filter(file_id=str(record["file_id"])).update(**defaults)

    @staticmethod
    def _deserialize_file_record(record: StoredFile) -> dict[str, object]:
        return {
            "file_id": record.file_id,
            "original_name": record.original_name,
            "stored_path": record.stored_path,
            "mime_type": record.mime_type,
            "size_bytes": record.size_bytes,
            "file_role": FileRole(record.file_role),
        }
