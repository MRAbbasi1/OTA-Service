"""Administrative accounts: creation, roles, status, and credentials.

This service owns the one path by which an administrator comes into existence.
There is no default account and no seeded password: the first `SUPER_ADMIN` is
created by the deployment command, and every later one by a `SUPER_ADMIN` through
the API, always audited (`docs/08-security.md` §16).

Two invariants are enforced here because neither is safe to leave to a caller:

* the last active `SUPER_ADMIN` cannot be deactivated or demoted, or the platform
  would have no account able to recreate one;
* any credential or status change bumps `session_version`, which invalidates
  already-issued sessions immediately.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import (
    hash_password,
    password_needs_rehash,
    validate_password_strength,
    verify_password,
)
from app.db.models import AdminRole, AdminUser
from app.domain.errors import DomainError

logger = logging.getLogger(__name__)

EMAIL_MAX_LENGTH = 254


class AdminUserService:
    def __init__(self, session: Session) -> None:
        self._session = session

    # --- bootstrap ------------------------------------------------------------------

    def bootstrap_super_admin(self, email: str, password: str) -> tuple[AdminUser, bool]:
        """Create the first `SUPER_ADMIN`, or report that one already exists.

        Idempotent on purpose: a deployment command may run on every boot, so it
        must never overwrite an existing account or fail because one is present.
        The password is never logged, and it is never accepted outside this call.
        """
        if self.count() > 0:
            existing = self._session.execute(
                select(AdminUser).order_by(AdminUser.id).limit(1)
            ).scalar_one()
            return existing, False
        admin = self.create(email=email, password=password, role=AdminRole.SUPER_ADMIN)
        logger.info(
            "admin_bootstrapped",
            extra={"extra_fields": {"admin_id": admin.id, "role": admin.role.value}},
        )
        return admin, True

    # --- writes ---------------------------------------------------------------------

    def create(self, *, email: str, password: str, role: AdminRole) -> AdminUser:
        normalized = normalize_email(email)
        validate_password_strength(password)
        if self._find(normalized) is not None:
            raise DomainError("admin_email_exists")
        admin = AdminUser(
            email=normalized,
            password_hash=hash_password(password),
            role=role,
        )
        self._session.add(admin)
        try:
            self._session.flush()
        except IntegrityError:  # two concurrent creations of the same email
            self._session.rollback()
            raise DomainError("admin_email_exists") from None
        return admin

    def set_active(self, admin_id: int, is_active: bool) -> AdminUser:
        admin = self.get(admin_id)
        if not is_active:
            self._assert_not_last_super_admin(admin)
            admin.session_version += 1  # deactivation must end live sessions
        admin.is_active = is_active
        self._session.flush()
        return admin

    def set_role(self, admin_id: int, role: AdminRole) -> AdminUser:
        admin = self.get(admin_id)
        if role is not AdminRole.SUPER_ADMIN:
            self._assert_not_last_super_admin(admin)
        if role is not admin.role:
            # The role is part of the token, so a change has to invalidate it.
            admin.session_version += 1
        admin.role = role
        self._session.flush()
        return admin

    def set_password(self, admin_id: int, password: str) -> AdminUser:
        validate_password_strength(password)
        admin = self.get(admin_id)
        admin.password_hash = hash_password(password)
        admin.session_version += 1
        self._session.flush()
        return admin

    def change_own_password(
        self, admin_id: int, *, current_password: str, new_password: str
    ) -> AdminUser:
        """Verify the current password before replacing it.

        A stolen session alone must not be enough to take over the account, which
        is the point of asking for the password that the attacker does not have.
        """
        admin = self.get(admin_id)
        if not verify_password(current_password, admin.password_hash):
            raise DomainError("invalid_password")
        validate_password_strength(new_password)
        if verify_password(new_password, admin.password_hash):
            raise DomainError("password_unchanged")
        return self.set_password(admin_id, new_password)

    # --- reads ----------------------------------------------------------------------

    def count(self) -> int:
        return int(self._session.execute(select(func.count()).select_from(AdminUser)).scalar_one())

    def get(self, admin_id: int) -> AdminUser:
        admin = self._session.get(AdminUser, admin_id)
        if admin is None:
            raise DomainError("admin_not_found")
        return admin

    def get_by_email(self, email: str) -> AdminUser | None:
        return self._find(normalize_email(email))

    def list_admins(self, *, page: int = 1, page_size: int = 50) -> tuple[list[AdminUser], int]:
        total = self.count()
        rows = self._session.execute(
            select(AdminUser).order_by(AdminUser.id).offset((page - 1) * page_size).limit(page_size)
        ).scalars()
        return list(rows), total

    def authenticate(self, email: str, password: str) -> AdminUser:
        """Resolve an email/password pair, or raise ``invalid_credentials``.

        The same error is returned for an unknown email, a wrong password, and an
        inactive account: the response must not tell a caller which accounts
        exist, and the hashing work is done either way so the timing does not
        either (`app.core.security.verify_password`).
        """
        admin = self._find(normalize_email(email))
        stored_hash = admin.password_hash if admin is not None else None
        password_ok = verify_password(password, stored_hash)
        if admin is None or not password_ok or not admin.is_active:
            raise DomainError("invalid_credentials")
        if password_needs_rehash(admin.password_hash):
            admin.password_hash = hash_password(password)
        admin.last_login_at = datetime.now(UTC)
        self._session.flush()
        return admin

    # --- internals ------------------------------------------------------------------

    def _find(self, email: str) -> AdminUser | None:
        return self._session.execute(
            select(AdminUser).where(AdminUser.email == email)
        ).scalar_one_or_none()

    def _assert_not_last_super_admin(self, admin: AdminUser) -> None:
        if admin.role is not AdminRole.SUPER_ADMIN:
            return
        others = int(
            self._session.execute(
                select(func.count())
                .select_from(AdminUser)
                .where(
                    AdminUser.role == AdminRole.SUPER_ADMIN,
                    AdminUser.is_active.is_(True),
                    AdminUser.id != admin.id,
                )
            ).scalar_one()
        )
        if others == 0:
            # Otherwise the platform could be left with no account able to create
            # another one, which is only recoverable by direct database access.
            raise DomainError("last_super_admin")


def normalize_email(email: str) -> str:
    """Lowercase and trim, so one account cannot be registered twice."""
    normalized = email.strip().lower()
    if not _looks_like_email(normalized):
        raise DomainError("invalid_email")
    return normalized


def _looks_like_email(value: str) -> bool:
    if not value or len(value) > EMAIL_MAX_LENGTH or value.count("@") != 1:
        return False
    local, _, domain = value.partition("@")
    return bool(local) and "." in domain and not domain.startswith(".") and " " not in value
