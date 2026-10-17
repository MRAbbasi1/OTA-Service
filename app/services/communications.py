"""Durable, bounded-field OTA evidence. No raw headers, tokens, or binary capture."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from app.db.models import Device, DeviceCommunication

logger = logging.getLogger(__name__)


def safe_segment(value: str | None, limit: int = 64) -> str | None:
    if value and len(value) <= limit and re.fullmatch(r"[A-Za-z0-9_.:-]+", value):
        return value
    return None


class CommunicationRecorder:
    """Persist receipt before work, then response preparation and transfer outcome.

    A separate short transaction avoids changing OTA service commits. Evidence
    failure never changes device-facing status or bytes. An unfinished receipt
    remains visible after a crash; it is never inferred to be successful.
    """

    def __init__(
        self,
        factory: sessionmaker[Session],
        *,
        request_id: str | None,
        kind: str,
        serial: str | None,
        mac: str | None,
        token_present: bool,
        parameters: dict[str, str],
        observe: Callable[[str], None] | None = None,
        method: str = "GET",
    ) -> None:
        self.factory = factory
        self.started = time.perf_counter()
        self.device_id: int | None = None
        self.reason: str | None = None
        self.version: str | None = None
        self.id: int | None = None
        self.responded = False
        self._observe = observe
        serial = serial.strip() if serial else ""
        claimed = str(int(serial)) if re.fullmatch(r"[0-9]{1,19}", serial) else None
        safe_mac = (
            mac.upper()
            if mac and re.fullmatch(r"[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}", mac)
            else None
        )
        self.detail: dict[str, Any] = {
            "request": {
                "method": method,
                "route": (
                    "/api/v1/firmware/{device_type}/{platform}/manifest.json"
                    if kind == "manifest"
                    else "/firmware/{device_type}/{platform}/v{version}/{filename}"
                ),
                "serial": claimed,
                "mac": safe_mac,
                "token_present": token_present,
                "parameters": {key: safe_segment(value) for key, value in parameters.items()},
            },
            "events": [{"event": "request_received", "at": datetime.now(UTC).isoformat()}],
        }
        try:
            with factory.begin() as session:
                row = DeviceCommunication(
                    request_id=request_id or str(uuid.uuid4()),
                    kind=kind,
                    claimed_serial=claimed,
                    outcome="received",
                    detail=self.detail,
                    created_at=datetime.now(UTC),
                )
                session.add(row)
                session.flush()
                self.id = row.id
            self._notify("received")
        except Exception:
            self._notify("failed")
            logger.error("communication_record_failed")

    def _notify(self, event: str) -> None:
        if self._observe is not None:
            try:
                self._observe(event)
            except Exception:
                logger.error("communication_record_failed")

    def rejected(self, status: int, reason: str) -> None:
        self.responded = True
        self.reason = reason
        self.detail["request"]["route"] = None
        self.detail["request"]["path_capture"] = "unmatched_ota_path_redacted"
        self._save("request_rejected", status_code=status, completed_at=datetime.now(UTC))

    def authenticated(self, device_id: int) -> None:
        self.device_id = device_id

    def decision(self, reason: str, version: str | None = None) -> None:
        self.reason = safe_segment(reason)
        self.version = safe_segment(version, 15)

    def response(self, status: int, body: bytes, *, streaming: bool, size: int) -> None:
        self.responded = True
        if streaming:
            outcome = "streaming"
        elif status == 200:
            outcome = "offered"
        elif status in (401, 403):
            outcome = "auth_rejected"
        elif status == 404:
            outcome = "no_offer"
        elif status == 429:
            outcome = "rate_limited"
        else:
            outcome = "server_error"
        response_body: dict[str, Any] | None = None
        if body and len(body) <= 4096:
            try:
                parsed = json.loads(body)
                if isinstance(parsed, dict):
                    response_body = {
                        key: value
                        for key, value in parsed.items()
                        if key in {"version", "url", "md5", "size", "signature", "code", "reason"}
                        and isinstance(value, (str, int))
                        and len(str(value)) <= 1024
                    }
            except (ValueError, UnicodeError):
                pass
        self.detail["response"] = {
            "body": response_body,
            "version": self.version,
            "body_capture": "binary_not_captured" if streaming else "reviewed_fields_only",
        }
        self._save(
            outcome,
            status_code=status,
            bytes_expected=size,
            bytes_sent=None if streaming else len(body),
            completed_at=None if streaming else datetime.now(UTC),
        )

    def transfer(self, sent: int, expected: int, completed: bool) -> None:
        self._save(
            "download_served" if completed and sent == expected else "transfer_incomplete",
            bytes_sent=sent,
            completed_at=datetime.now(UTC),
        )

    def failed(self) -> None:
        self.responded = True
        self.reason = "internal_error"
        self._save("server_error", status_code=500, completed_at=datetime.now(UTC))

    def _save(self, outcome: str, **values: Any) -> None:
        if self.id is None:
            return
        self.detail["events"].append({"event": outcome, "at": datetime.now(UTC).isoformat()})
        try:
            with self.factory.begin() as session:
                row = session.get(DeviceCommunication, self.id)
                if row is None:
                    return
                row.device_id = self.device_id
                row.outcome = outcome
                row.reason = self.reason
                row.duration_ms = round((time.perf_counter() - self.started) * 1000, 2)
                row.detail = dict(self.detail)
                for key, value in values.items():
                    setattr(row, key, value)
            self._notify("persisted")
        except Exception:
            self._notify("failed")
            logger.error("communication_record_failed")


class CommunicationService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def summary(self) -> dict[str, Any]:
        until = datetime.now(UTC)
        since = until - timedelta(hours=24)
        counts = dict(
            self.session.execute(
                select(DeviceCommunication.outcome, func.count())
                .where(
                    DeviceCommunication.created_at >= since, DeviceCommunication.created_at <= until
                )
                .group_by(DeviceCommunication.outcome)
            ).all()
        )
        return {
            "since": since,
            "until": until,
            "total": sum(counts.values()),
            "by_outcome": counts,
            "last_received_at": self.session.scalar(
                select(func.max(DeviceCommunication.created_at))
            ),
        }

    def list(
        self,
        *,
        serial: str | None = None,
        device_id: int | None = None,
        device_type_id: int | None = None,
        release_id: int | None = None,
        policy_id: int | None = None,
        kind: str | None = None,
        outcome: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        request_id: str | None = None,
        snapshot: int | None = None,
        page: int = 1,
        page_size: int = 50,
        sort: str = "newest",
    ) -> tuple[list[DeviceCommunication], int, int]:
        snapshot = (
            snapshot
            if snapshot is not None
            else int(self.session.scalar(select(func.max(DeviceCommunication.id))) or 0)
        )
        conditions = [DeviceCommunication.id <= snapshot]
        if serial:
            conditions.append(
                DeviceCommunication.claimed_serial.startswith(serial.lstrip("0") or "0")
            )
        if device_id is not None:
            conditions.append(DeviceCommunication.device_id == device_id)
        if device_type_id is not None:
            conditions.append(
                DeviceCommunication.device_id.in_(
                    select(Device.id).where(Device.device_type_id == device_type_id)
                )
            )
        if release_id is not None:
            conditions.append(
                or_(
                    DeviceCommunication.detail["decision"]["release_id"].as_integer() == release_id,
                    DeviceCommunication.detail["delivery"]["release_id"].as_integer() == release_id,
                )
            )
        if policy_id is not None:
            conditions.append(
                DeviceCommunication.detail["decision"]["policy_id"].as_integer() == policy_id
            )
        if kind:
            conditions.append(DeviceCommunication.kind == kind)
        if outcome:
            conditions.append(DeviceCommunication.outcome == outcome)
        if since:
            conditions.append(DeviceCommunication.created_at >= since)
        if until:
            conditions.append(DeviceCommunication.created_at <= until)
        if request_id:
            conditions.append(DeviceCommunication.request_id == request_id)
        stmt = select(DeviceCommunication).where(*conditions)
        total = int(self.session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
        orders: dict[str, ColumnElement[Any]] = {
            "newest": DeviceCommunication.id.desc(),
            "oldest": DeviceCommunication.id.asc(),
            "slowest": DeviceCommunication.duration_ms.desc().nulls_last(),
        }
        rows = self.session.scalars(
            stmt.order_by(orders[sort], DeviceCommunication.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(rows), total, snapshot


def recording_health(
    factory: sessionmaker[Session], process: dict[str, Any], started_at: datetime
) -> dict[str, Any]:
    latest = None
    available = True
    try:
        with factory() as session:
            latest = session.scalar(
                select(DeviceCommunication.created_at)
                .order_by(DeviceCommunication.id.desc())
                .limit(1)
            )
    except SQLAlchemyError:
        available = False
    return {
        "status": "unavailable"
        if not available
        else "degraded"
        if process["persistence_degraded"]
        else "ready",
        "schema_available": available,
        "received_in_process": process["received_in_process"],
        "write_failures_in_process": process["write_failures_in_process"],
        "last_persisted_at": process["last_persisted_at"],
        "last_failure_at": process["last_failure_at"],
        "latest_stored_at": latest,
        "process_started_at": started_at,
        "checked_at": datetime.now(UTC),
    }
