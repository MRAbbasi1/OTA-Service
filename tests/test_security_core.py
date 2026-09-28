"""Unit tests for the security primitives and the production configuration guards.

These are the pieces where a subtle mistake is invisible in normal use: a token
that accepts `alg: none`, a hash comparison that leaks whether an account exists,
or a production deployment that silently starts with a development secret.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from pydantic import ValidationError

from app.core.config import (
    DEVELOPMENT_JWT_SECRET,
    MINIMUM_JWT_SECRET_LENGTH,
    Settings,
)
from app.core.security import (
    JWT_ALGORITHM,
    JWT_AUDIENCE,
    JWT_ISSUER,
    PASSWORD_MIN_LENGTH,
    create_access_token,
    csrf_tokens_match,
    decode_access_token,
    hash_password,
    new_csrf_token,
    password_needs_rehash,
    validate_password_strength,
    verify_password,
)
from app.db.models import AdminRole
from app.domain.errors import DomainError

SECRET = "a-test-signing-secret-of-at-least-32-chars"
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def base_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "development",
        "database_url": "postgresql+psycopg://user:pass@localhost:5432/db",
        "minio_endpoint": "localhost:9000",
        "minio_access_key": "key",
        "minio_secret_key": "secret",
        "ota_manifest_base_url": "https://api.ota-service.example",
        "ota_firmware_base_url": "https://cdn.ota-service.example",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


class TestPasswords:
    def test_a_password_is_never_recoverable_from_its_hash(self) -> None:
        digest = hash_password("a-long-enough-password")

        assert digest.startswith("$argon2id$")
        assert "a-long-enough-password" not in digest
        assert verify_password("a-long-enough-password", digest) is True
        assert verify_password("a-long-enough-passwerd", digest) is False
        assert password_needs_rehash(digest) is False

    def test_an_absent_hash_never_verifies_but_still_does_the_work(self) -> None:
        """Timing must not reveal whether an account exists."""
        assert verify_password("anything-at-all", None) is False
        assert verify_password("anything-at-all", "") is False

    def test_a_corrupt_hash_is_a_failure_not_a_crash(self) -> None:
        assert verify_password("x", "not-a-hash") is False
        assert password_needs_rehash("not-a-hash") is True

    @pytest.mark.parametrize(
        "password",
        [
            "too-short",
            " " * (PASSWORD_MIN_LENGTH + 1),
            "has-a-trailing-space ",
            "x" * 200,
        ],
    )
    def test_weak_passwords_are_rejected(self, password: str) -> None:
        with pytest.raises(DomainError) as exc:
            validate_password_strength(password)
        assert exc.value.code == "weak_password"

    def test_a_long_passphrase_is_accepted(self) -> None:
        validate_password_strength("correct horse battery staple")


class TestSessionTokens:
    def test_a_token_round_trips_with_its_claims(self) -> None:
        issued = datetime.now(UTC)
        token, expires_at = create_access_token(
            admin_id=7,
            role=AdminRole.FLEET_MANAGER,
            session_version=3,
            secret=SECRET,
            ttl_minutes=30,
            now=issued,
        )

        session = decode_access_token(token, secret=SECRET)

        assert session.admin_id == 7
        assert session.role is AdminRole.FLEET_MANAGER
        assert session.session_version == 3
        # `exp` is a numeric date, so it carries whole seconds only.
        assert abs((session.expires_at - expires_at).total_seconds()) < 1
        assert abs((expires_at - (issued + timedelta(minutes=30))).total_seconds()) < 1

    def test_a_token_signed_with_another_secret_is_rejected(self) -> None:
        token, _ = create_access_token(
            admin_id=1,
            role=AdminRole.VIEWER,
            session_version=1,
            secret="another-secret-of-sufficient-length",
            ttl_minutes=30,
        )

        with pytest.raises(DomainError) as exc:
            decode_access_token(token, secret=SECRET)
        assert exc.value.code == "invalid_session"

    def test_an_expired_token_is_rejected(self) -> None:
        token, _ = create_access_token(
            admin_id=1,
            role=AdminRole.VIEWER,
            session_version=1,
            secret=SECRET,
            ttl_minutes=30,
            now=NOW - timedelta(hours=2),
        )

        with pytest.raises(DomainError):
            decode_access_token(token, secret=SECRET)

    def test_the_unsigned_algorithm_is_not_accepted(self) -> None:
        """The classic JWT bypass: `alg: none` with an empty key."""
        forged = jwt.encode(
            {
                "sub": "1",
                "role": "super_admin",
                "sv": 1,
                "iat": NOW,
                "exp": NOW + timedelta(minutes=30),
                "iss": JWT_ISSUER,
                "aud": JWT_AUDIENCE,
            },
            key="",
            algorithm="none",
        )

        with pytest.raises(DomainError):
            decode_access_token(forged, secret=SECRET)

    def test_a_token_for_another_audience_is_rejected(self) -> None:
        foreign = jwt.encode(
            {
                "sub": "1",
                "role": "super_admin",
                "sv": 1,
                "iat": NOW,
                "exp": NOW + timedelta(minutes=30),
                "iss": JWT_ISSUER,
                "aud": "some-other-service",
            },
            SECRET,
            algorithm=JWT_ALGORITHM,
        )

        with pytest.raises(DomainError):
            decode_access_token(foreign, secret=SECRET)

    @pytest.mark.parametrize("missing", ["exp", "iat", "sub", "sv"])
    def test_required_claims_cannot_be_omitted(self, missing: str) -> None:
        claims = {
            "sub": "1",
            "role": "viewer",
            "sv": 1,
            "iat": NOW,
            "exp": NOW + timedelta(minutes=30),
            "iss": JWT_ISSUER,
            "aud": JWT_AUDIENCE,
        }
        del claims[missing]
        token = jwt.encode(claims, SECRET, algorithm=JWT_ALGORITHM)

        with pytest.raises(DomainError):
            decode_access_token(token, secret=SECRET)

    def test_an_unknown_role_claim_is_rejected(self) -> None:
        token = jwt.encode(
            {
                "sub": "1",
                "role": "root",
                "sv": 1,
                "iat": NOW,
                "exp": NOW + timedelta(minutes=30),
                "iss": JWT_ISSUER,
                "aud": JWT_AUDIENCE,
            },
            SECRET,
            algorithm=JWT_ALGORITHM,
        )

        with pytest.raises(DomainError):
            decode_access_token(token, secret=SECRET)


class TestCsrf:
    def test_a_csrf_token_is_unpredictable_and_compared_exactly(self) -> None:
        first, second = new_csrf_token(), new_csrf_token()

        assert first != second
        assert len(first) >= 32
        assert csrf_tokens_match(first, first) is True
        assert csrf_tokens_match(first, second) is False
        assert csrf_tokens_match(None, first) is False
        assert csrf_tokens_match(first, None) is False
        assert csrf_tokens_match("", "") is False


class TestProductionGuards:
    def test_rejects_legacy_branding_in_the_openapi_title(self) -> None:
        with pytest.raises(ValidationError) as exc:
            base_settings(app_name="Bently OTA Service")
        assert "APP_NAME" in str(exc.value)

    def test_production_refuses_the_development_session_secret(self) -> None:
        with pytest.raises(ValidationError) as exc:
            base_settings(environment="production", minio_secure=True)
        assert "ADMIN_JWT_SECRET" in str(exc.value)

    def test_production_refuses_a_short_session_secret(self) -> None:
        with pytest.raises(ValidationError) as exc:
            base_settings(
                environment="production",
                minio_secure=True,
                admin_jwt_secret="short-secret",
            )
        assert "ADMIN_JWT_SECRET" in str(exc.value)

    def test_production_refuses_plain_http_and_minio_without_tls(self) -> None:
        with pytest.raises(ValidationError):
            base_settings(
                environment="production",
                minio_secure=True,
                admin_jwt_secret="x" * MINIMUM_JWT_SECRET_LENGTH,
                ota_manifest_base_url="http://api.ota-service.example",
            )
        with pytest.raises(ValidationError):
            base_settings(
                environment="production",
                admin_jwt_secret="x" * MINIMUM_JWT_SECRET_LENGTH,
            )

    def test_production_refuses_an_insecure_cookie_override(self) -> None:
        with pytest.raises(ValidationError) as exc:
            base_settings(
                environment="production",
                minio_secure=True,
                admin_jwt_secret="x" * MINIMUM_JWT_SECRET_LENGTH,
                admin_cookie_secure=False,
            )
        assert "ADMIN_COOKIE_SECURE" in str(exc.value)

    def test_secure_cookies_are_derived_from_the_environment(self) -> None:
        production = base_settings(
            environment="production",
            minio_secure=True,
            admin_jwt_secret="x" * MINIMUM_JWT_SECRET_LENGTH,
        )
        development = base_settings()

        assert production.secure_admin_cookies is True
        assert development.secure_admin_cookies is False
        # An explicit value is still honoured, which matters behind a proxy that
        # terminates TLS before the application.
        assert base_settings(admin_cookie_secure=True).secure_admin_cookies is True

    def test_development_accepts_the_template_defaults(self) -> None:
        settings = base_settings()

        assert settings.admin_jwt_secret.get_secret_value() == DEVELOPMENT_JWT_SECRET
        assert settings.admin_csrf_header == "X-CSRF-Token"
