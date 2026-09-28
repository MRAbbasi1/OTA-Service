from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AnyUrl, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Development-only values. Production refuses to start with either of them, so a
# deployment cannot silently inherit an insecure default from a copied `.env`.
DEVELOPMENT_JWT_SECRET = "development-only-secret-do-not-use-in-production"
MINIMUM_JWT_SECRET_LENGTH = 32


class Settings(BaseSettings):
    """Typed runtime configuration loaded from environment variables and `.env`."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: Literal["development", "testing", "production"] = "development"
    app_name: str = "OTA-Service"
    app_version: str = "0.1.0"
    log_level: str = "INFO"

    database_url: str
    database_pool_size: int = Field(default=5, ge=1)
    database_max_overflow: int = Field(default=10, ge=0)
    database_pool_timeout_seconds: int = Field(default=30, ge=1, le=300)

    minio_endpoint: str
    minio_access_key: SecretStr
    minio_secret_key: SecretStr
    minio_bucket: str = "ota-firmware"
    minio_region: str = "us-east-1"
    minio_secure: bool = False

    # Manifest retrieval and firmware delivery are separate public hostnames (see
    # docs/16-update-path-and-publication.md). The manifest host is provisioned on
    # devices and is hard to change in the field; the delivery host is expected to
    # evolve, so both are composed from configuration rather than hardcoded.
    ota_manifest_base_url: AnyUrl
    ota_firmware_base_url: AnyUrl

    # Administrative authentication (docs/08-security.md §§6-7).
    admin_jwt_secret: SecretStr = SecretStr(DEVELOPMENT_JWT_SECRET)
    admin_access_token_ttl_minutes: int = Field(default=30, ge=1, le=1440)
    admin_session_cookie: str = "ota_admin_session"
    admin_csrf_cookie: str = "ota_admin_csrf"
    admin_csrf_header: str = "X-CSRF-Token"
    # `None` derives from the environment: production can only be served over
    # HTTPS, so the session cookie is `Secure` there and only there. An explicit
    # value is honoured, except that production refuses `false`.
    admin_cookie_secure: bool | None = None

    @property
    def secure_admin_cookies(self) -> bool:
        if self.admin_cookie_secure is not None:
            return self.admin_cookie_secure
        return self.environment == "production"

    @model_validator(mode="after")
    def validate_production_security(self) -> Settings:
        if any(term in self.app_name.casefold() for term in ("bently", "bentley")):
            raise ValueError("APP_NAME must not contain legacy branding")
        if self.environment != "production":
            return self
        if not self.minio_secure:
            raise ValueError("MINIO_SECURE must be true in production")
        for name, base_url in (
            ("OTA_MANIFEST_BASE_URL", self.ota_manifest_base_url),
            ("OTA_FIRMWARE_BASE_URL", self.ota_firmware_base_url),
        ):
            if base_url.scheme != "https":
                raise ValueError(f"{name} must use HTTPS in production")
        if self.admin_cookie_secure is False:
            raise ValueError("ADMIN_COOKIE_SECURE must not be false in production")
        secret = self.admin_jwt_secret.get_secret_value()
        if secret == DEVELOPMENT_JWT_SECRET or len(secret) < MINIMUM_JWT_SECRET_LENGTH:
            raise ValueError(
                f"ADMIN_JWT_SECRET must be set to a random value of at least "
                f"{MINIMUM_JWT_SECRET_LENGTH} characters in production"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    # Required values are loaded from the environment / `.env`, not constructor args.
    return Settings()  # type: ignore[call-arg]
