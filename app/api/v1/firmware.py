"""Admin firmware endpoints.

Upload accepts the binary and the signed manifest together so the platform can
reject a manifest whose signed fields disagree with the bytes it was handed,
rather than publishing a pairing no device can install. Everything else —
storage keys, the download URL, and the manifest URL — is derived, never
accepted from the request.

Rejection codes are defined in `docs/16-update-path-and-publication.md` §8.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile, status
from sqlalchemy.orm import Session

from app.api.v1.deps import (
    build_firmware_service,
    get_manifest_verifier,
    get_session,
    get_settings,
    get_storage,
)
from app.api.v1.errors import raise_domain_error
from app.api.v1.schemas import (
    FirmwareArtifactRead,
    FirmwareManifestRead,
    FirmwareReleaseDetail,
    FirmwareReleaseRead,
)
from app.api.v1.security import FirmwareRead, FirmwareWrite
from app.core.config import Settings
from app.db.models import FirmwareRelease, FirmwareReleaseStatus
from app.domain.errors import DomainError
from app.domain.manifest import MAX_ARTIFACT_SIZE, MAX_MANIFEST_BYTES, ManifestVerifier
from app.domain.update_path import (
    firmware_url,
    manifest_url,
    release_storage_prefix,
)
from app.services.audit import AuditService
from app.services.firmware import ArtifactUpload, FirmwareService
from app.storage.base import ObjectStorage

router = APIRouter(prefix="/api/v1/admin/firmware", tags=["firmware"])

# One byte past the limit, so an oversized body is rejected without buffering it all.
MAX_ARTIFACT_UPLOAD_BYTES = MAX_ARTIFACT_SIZE + 1
MAX_MANIFEST_UPLOAD_BYTES = MAX_MANIFEST_BYTES + 1


def _service(
    session: Session,
    storage: ObjectStorage,
    settings: Settings,
    verifier: ManifestVerifier | None,
) -> FirmwareService:
    return build_firmware_service(session, storage, settings, verifier)


def _release_read(release: FirmwareRelease, settings: Settings) -> FirmwareReleaseRead:
    code = release.device_type.code
    platform = release.device_type.platform
    return FirmwareReleaseRead(
        id=release.id,
        device_type_id=release.device_type_id,
        device_type_code=code,
        platform=platform,
        version=release.version,
        name=release.name,
        release_notes=release.release_notes,
        status=release.status,
        created_by=release.created_by,
        created_at=release.created_at,
        published_at=release.published_at,
        manifest_url=manifest_url(str(settings.ota_manifest_base_url), code, platform),
        storage_prefix=release_storage_prefix(code, platform, release.version),
    )


def _release_detail(release: FirmwareRelease, settings: Settings) -> FirmwareReleaseDetail:
    base = _release_read(release, settings)
    artifact = release.artifact
    manifest = release.manifest
    return FirmwareReleaseDetail(
        **base.model_dump(),
        artifact=None
        if artifact is None
        else FirmwareArtifactRead(
            filename=artifact.filename,
            content_type=artifact.content_type,
            size=artifact.size,
            md5=artifact.md5,
            sha256=artifact.sha256,
            storage_bucket=artifact.storage_bucket,
            storage_key=artifact.storage_key,
        ),
        manifest=None
        if manifest is None
        else FirmwareManifestRead(
            version=manifest.version,
            url=manifest.url,
            md5=manifest.md5,
            size=manifest.size,
            signature=manifest.signature,
            storage_key=manifest.storage_key,
            manifest_sha256=manifest.manifest_sha256,
            signature_source=manifest.signature_source,
            signature_verified=manifest.signature_verified,
            validated_at=manifest.validated_at,
        ),
        firmware_url=None
        if artifact is None
        else firmware_url(
            str(settings.ota_firmware_base_url),
            release.device_type.code,
            release.device_type.platform,
            release.version,
            artifact.filename,
        ),
    )


@router.post("/releases", status_code=status.HTTP_201_CREATED)
def create_release(
    device_type_id: Annotated[int, Form()],
    version: Annotated[str, Form(max_length=32)],
    artifact: Annotated[UploadFile, File()],
    admin: FirmwareWrite,
    manifest: Annotated[UploadFile | None, File()] = None,
    name: Annotated[str | None, Form(max_length=128)] = None,
    release_notes: Annotated[str | None, Form()] = None,
    session: Session = Depends(get_session),
    storage: ObjectStorage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
    verifier: ManifestVerifier | None = Depends(get_manifest_verifier),
) -> FirmwareReleaseDetail:
    artifact_data = artifact.file.read(MAX_ARTIFACT_UPLOAD_BYTES)
    manifest_data = manifest.file.read(MAX_MANIFEST_UPLOAD_BYTES) if manifest is not None else None
    if manifest_data == b"":  # an unfilled optional part is not a manifest
        manifest_data = None

    service = _service(session, storage, settings, verifier)
    try:
        release = service.create_release(
            device_type_id=device_type_id,
            version=version,
            artifact=ArtifactUpload(filename=artifact.filename or "", data=artifact_data),
            manifest_bytes=manifest_data,
            name=name,
            release_notes=release_notes,
            created_by=admin.email,
        )
    except DomainError as exc:
        session.rollback()
        raise_domain_error(exc)

    AuditService(session).record(
        admin.email,
        "firmware_uploaded",
        "firmware_release",
        release.id,
        {
            "device_type_id": release.device_type_id,
            "version": release.version,
            "size": release.artifact.size if release.artifact else None,
            "md5": release.artifact.md5 if release.artifact else None,
            "sha256": release.artifact.sha256 if release.artifact else None,
            "storage_key": release.artifact.storage_key if release.artifact else None,
        },
    )
    session.commit()
    return _release_detail(release, settings)


@router.get("/releases")
def list_releases(
    admin: FirmwareRead,
    device_type_id: int | None = None,
    release_status: FirmwareReleaseStatus | None = Query(default=None, alias="status"),
    version: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
    storage: ObjectStorage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
    verifier: ManifestVerifier | None = Depends(get_manifest_verifier),
) -> dict[str, object]:
    releases, total = _service(session, storage, settings, verifier).list_releases(
        device_type_id=device_type_id,
        release_status=release_status,
        version=version,
        page=page,
        page_size=page_size,
    )
    return {
        "items": [_release_read(release, settings) for release in releases],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/releases/{release_id}")
def get_release(
    release_id: int,
    admin: FirmwareRead,
    session: Session = Depends(get_session),
    storage: ObjectStorage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
    verifier: ManifestVerifier | None = Depends(get_manifest_verifier),
) -> FirmwareReleaseDetail:
    try:
        release = _service(session, storage, settings, verifier).get_release(release_id)
    except DomainError as exc:
        raise_domain_error(exc)
    return _release_detail(release, settings)


@router.get("/releases/{release_id}/manifest")
def get_manifest_bytes(
    release_id: int,
    admin: FirmwareRead,
    session: Session = Depends(get_session),
    storage: ObjectStorage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
    verifier: ManifestVerifier | None = Depends(get_manifest_verifier),
) -> Response:
    """The exact stored manifest bytes that a device will be served."""
    try:
        raw = _service(session, storage, settings, verifier).stored_manifest_bytes(release_id)
    except DomainError as exc:
        raise_domain_error(exc)
    return Response(content=raw, media_type="application/json")


def _transition(
    release_id: int,
    action: str,
    audit_action: str,
    actor: str,
    session: Session,
    storage: ObjectStorage,
    settings: Settings,
    verifier: ManifestVerifier | None,
) -> FirmwareReleaseDetail:
    service = _service(session, storage, settings, verifier)
    try:
        release = getattr(service, action)(release_id)
    except DomainError as exc:
        session.rollback()
        raise_domain_error(exc)
    AuditService(session).record(
        actor, audit_action, "firmware_release", release_id, {"version": release.version}
    )
    session.commit()
    return _release_detail(release, settings)


@router.post("/releases/{release_id}/draft")
def mark_draft(
    release_id: int,
    admin: FirmwareWrite,
    session: Session = Depends(get_session),
    storage: ObjectStorage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
    verifier: ManifestVerifier | None = Depends(get_manifest_verifier),
) -> FirmwareReleaseDetail:
    return _transition(
        release_id,
        "mark_draft",
        "firmware_drafted",
        admin.email,
        session,
        storage,
        settings,
        verifier,
    )


@router.post("/releases/{release_id}/publish")
def publish_release(
    release_id: int,
    admin: FirmwareWrite,
    session: Session = Depends(get_session),
    storage: ObjectStorage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
    verifier: ManifestVerifier | None = Depends(get_manifest_verifier),
) -> FirmwareReleaseDetail:
    return _transition(
        release_id,
        "publish_release",
        "firmware_published",
        admin.email,
        session,
        storage,
        settings,
        verifier,
    )


@router.post("/releases/{release_id}/deprecate")
def deprecate_release(
    release_id: int,
    admin: FirmwareWrite,
    session: Session = Depends(get_session),
    storage: ObjectStorage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
    verifier: ManifestVerifier | None = Depends(get_manifest_verifier),
) -> FirmwareReleaseDetail:
    return _transition(
        release_id,
        "deprecate_release",
        "firmware_deprecated",
        admin.email,
        session,
        storage,
        settings,
        verifier,
    )


@router.post("/releases/{release_id}/archive")
def archive_release(
    release_id: int,
    admin: FirmwareWrite,
    session: Session = Depends(get_session),
    storage: ObjectStorage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
    verifier: ManifestVerifier | None = Depends(get_manifest_verifier),
) -> FirmwareReleaseDetail:
    return _transition(
        release_id,
        "archive_release",
        "firmware_archived",
        admin.email,
        session,
        storage,
        settings,
        verifier,
    )
