from __future__ import annotations

import json
import logging
import sys
import traceback
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import PurePath
from typing import Any

from app.core.request_context import request_id_context


class JsonFormatter(logging.Formatter):
    """Small dependency-free JSON formatter for application logs."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None) or request_id_context.get()
        if request_id is not None:
            payload["request_id"] = request_id
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, Mapping):
            payload.update(extra)
        if record.exc_info:
            # Exception messages, SQL parameter dumps and source lines may
            # contain credentials. Keep actionable locations, not raw prose.
            exception_type, _, tb = record.exc_info
            payload["exception_type"] = exception_type.__name__ if exception_type else "unknown"
            payload["exception_frames"] = [
                {
                    "file": PurePath(frame.filename).name,
                    "line": frame.lineno,
                    "function": frame.name,
                }
                for frame in traceback.extract_tb(tb)[-12:]
            ]
        return json.dumps(payload, default=str)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(level.upper())
