"""Administrative sessions.

A session is a signed, short-lived token plus the account state it must still
agree with. The token alone is never sufficient: `resolve` reloads the account and
requires it to be active and to still carry the token's `session_version`, so a
deactivated account, a changed password, or a changed role ends every live session
at once rather than whenever the token happens to expire
(`docs/08-security.md` §5).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.security import AdminSession, create_access_token, decode_access_token
from app.db.models import AdminUser
from app.domain.errors import DomainError
from app.services.admin_users import AdminUserService


class AdminAuthService:
    def __init__(
        self, session: Session, *, settings: Settings, users: AdminUserService | None = None
    ) -> None:
        self._session = session
        self._settings = settings
        self._users = users or AdminUserService(session)

    def login(self, email: str, password: str) -> tuple[AdminUser, str, datetime]:
        """Verify credentials and issue a session token."""
        admin = self._users.authenticate(email, password)
        token, expires_at = self.issue_token(admin)
        return admin, token, expires_at

    def issue_token(self, admin: AdminUser) -> tuple[str, datetime]:
        return create_access_token(
            admin_id=admin.id,
            role=admin.role,
            session_version=admin.session_version,
            secret=self._secret,
            ttl_minutes=self._settings.admin_access_token_ttl_minutes,
        )

    def resolve(self, token: str | None) -> AdminUser:
        """Return the account a presented token belongs to, or fail."""
        if not token:
            raise DomainError("unauthenticated")
        session: AdminSession = decode_access_token(token, secret=self._secret)
        admin = self._session.get(AdminUser, session.admin_id)
        if admin is None or not admin.is_active:
            raise DomainError("invalid_session")
        if admin.session_version != session.session_version:
            # Revoked: a password change, a role change, or a deactivation.
            raise DomainError("invalid_session")
        return admin

    @property
    def _secret(self) -> str:
        return self._settings.admin_jwt_secret.get_secret_value()
