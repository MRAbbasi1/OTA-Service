"""Administrative authentication and account management.

Login is the only unauthenticated administrative endpoint. It is rate limited,
returns one generic error for every failure reason, and records both the success
and the failure in the audit trail with the *attempted* email as the actor — never
the password (`docs/08-security.md` §16).

There is no user-registration endpoint and no default account: the first
`SUPER_ADMIN` comes from the deployment command, and further accounts are created
by a `SUPER_ADMIN` here.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.api.v1.deps import get_rate_limiter, get_session, get_settings
from app.api.v1.errors import raise_domain_error
from app.api.v1.schemas import (
    AdminActiveUpdate,
    AdminLogin,
    AdminPasswordChange,
    AdminRoleUpdate,
    AdminSessionRead,
    AdminUserCreate,
    AdminUserRead,
)
from app.api.v1.security import (
    AdminsManage,
    Authenticated,
    clear_session_cookies,
    enforce_csrf,
    enforce_login_rate_limit,
    new_csrf_pair,
    set_session_cookies,
)
from app.core.config import Settings
from app.db.models import AdminUser
from app.domain.errors import DomainError
from app.domain.rbac import capabilities_for
from app.services.admin_auth import AdminAuthService
from app.services.admin_users import AdminUserService
from app.services.audit import AuditService
from app.services.rate_limit import InMemoryRateLimiter

logger = logging.getLogger(__name__)

auth_router = APIRouter(prefix="/api/v1/admin/auth", tags=["admin-auth"])
admins_router = APIRouter(prefix="/api/v1/admin/admins", tags=["admin-admins"])


def admin_read(admin: AdminUser) -> AdminUserRead:
    return AdminUserRead(
        id=admin.id,
        email=admin.email,
        role=admin.role,
        is_active=admin.is_active,
        last_login_at=admin.last_login_at,
        created_at=admin.created_at,
        capabilities=capabilities_for(admin.role),
    )


@auth_router.post("/login")
def login(
    payload: AdminLogin,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    limiter: InMemoryRateLimiter = Depends(get_rate_limiter),
) -> AdminSessionRead:
    """Exchange credentials for a session cookie.

    Every failure — unknown account, wrong password, deactivated account — is the
    same `401`, and the account is hashed anyway, so the endpoint cannot be used
    to enumerate administrators.
    """
    enforce_login_rate_limit(request, payload.email, limiter, settings)
    service = AdminAuthService(session, settings=settings)
    try:
        admin, token, expires_at = service.login(payload.email, payload.password)
    except DomainError as exc:
        session.rollback()
        AuditService(session).record(
            payload.email.strip().lower(),
            "admin_login_failed",
            "admin_user",
            None,
            {"reason": exc.code},
        )
        session.commit()
        logger.warning(
            "admin_login_failed",
            extra={
                "extra_fields": {"address": request.client.host if request.client else "unknown"}
            },
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "invalid_credentials"}
        ) from None

    AuditService(session).record(admin.email, "admin_login", "admin_user", admin.id)
    session.commit()

    csrf_token = new_csrf_pair()
    set_session_cookies(
        response,
        settings=settings,
        token=token,
        csrf_token=csrf_token,
        max_age_seconds=settings.admin_access_token_ttl_minutes * 60,
    )
    return AdminSessionRead(
        admin=admin_read(admin),
        csrf_token=csrf_token,
        expires_at=expires_at,
        csrf_header=settings.admin_csrf_header,
    )


@auth_router.post("/logout")
def logout(
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    """Clear the session cookies, whether or not the session was still valid.

    Deliberately not `401` when the session has already expired: a client whose
    `HttpOnly` cookie is stale must still be able to get rid of it.
    """
    enforce_csrf(request, settings)
    token = request.cookies.get(settings.admin_session_cookie)
    if token:
        try:
            admin = AdminAuthService(session, settings=settings).resolve(token)
        except DomainError:
            admin = None
        if admin is not None:
            AuditService(session).record(admin.email, "admin_logout", "admin_user", admin.id)
            session.commit()
    clear_session_cookies(response, settings=settings)
    return {"status": "logged_out"}


@auth_router.get("/me")
def me(admin: Authenticated) -> AdminUserRead:
    """The current account, with the capabilities its role grants."""
    return admin_read(admin)


@auth_router.post("/password")
def change_own_password(
    payload: AdminPasswordChange,
    admin: Authenticated,
    session: Session = Depends(get_session),
) -> AdminUserRead:
    """Change the caller's own password; every other session ends as a result."""
    service = AdminUserService(session)
    try:
        updated = service.change_own_password(
            admin.id,
            current_password=payload.current_password,
            new_password=payload.new_password,
        )
        session.commit()
    except DomainError as exc:
        session.rollback()
        raise_domain_error(exc)
    AuditService(session).record(updated.email, "admin_password_changed", "admin_user", updated.id)
    session.commit()
    return admin_read(updated)


@admins_router.get("")
def list_admins(
    admin: AdminsManage,
    page: int = 1,
    page_size: int = 50,
    session: Session = Depends(get_session),
) -> dict[str, object]:
    admins, total = AdminUserService(session).list_admins(page=page, page_size=page_size)
    return {
        "items": [admin_read(row) for row in admins],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@admins_router.post("", status_code=status.HTTP_201_CREATED)
def create_admin(
    payload: AdminUserCreate,
    admin: AdminsManage,
    session: Session = Depends(get_session),
) -> AdminUserRead:
    """Create an administrative account. Audited, and no password is ever logged."""
    service = AdminUserService(session)
    try:
        created = service.create(email=payload.email, password=payload.password, role=payload.role)
        session.commit()
    except DomainError as exc:
        session.rollback()
        raise_domain_error(exc)
    AuditService(session).record(
        admin.email,
        "admin_created",
        "admin_user",
        created.id,
        {"email": created.email, "role": created.role.value},
    )
    session.commit()
    return admin_read(created)


@admins_router.post("/{admin_id}/active")
def set_admin_active(
    admin_id: int,
    payload: AdminActiveUpdate,
    admin: AdminsManage,
    session: Session = Depends(get_session),
) -> AdminUserRead:
    service = AdminUserService(session)
    try:
        updated = service.set_active(admin_id, payload.is_active)
        session.commit()
    except DomainError as exc:
        session.rollback()
        raise_domain_error(exc)
    AuditService(session).record(
        admin.email,
        "admin_activated" if payload.is_active else "admin_deactivated",
        "admin_user",
        admin_id,
    )
    session.commit()
    return admin_read(updated)


@admins_router.post("/{admin_id}/role")
def set_admin_role(
    admin_id: int,
    payload: AdminRoleUpdate,
    admin: AdminsManage,
    session: Session = Depends(get_session),
) -> AdminUserRead:
    service = AdminUserService(session)
    try:
        updated = service.set_role(admin_id, payload.role)
        session.commit()
    except DomainError as exc:
        session.rollback()
        raise_domain_error(exc)
    AuditService(session).record(
        admin.email,
        "admin_role_changed",
        "admin_user",
        admin_id,
        {"role": updated.role.value},
    )
    session.commit()
    return admin_read(updated)
