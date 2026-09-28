from __future__ import annotations

from collections.abc import Generator

from fastapi import Request
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.domain.manifest import ManifestVerifier
from app.services.firmware import FirmwareService
from app.services.policies import PolicyService
from app.services.rate_limit import InMemoryRateLimiter
from app.services.update_decision import UpdateDecisionService
from app.storage.base import ObjectStorage


def get_session(request: Request) -> Generator[Session]:
    factory = request.app.state.session_factory
    session: Session = factory()
    try:
        yield session
    finally:
        session.close()


def get_storage(request: Request) -> ObjectStorage:
    storage: ObjectStorage = request.app.state.storage
    return storage


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_manifest_verifier(request: Request) -> ManifestVerifier | None:
    verifier: ManifestVerifier | None = request.app.state.manifest_verifier
    return verifier


def get_rate_limiter(request: Request) -> InMemoryRateLimiter:
    limiter: InMemoryRateLimiter = request.app.state.rate_limiter
    return limiter


def build_firmware_service(
    session: Session,
    storage: ObjectStorage,
    settings: Settings,
    verifier: ManifestVerifier | None,
) -> FirmwareService:
    """The one place `FirmwareService` is constructed.

    Both surfaces need identical path, URL, and validation rules: the admin upload
    path, and the device manifest endpoint, which resolves and serves the release
    the same way the upload recorded it.
    """
    return FirmwareService(
        session,
        storage,
        manifest_base_url=str(settings.ota_manifest_base_url),
        firmware_base_url=str(settings.ota_firmware_base_url),
        bucket=settings.minio_bucket,
        verifier=verifier,
    )


def build_update_decision_service(
    session: Session, firmware: FirmwareService
) -> UpdateDecisionService:
    """The one place eligibility is assembled.

    Both the device endpoint and the dashboard preview call this, which is what
    makes "the same device state yields the same decision" true by construction
    rather than by two implementations agreeing.
    """
    return UpdateDecisionService(session, firmware, PolicyService(session))
