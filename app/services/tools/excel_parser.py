from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from app.core.exceptions import FileProcessingError


class ExcelParser:
    def parse_preview(self, file_path: Path, sample_rows: int = 5) -> dict[str, Any]:
        if not file_path.exists():
            raise FileProcessingError("文件不存在", detail={"path": str(file_path)})

        suffix = file_path.suffix.lower()
        if suffix == ".csv":
            dataframe = pd.read_csv(file_path)
            sheets = [self._build_sheet_payload("Sheet1", dataframe, sample_rows)]
        else:
            with pd.ExcelFile(file_path) as workbook:
                sheets = [
                    self._build_sheet_payload(sheet_name, workbook.parse(sheet_name), sample_rows)
                    for sheet_name in workbook.sheet_names
                ]

        columns = [column for sheet in sheets for column in sheet["columns"]]
        capabilities = self._detect_capabilities(columns)

        return {
            "file_summary": {
                "file_name": file_path.name,
                "sheet_count": len(sheets),
            },
            "sheets": sheets,
            "detected_capabilities": capabilities,
        }

    def _build_sheet_payload(
        self,
        sheet_name: str,
        dataframe: pd.DataFrame,
        sample_rows: int,
    ) -> dict[str, Any]:
        preview_df = dataframe.head(sample_rows).copy()
        preview_df = preview_df.where(pd.notnull(preview_df), None)
        columns = [
            {
                "name": str(column),
                "dtype": str(dataframe[column].dtype),
                "sample_values": [value for value in preview_df[column].tolist()[:sample_rows]],
            }
            for column in dataframe.columns
        ]
        return {
            "sheet_name": sheet_name,
            "row_count": int(dataframe.shape[0]),
            "column_count": int(dataframe.shape[1]),
            "columns": columns,
            "sample_rows": preview_df.to_dict(orient="records"),
        }

    def _detect_capabilities(self, columns: list[dict[str, Any]]) -> list[str]:
        capabilities: set[str] = {"cleaning", "aggregation"}
        for column in columns:
            name = column["name"].lower()
            if "date" in name or "time" in name:
                capabilities.add("date_analysis")
            if "image" in name or "img" in name or "picture" in name:
                capabilities.add("image_embedding")
            if "url" in name or "link" in name:
                capabilities.add("url_processing")
        return sorted(capabilities)
