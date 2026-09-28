"""Firmware release management.

Publication is the only path by which a device can receive a version, so this
service is the single gate that decides whether an uploaded binary and manifest
are a publishable pair. All device-facing paths and object keys come from
``app.domain.update_path``; all manifest rules come from ``app.domain.manifest``.

The signing key belongs to the release pipeline, not to this service: the
pipeline produces the manifest and its signature, and the platform stores and
serves those bytes verbatim. Only the device verifies the signature, using the
public key embedded in its firmware. A :class:`ManifestVerifier` may be supplied
by a deployment that wants the extra guard of checking the signature before a
release is accepted; when one is supplied, verification is mandatory, and when
none is, the manifest is stored with ``signature_verified=False``.

Procedure and rejection codes: ``docs/16-update-path-and-publication.md`` §8.

Immutability is structural rather than enforced by update methods: there is no
way to replace an artifact or a manifest of an existing release, so a changed
binary can only ever arrive as a new version.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.db.models import (
    DeviceType,
    FirmwareArtifact,
    FirmwareManifest,
    FirmwareRelease,
    FirmwareReleaseStatus,
)
from app.domain.errors import DomainError
from app.domain.manifest import (
    MAX_ARTIFACT_SIZE,
    Manifest,
    ManifestVerifier,
    parse_manifest,
    signed_payload,
)
from app.domain.update_path import (
    firmware_url,
    manifest_storage_key,
    storage_key,
    validate_filename,
    validate_version,
)
from app.storage.base import ObjectStorage

logger = logging.getLogger(__name__)

ARTIFACT_CONTENT_TYPE = "application/octet-stream"
MANIFEST_CONTENT_TYPE = "application/json"

SIGNATURE_SOURCE_UPLOADED = "uploaded"
SIGNATURE_SOURCE_GENERATED = "generated"

# Only a release that passed the full gate may be published.
PUBLISHABLE_STATUSES = (FirmwareReleaseStatus.VALIDATED, FirmwareReleaseStatus.DRAFT)
# A deprecated release is no longer offered as the normal latest release, but it
# stays deliverable by exact version: a device already handed a signed manifest
# must be able to finish that download, and pinned devices may still use it
# (docs/06-firmware-management.md §12). An archived release is not delivered.
DELIVERABLE_STATUSES = (FirmwareReleaseStatus.PUBLISHED, FirmwareReleaseStatus.DEPRECATED)
ARCHIVABLE_STATUSES = (
    FirmwareReleaseStatus.VALIDATED,
    FirmwareReleaseStatus.DRAFT,
    FirmwareReleaseStatus.PUBLISHED,
    FirmwareReleaseStatus.DEPRECATED,
)


@dataclass(frozen=True)
class ArtifactUpload:
    filename: str
    data: bytes
    content_type: str = ARTIFACT_CONTENT_TYPE


class FirmwareService:
    def __init__(
        self,
        session: Session,
        storage: ObjectStorage,
        *,
        manifest_base_url: str,
        firmware_base_url: str,
        bucket: str,
        verifier: ManifestVerifier | None = None,
    ) -> None:
        self._session = session
        self._storage = storage
        self._manifest_base_url = manifest_base_url
        self._firmware_base_url = firmware_base_url
        self._bucket = bucket
        self._verifier = verifier

    # --- creation -----------------------------------------------------------------

    def create_release(
        self,
        *,
        device_type_id: int,
        version: str,
        artifact: ArtifactUpload,
        manifest_bytes: bytes | None = None,
        name: str | None = None,
        release_notes: str | None = None,
        created_by: str = "admin",
    ) -> FirmwareRelease:
        """Validate an upload and store it at the derived keys.

        The object writes and the metadata commit are owned here (not by the
        caller) so that a failure has exactly one compensating action: MinIO and
        PostgreSQL are not one atomic transaction (``docs/10-storage.md`` §9).

        A release uploaded without a manifest is stored as ``UPLOADED`` and
        cannot be published until a signed manifest exists for it.
        """
        device_type = self._device_type(device_type_id)
        code, platform = device_type.code, device_type.platform

        validate_version(version)
        validate_filename(artifact.filename)
        self._assert_version_available(device_type_id, version)

        data = artifact.data
        if not data:
            raise DomainError("artifact_empty")
        if len(data) > MAX_ARTIFACT_SIZE:
            raise DomainError("artifact_too_large")

        # MD5 exists for compatibility with the device OTA implementation; SHA-256
        # is the backend-side integrity hash (docs/06-firmware-management.md §6).
        md5 = hashlib.md5(data).hexdigest()
        sha256 = hashlib.sha256(data).hexdigest()
        url = firmware_url(self._firmware_base_url, code, platform, version, artifact.filename)
        artifact_key = storage_key(code, platform, version, artifact.filename)

        pending_manifest = self._prepare_manifest(
            manifest_bytes,
            version=version,
            md5=md5,
            size=len(data),
            url=url,
            code=code,
            platform=platform,
        )

        release = FirmwareRelease(
            device_type_id=device_type_id,
            version=version,
            name=name,
            release_notes=release_notes,
            status=FirmwareReleaseStatus.UPLOADED,
            created_by=created_by,
        )
        self._session.add(release)
        self._session.flush()

        written: list[str] = []
        try:
            written.append(artifact_key)
            self._storage.put_bytes(artifact_key, data, artifact.content_type)
            if pending_manifest is not None:
                manifest_key, manifest_raw, _ = pending_manifest
                self._storage.put_bytes(manifest_key, manifest_raw, MANIFEST_CONTENT_TYPE)
                written.append(manifest_key)
            self._assert_stored_integrity(
                artifact_key,
                expected_size=len(data),
                expected_md5=md5,
            )

            artifact_row = FirmwareArtifact(
                storage_bucket=self._bucket,
                storage_key=artifact_key,
                filename=artifact.filename,
                content_type=artifact.content_type,
                size=len(data),
                md5=md5,
                sha256=sha256,
            )
            release.artifact = artifact_row
            if pending_manifest is not None:
                _, _, manifest_row = pending_manifest
                # The manifest is bound to the artifact whose bytes and hashes it
                # signed, so flush first to obtain the artifact primary key.
                self._session.flush()
                manifest_row.artifact_id = artifact_row.id
                release.manifest = manifest_row
                release.status = FirmwareReleaseStatus.VALIDATED

            self._session.commit()
        except Exception:
            self._session.rollback()
            self._discard_objects(written)
            raise

        return release

    # --- reads --------------------------------------------------------------------

    def get_release(self, release_id: int) -> FirmwareRelease:
        release = self._session.get(FirmwareRelease, release_id)
        if release is None:
            raise DomainError("release_not_found")
        return release

    def list_releases(
        self,
        *,
        device_type_id: int | None = None,
        release_status: FirmwareReleaseStatus | None = None,
        version: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[FirmwareRelease], int]:
        conditions = []
        if device_type_id is not None:
            conditions.append(FirmwareRelease.device_type_id == device_type_id)
        if release_status is not None:
            conditions.append(FirmwareRelease.status == release_status)
        if version:
            conditions.append(FirmwareRelease.version == version)
        stmt = select(FirmwareRelease)
        if conditions:
            stmt = stmt.where(*conditions)
        total = self._session.execute(
            select(func.count()).select_from(stmt.subquery())
        ).scalar_one()
        rows = self._session.execute(
            stmt.options(
                selectinload(FirmwareRelease.device_type),
                selectinload(FirmwareRelease.artifact),
                selectinload(FirmwareRelease.manifest),
            )
            .order_by(FirmwareRelease.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).scalars()
        return list(rows), int(total)

    def get_artifact(self, release_id: int) -> FirmwareArtifact:
        artifact = self._session.execute(
            select(FirmwareArtifact).where(FirmwareArtifact.release_id == release_id)
        ).scalar_one_or_none()
        if artifact is None:
            raise DomainError("artifact_missing")
        return artifact

    def published_releases(self, device_type_id: int) -> list[FirmwareRelease]:
        """Every ``PUBLISHED`` release of one device type, unordered.

        This is the candidate set for eligibility: deprecated and archived
        releases are never offered, and no release of another device type can
        enter it, so a policy can only narrow what this type publishes and never
        widen it. Selection and ordering belong to the decision service, which
        compares parsed versions because string ordering would rank ``2.10.0``
        below ``2.9.0``.
        """
        rows = self._session.execute(
            select(FirmwareRelease)
            .where(
                FirmwareRelease.device_type_id == device_type_id,
                FirmwareRelease.status == FirmwareReleaseStatus.PUBLISHED,
            )
            .options(
                selectinload(FirmwareRelease.device_type),
                selectinload(FirmwareRelease.artifact),
                selectinload(FirmwareRelease.manifest),
            )
        ).scalars()
        return list(rows)

    def published_by_version(self, device_type_id: int, version: str) -> FirmwareRelease | None:
        """Resolve a specifically requested deliverable release of a device type.

        Used by the firmware endpoint: the signed manifest is the authorization
        for one exact URL, so the download resolves that release rather than the
        current offer, which lets an in-flight download survive a release being
        deprecated or a newer one being published mid-download.
        """
        return self._session.execute(
            select(FirmwareRelease)
            .where(
                FirmwareRelease.device_type_id == device_type_id,
                FirmwareRelease.version == version,
                FirmwareRelease.status.in_(DELIVERABLE_STATUSES),
            )
            .options(
                selectinload(FirmwareRelease.device_type),
                selectinload(FirmwareRelease.artifact),
                selectinload(FirmwareRelease.manifest),
            )
        ).scalar_one_or_none()

    def get_manifest(self, release_id: int) -> FirmwareManifest:
        manifest = self._session.execute(
            select(FirmwareManifest).where(FirmwareManifest.release_id == release_id)
        ).scalar_one_or_none()
        if manifest is None:
            raise DomainError("manifest_required")
        return manifest

    def stored_manifest_bytes(self, release_id: int) -> bytes:
        """The exact bytes that will be served to devices."""
        manifest = self.get_manifest(release_id)
        return self._storage.get_bytes(manifest.storage_key)

    # --- lifecycle ----------------------------------------------------------------

    def mark_draft(self, release_id: int) -> FirmwareRelease:
        release = self.get_release(release_id)
        if release.status is not FirmwareReleaseStatus.VALIDATED:
            raise DomainError("invalid_release_transition")
        release.status = FirmwareReleaseStatus.DRAFT
        self._session.flush()
        return release

    def publish_release(self, release_id: int) -> FirmwareRelease:
        release = self.get_release(release_id)
        if release.status not in PUBLISHABLE_STATUSES:
            raise DomainError("invalid_release_transition")

        artifact = self.get_artifact(release_id)
        manifest = self.get_manifest(release_id)
        if self._verifier is not None and not manifest.signature_verified:
            # This deployment verifies signatures, so a manifest that was stored
            # without that check must be re-uploaded rather than published.
            raise DomainError("signature_unverified")
        if self._verifier is None:
            logger.warning(
                "publishing_without_signature_verification",
                extra={
                    "extra_fields": {
                        "release_id": release.id,
                        "version": release.version,
                        "reason": "no_verifier_configured",
                    }
                },
            )
        self._assert_artifact_intact(artifact)

        release.status = FirmwareReleaseStatus.PUBLISHED
        release.published_at = datetime.now(UTC)
        self._session.flush()
        return release

    def deprecate_release(self, release_id: int) -> FirmwareRelease:
        release = self.get_release(release_id)
        if release.status is not FirmwareReleaseStatus.PUBLISHED:
            raise DomainError("invalid_release_transition")
        release.status = FirmwareReleaseStatus.DEPRECATED
        self._session.flush()
        return release

    def archive_release(self, release_id: int) -> FirmwareRelease:
        release = self.get_release(release_id)
        if release.status not in ARCHIVABLE_STATUSES:
            raise DomainError("invalid_release_transition")
        release.status = FirmwareReleaseStatus.ARCHIVED
        self._session.flush()
        return release

    # --- internals ----------------------------------------------------------------

    def _device_type(self, device_type_id: int) -> DeviceType:
        device_type = self._session.get(DeviceType, device_type_id)
        if device_type is None:
            raise DomainError("device_type_not_found")
        if not device_type.is_active:
            raise DomainError("device_type_inactive")
        return device_type

    def _assert_version_available(self, device_type_id: int, version: str) -> None:
        existing = self._session.execute(
            select(FirmwareRelease.id).where(
                FirmwareRelease.device_type_id == device_type_id,
                FirmwareRelease.version == version,
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise DomainError("version_exists")

    def _prepare_manifest(
        self,
        manifest_bytes: bytes | None,
        *,
        version: str,
        md5: str,
        size: int,
        url: str,
        code: str,
        platform: str,
    ) -> tuple[str, bytes, FirmwareManifest] | None:
        """Parse, cross-check, and signature-gate an uploaded manifest.

        The uploaded manifest is a claim to verify, never configuration: its
        ``url`` must equal the URL this backend composed, because the signature
        covers that URL.
        """
        if manifest_bytes is None:
            return None

        manifest = parse_manifest(manifest_bytes)
        self._cross_check(manifest, version=version, md5=md5, size=size, url=url)
        signature_verified = self._verify_signature(manifest)

        key = manifest_storage_key(code, platform, version)
        row = FirmwareManifest(
            version=manifest.version,
            url=manifest.url,
            md5=manifest.md5,
            size=manifest.size,
            signature=manifest.signature,
            storage_key=key,
            manifest_sha256=hashlib.sha256(manifest.raw).hexdigest(),
            signature_source=SIGNATURE_SOURCE_UPLOADED,
            signature_verified=signature_verified,
        )
        return key, manifest.raw, row

    def _cross_check(
        self, manifest: Manifest, *, version: str, md5: str, size: int, url: str
    ) -> None:
        if manifest.version != version:
            raise DomainError("manifest_version_mismatch")
        if manifest.md5 != md5:
            raise DomainError("manifest_md5_mismatch")
        if manifest.size != size:
            raise DomainError("manifest_size_mismatch")
        if manifest.url != url:
            raise DomainError("manifest_url_mismatch")

    def _verify_signature(self, manifest: Manifest) -> bool:
        if self._verifier is None:
            # The device is the only verifier the contract requires; it holds the
            # embedded public key. Server-side verification is an optional
            # deployment guard, so an unverified manifest is recorded as such
            # rather than rejected.
            logger.warning(
                "manifest_signature_not_verified",
                extra={"extra_fields": {"version": manifest.version, "reason": "no_verifier"}},
            )
            return False
        payload = signed_payload(manifest.version, manifest.url, manifest.md5, manifest.size)
        if not self._verifier.verify(payload, manifest.signature_bytes):
            raise DomainError("invalid_signature")
        return True

    def _assert_stored_integrity(self, key: str, *, expected_size: int, expected_md5: str) -> None:
        stored = self._storage.get_bytes(key)
        if len(stored) != expected_size or hashlib.md5(stored).hexdigest() != expected_md5:
            raise DomainError("storage_verification_failed")

    def _assert_artifact_intact(self, artifact: FirmwareArtifact) -> None:
        """Re-read the object before publishing: a published release must be servable."""
        stored = self._storage.get_bytes(artifact.storage_key)
        if len(stored) != artifact.size or hashlib.md5(stored).hexdigest() != artifact.md5:
            raise DomainError("artifact_mismatch")

    def _discard_objects(self, keys: list[str]) -> None:
        for key in keys:
            try:
                self._storage.remove(key)
            except Exception:  # noqa: BLE001 - cleanup must never mask the original failure
                logger.warning(
                    "object_cleanup_failed",
                    extra={"extra_fields": {"storage_key": key}},
                )
