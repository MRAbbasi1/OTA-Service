"""Administrative request security.

One dependency factory, :func:`require`, is the whole enforcement point for every
administrative route. It resolves the session cookie, then applies, in order:

```text
authentication   -> 401 when the session is missing, invalid, or revoked
rate limiting    -> 429, keyed by the authenticated account
CSRF             -> 403 on an unsafe method without a matching header/cookie pair
authorization    -> 403 when the account's role lacks the required capability
```

Routes then declare the capability they need as an annotated parameter
(`admin: DevicesWrite`), which both enforces it and gives the handler the actor to
put in the audit trail — the previous literal `"admin"` actor recorded that
*something* happened, not who did it.

Failures are deliberately uniform: an unauthenticated request always sees the
same `401 unauthenticated` body, so the surface cannot be used to probe for
accounts, and the specific reason goes to the log instead.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.api.v1.deps import get_rate_limiter, get_session, get_settings
from app.core.config import Settings
from app.core.security import csrf_tokens_match, new_csrf_token
from app.db.models import AdminUser
from app.domain.errors import DomainError
from app.domain.rbac import Capability, has_capability
from app.services.admin_auth import AdminAuthService
from app.services.rate_limit import (
    ADMIN_API_RATE_LIMIT,
    ADMIN_API_SURFACE,
    ADMIN_LOGIN_RATE_LIMIT,
    ADMIN_LOGIN_SURFACE,
    InMemoryRateLimiter,
)

logger = logging.getLogger(__name__)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: Cookie attributes shared by the session and CSRF cookies. `SameSite=Lax` is
#: what stops a cross-site form post from arriving with cookies at all; the
#: double-submit token below is the second, independent layer.
COOKIE_SAMESITE: Literal["lax", "strict", "none"] = "lax"
COOKIE_PATH = "/"


def build_admin_auth_service(session: Session, settings: Settings) -> AdminAuthService:
    return AdminAuthService(session, settings=settings)


def unauthenticated() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "unauthenticated"},
        headers={"WWW-Authenticate": "Cookie"},
    )


def insufficient_capability(capability: Capability) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"code": "insufficient_capability", "required": capability.value},
    )


def resolve_admin(request: Request, session: Session, settings: Settings) -> AdminUser:
    """Authenticate the request's session cookie, or raise ``401``."""
    token = request.cookies.get(settings.admin_session_cookie)
    try:
        admin = build_admin_auth_service(session, settings).resolve(token)
    except DomainError as exc:
        logger.info(
            "admin_session_rejected",
            extra={"extra_fields": {"code": exc.code, "path": request.url.path}},
        )
        raise unauthenticated() from None
    return admin


def enforce_csrf(request: Request, settings: Settings) -> None:
    """Reject an unsafe method whose header does not match the CSRF cookie.

    A request that carries neither cookie is not checked: there is no credential
    in it for a cross-site page to ride on. As soon as either cookie is present
    the pair must match, which is what a cross-site request cannot produce.
    """
    if request.method in SAFE_METHODS:
        return
    cookie = request.cookies.get(settings.admin_csrf_cookie)
    if not cookie and not request.cookies.get(settings.admin_session_cookie):
        return
    header = request.headers.get(settings.admin_csrf_header)
    if csrf_tokens_match(header, cookie):
        return
    logger.warning(
        "admin_csrf_rejected",
        extra={"extra_fields": {"path": request.url.path, "method": request.method}},
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"code": "csrf_failed", "header": settings.admin_csrf_header},
    )


def enforce_admin_rate_limit(admin: AdminUser, limiter: InMemoryRateLimiter) -> None:
    if limiter.allow(f"{ADMIN_API_SURFACE}:{admin.id}", ADMIN_API_RATE_LIMIT):
        return
    logger.warning(
        "admin_rate_limited",
        extra={"extra_fields": {"admin_id": admin.id, "surface": ADMIN_API_SURFACE}},
    )
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail={"code": "rate_limited"}
    )


def guard(
    request: Request,
    session: Session,
    settings: Settings,
    limiter: InMemoryRateLimiter,
    capability: Capability | None = None,
) -> AdminUser:
    """Authenticate, throttle, check CSRF, and optionally check a capability."""
    admin = resolve_admin(request, session, settings)
    enforce_admin_rate_limit(admin, limiter)
    enforce_csrf(request, settings)
    if capability is None or has_capability(admin.role, capability):
        return admin
    logger.warning(
        "admin_capability_denied",
        extra={
            "extra_fields": {
                "admin_id": admin.id,
                "role": admin.role.value,
                "capability": capability.value,
                "path": request.url.path,
            }
        },
    )
    raise insufficient_capability(capability)


def authenticated_admin(
    request: Request,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    limiter: InMemoryRateLimiter = Depends(get_rate_limiter),
) -> AdminUser:
    """Any active administrator: used where no specific capability applies."""
    return guard(request, session, settings, limiter)


def require(capability: Capability) -> Callable[..., AdminUser]:
    """Build the dependency that guards one capability."""

    def dependency(
        request: Request,
        session: Session = Depends(get_session),
        settings: Settings = Depends(get_settings),
        limiter: InMemoryRateLimiter = Depends(get_rate_limiter),
    ) -> AdminUser:
        return guard(request, session, settings, limiter, capability)

    return dependency


#: Every administrative endpoint must take one of these, so authentication is not
#: something a route can forget to opt into.
Authenticated = Annotated[AdminUser, Depends(authenticated_admin)]


# One alias per capability, so a route's requirement is visible in its signature
# and cannot be forgotten in a decorator list.
DashboardRead = Annotated[AdminUser, Depends(require(Capability.DASHBOARD_READ))]
DevicesRead = Annotated[AdminUser, Depends(require(Capability.DEVICES_READ))]
DevicesWrite = Annotated[AdminUser, Depends(require(Capability.DEVICES_WRITE))]
FirmwareRead = Annotated[AdminUser, Depends(require(Capability.FIRMWARE_READ))]
FirmwareWrite = Annotated[AdminUser, Depends(require(Capability.FIRMWARE_WRITE))]
PoliciesRead = Annotated[AdminUser, Depends(require(Capability.POLICIES_READ))]
PoliciesWrite = Annotated[AdminUser, Depends(require(Capability.POLICIES_WRITE))]
AuditRead = Annotated[AdminUser, Depends(require(Capability.AUDIT_READ))]
AdminsManage = Annotated[AdminUser, Depends(require(Capability.ADMINS_MANAGE))]


def enforce_login_rate_limit(
    request: Request, email: str, limiter: InMemoryRateLimiter, settings: Settings
) -> None:
    """Throttle credential attempts by source address *and* submitted email.

    Keying by both means a shared address is not a shared lockout, and one
    account cannot be attacked from many addresses at full speed within a single
    process either. The address comes from the transport peer, not from a
    client-supplied header, because a spoofable key would defeat the limit.
    """
    address = request.client.host if request.client is not None else "unknown"
    key = f"{ADMIN_LOGIN_SURFACE}:{address}:{email.strip().lower()}"
    if limiter.allow(key, ADMIN_LOGIN_RATE_LIMIT):
        return
    logger.warning("admin_login_rate_limited", extra={"extra_fields": {"address": address}})
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail={"code": "rate_limited"}
    )


def set_session_cookies(
    response: Response,
    *,
    settings: Settings,
    token: str,
    csrf_token: str,
    max_age_seconds: int,
) -> None:
    """Write the session cookie (unreadable by JS) and the CSRF cookie (readable)."""
    response.set_cookie(
        settings.admin_session_cookie,
        token,
        max_age=max_age_seconds,
        httponly=True,
        secure=settings.secure_admin_cookies,
        samesite=COOKIE_SAMESITE,
        path=COOKIE_PATH,
    )
    response.set_cookie(
        settings.admin_csrf_cookie,
        csrf_token,
        max_age=max_age_seconds,
        httponly=False,
        secure=settings.secure_admin_cookies,
        samesite=COOKIE_SAMESITE,
        path=COOKIE_PATH,
    )


def clear_session_cookies(response: Response, *, settings: Settings) -> None:
    response.delete_cookie(
        settings.admin_session_cookie, path=COOKIE_PATH, samesite=COOKIE_SAMESITE
    )
    response.delete_cookie(settings.admin_csrf_cookie, path=COOKIE_PATH, samesite=COOKIE_SAMESITE)


def new_csrf_pair() -> str:
    """A CSRF secret for one browser session, echoed in the response body."""
    return new_csrf_token()
