"""Administrative credential and session primitives.

Three separate mechanisms live here, and they are not interchangeable:

* **Passwords** are hashed with Argon2id (`argon2-cffi`) and never logged,
  returned, or stored in plaintext.
* **Sessions** are short-lived JSON Web Tokens signed with HS256. The token is
  carried in an `HttpOnly` cookie, so browser JavaScript cannot read it, and it
  pins the issuer, the audience, an expiry, and the account's `session_version`,
  which is what makes revocation immediate rather than "within TTL"
  (`docs/08-security.md` §§5-6).
* **CSRF** uses the double-submit pattern: a random value in a cookie that is
  deliberately *not* `HttpOnly`, echoed in a request header, so a cross-site
  request (which cannot read the cookie) cannot produce a matching pair.

Verification is deliberately strict: one allowed algorithm, required claims, and
a small clock leeway. Anything unexpected is a failure, never a fallback.
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.db.models import AdminRole
from app.domain.errors import DomainError

#: Argon2id with the library's defaults (OWASP-acceptable cost parameters).
_password_hasher = PasswordHasher()

#: A real hash of a throwaway value, used to equalise timing when the requested
#: account does not exist. Without it, a fast rejection reveals which emails are
#: registered.
_TIMING_EQUALISER_HASH = _password_hasher.hash("timing-equaliser-not-a-password")

PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 128

JWT_ALGORITHM = "HS256"
JWT_ISSUER = "ota-service"
JWT_AUDIENCE = "ota-service-admin"
JWT_LEEWAY_SECONDS = 10


def hash_password(password: str) -> str:
    """Return an Argon2id hash. The plaintext never leaves this function."""
    return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """Verify a password, doing the same work whether or not an account exists.

    An absent hash is still verified against a throwaway hash, so an unknown
    account takes as long as a wrong password and the endpoint cannot be timed to
    discover which emails are registered. A missing hash can only ever return
    ``False``.
    """
    try:
        _password_hasher.verify(password_hash or _TIMING_EQUALISER_HASH, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return password_hash is not None


def password_needs_rehash(password_hash: str) -> bool:
    """Whether a stored hash predates the current cost parameters."""
    try:
        return _password_hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def validate_password_strength(password: str) -> None:
    """Reject weak administrative passwords.

    Length is the only enforced rule: composition requirements push operators
    toward predictable substitutions, and a long passphrase is measurably
    stronger (`docs/08-security.md` §6).
    """
    if len(password) < PASSWORD_MIN_LENGTH or len(password) > PASSWORD_MAX_LENGTH:
        raise DomainError("weak_password")
    if password.strip() != password or not password.isprintable():
        raise DomainError("weak_password")


@dataclass(frozen=True)
class AdminSession:
    """The verified content of an access token."""

    admin_id: int
    role: AdminRole
    session_version: int
    expires_at: datetime


def create_access_token(
    *,
    admin_id: int,
    role: AdminRole,
    session_version: int,
    secret: str,
    ttl_minutes: int,
    now: datetime | None = None,
) -> tuple[str, datetime]:
    """Sign a session token and report when it expires."""
    issued_at = now or datetime.now(UTC)
    expires_at = issued_at + timedelta(minutes=ttl_minutes)
    token = jwt.encode(
        {
            "sub": str(admin_id),
            "role": role.value,
            "sv": session_version,
            "iat": issued_at,
            "exp": expires_at,
            "iss": JWT_ISSUER,
            "aud": JWT_AUDIENCE,
        },
        secret,
        algorithm=JWT_ALGORITHM,
    )
    return token, expires_at


def decode_access_token(token: str, *, secret: str) -> AdminSession:
    """Verify a session token or raise ``invalid_session``."""
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[JWT_ALGORITHM],
            issuer=JWT_ISSUER,
            audience=JWT_AUDIENCE,
            leeway=JWT_LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "sub", "sv"]},
        )
    except jwt.PyJWTError:
        raise DomainError("invalid_session") from None

    try:
        role = AdminRole(claims["role"])
        admin_id = int(claims["sub"])
        session_version = int(claims["sv"])
    except (KeyError, TypeError, ValueError):
        raise DomainError("invalid_session") from None
    return AdminSession(
        admin_id=admin_id,
        role=role,
        session_version=session_version,
        expires_at=datetime.fromtimestamp(claims["exp"], tz=UTC),
    )


def new_csrf_token() -> str:
    """A fresh CSRF secret for one browser session."""
    return secrets.token_urlsafe(32)


def csrf_tokens_match(candidate: str | None, expected: str | None) -> bool:
    """Constant-time comparison of the header value and the cookie value."""
    if not candidate or not expected:
        return False
    return hmac.compare_digest(candidate, expected)
