"""Shared bounded-evidence export representation, independent of log domains."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


class EvidenceExport(BaseModel):
    filename: str
    content: str
    media_type: str
    exported: int
    snapshot: int


class RecordingHealth(BaseModel):
    status: Literal["ready", "degraded", "unavailable"]
    schema_available: bool
    received_in_process: int
    write_failures_in_process: int
    last_persisted_at: datetime | None
    last_failure_at: datetime | None
    latest_stored_at: datetime | None
    process_started_at: datetime
    checked_at: datetime


def export_evidence(
    rows: list[dict[str, Any]],
    *,
    format: str,
    name: str,
    snapshot: int,
) -> EvidenceExport:
    if format == "json":
        content = json.dumps(rows, ensure_ascii=False, indent=2, default=str)
    else:
        buffer = io.StringIO(newline="")
        columns = list(rows[0]) if rows else ["id", "created_at"]
        writer = csv.DictWriter(buffer, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            # Spreadsheet formula protection applies to every exported string.
            values = {}
            for key, value in row.items():
                text = (
                    json.dumps(value, ensure_ascii=False, default=str)
                    if isinstance(value, (dict, list))
                    else str(value)
                    if value is not None
                    else ""
                )
                values[key] = (
                    "'" + text
                    if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r"))
                    else text
                )
            writer.writerow(values)
        content = buffer.getvalue()
    return EvidenceExport(
        filename=f"{name}-{snapshot}.{format}",
        content=content,
        media_type="application/json" if format == "json" else "text/csv;charset=utf-8",
        exported=len(rows),
        snapshot=snapshot,
    )
