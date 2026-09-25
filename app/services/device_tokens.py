from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import DeviceToken
from app.domain.errors import DomainError

TOKEN_BYTES = 32
_HASH_LENGTH = 64  # sha256 hex digest length


class TokenError(DomainError):
    """Raised when a token operation is not allowed."""


def generate_token() -> str:
    """High-entropy opaque token, shown once at issuance."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """SHA-256 is appropriate here: tokens are 256-bit random values, not passwords."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class DeviceTokenService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def issue(self, device_id: int) -> tuple[DeviceToken, str]:
        """Create an active token for the device; returns (record, plaintext)."""
        token = generate_token()
        record = DeviceToken(device_id=device_id, token_hash=hash_token(token))
        self._session.add(record)
        self._session.flush()
        return record, token

    def rotate(self, device_id: int, current_token_id: int) -> tuple[DeviceToken, str]:
        """Revoke the current token and issue a new one."""
        current = self._session.get(DeviceToken, current_token_id)
        if current is None or current.device_id != device_id:
            raise TokenError("token_not_found")
        if current.revoked_at is None:
            current.revoked_at = datetime.now(UTC)
            current.is_active = False
        return self.issue(device_id)

    def revoke(self, token_id: int) -> None:
        token = self._session.get(DeviceToken, token_id)
        if token is None:
            raise TokenError("token_not_found")
        token.revoked_at = datetime.now(UTC)
        token.is_active = False

    def find_active_by_hash(self, token_hash: str) -> DeviceToken | None:
        stmt = select(DeviceToken).where(
            DeviceToken.token_hash == token_hash,
            DeviceToken.is_active.is_(True),
            DeviceToken.revoked_at.is_(None),
        )
        return self._session.execute(stmt).scalar_one_or_none()
