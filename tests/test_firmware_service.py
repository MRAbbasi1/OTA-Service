from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    DeviceType,
    FirmwareRelease,
    FirmwareReleaseStatus,
)
from app.domain.errors import DomainError
from app.services.firmware import ArtifactUpload, FirmwareService

CODE = "bcs-controller-v1"
PLATFORM = "esp32-s3"
VERSION = "2.6.1"
FILENAME = "Controller.ino.bin"
BINARY = b"firmware-bytes" * 64

MANIFEST_BASE = "https://api.ota-service.example"
FIRMWARE_BASE = "https://cdn.ota-service.example"
BUCKET = "ota-firmware"

# Structurally valid DER ECDSA signature (the documented sample from
# docs/OtaManager.md §10). Content-independent: the structural screen does not
# look at the payload, and cryptographic verification needs the production key.
SIGNATURE = (
    "MEUCIA3pjuDY7q215q351W/FTH8qtm2EVWQVL51GRpU2YK1DAiEA+ptKGh8j1ziQ9+8KfTsJAsqMqUvDCWb"
    "FxKtu4ctf39E="
)


class AcceptingVerifier:
    def __init__(self) -> None:
        self.payloads: list[bytes] = []

    def verify(self, payload: bytes, signature: bytes) -> bool:
        self.payloads.append(payload)
        return True


class RejectingVerifier:
    def verify(self, payload: bytes, signature: bytes) -> bool:
        return False


class WrappedStorage:
    """Delegating wrapper used to inject storage failures."""

    def __init__(self, inner: Any, *, fail_on_put: int | None = None, truncate: bool = False):
        self._inner = inner
        self._fail_on_put = fail_on_put
        self._truncate = truncate
        self._puts = 0

    def put_bytes(self, object_name: str, data: bytes, content_type: str) -> None:
        self._puts += 1
        if self._fail_on_put is not None and self._puts == self._fail_on_put:
            raise RuntimeError("object storage unavailable")
        self._inner.put_bytes(object_name, data[:-1] if self._truncate else data, content_type)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def manifest_payload(binary: bytes = BINARY, **overrides: Any) -> bytes:
    document = {
        "version": overrides.pop("version", VERSION),
        "url": overrides.pop("url", firmware_url()),
        "md5": overrides.pop("md5", hashlib.md5(binary).hexdigest()),
        "size": overrides.pop("size", len(binary)),
        "signature": overrides.pop("signature", SIGNATURE),
    }
    document.update(overrides)
    return json.dumps(document, separators=(",", ":")).encode()


def firmware_url(version: str = VERSION, filename: str = FILENAME) -> str:
    return f"{FIRMWARE_BASE}/firmware/{CODE}/{PLATFORM}/v{version}/{filename}"


def storage_key(filename: str = FILENAME, version: str = VERSION) -> str:
    return f"firmware/{CODE}/{PLATFORM}/v{version}/{filename}"


def make_device_type(session: Session, *, is_active: bool = True) -> DeviceType:
    device_type = DeviceType(
        code=CODE, name="Smart Controller", platform=PLATFORM, is_active=is_active
    )
    session.add(device_type)
    session.commit()
    return device_type


def make_service(
    session: Session,
    storage: Any,
    verifier: Any = None,
) -> FirmwareService:
    return FirmwareService(
        session,
        storage,
        manifest_base_url=MANIFEST_BASE,
        firmware_base_url=FIRMWARE_BASE,
        bucket=BUCKET,
        verifier=verifier,
    )


@pytest.fixture
def session(committing_session: Session) -> Iterator[Session]:
    yield committing_session


def test_upload_creates_validated_release(session: Session, fake_storage: Any) -> None:
    device_type = make_device_type(session)
    verifier = AcceptingVerifier()

    release = make_service(session, fake_storage, verifier).create_release(
        device_type_id=device_type.id,
        version=VERSION,
        artifact=ArtifactUpload(filename=FILENAME, data=BINARY),
        manifest_bytes=manifest_payload(),
        created_by="firmware-manager",
    )

    assert release.status is FirmwareReleaseStatus.VALIDATED
    assert release.published_at is None
    assert release.artifact is not None
    assert release.artifact.storage_key == storage_key()
    assert release.artifact.storage_bucket == BUCKET
    assert release.artifact.size == len(BINARY)
    assert release.artifact.md5 == hashlib.md5(BINARY).hexdigest()
    assert release.artifact.sha256 == hashlib.sha256(BINARY).hexdigest()

    manifest = release.manifest
    assert manifest is not None
    assert manifest.url == firmware_url()
    assert manifest.version == VERSION
    assert manifest.signature_verified is True
    assert manifest.storage_key == f"firmware/{CODE}/{PLATFORM}/v{VERSION}/manifest.json"
    assert manifest.manifest_sha256 == hashlib.sha256(manifest_payload()).hexdigest()

    # Objects are stored at keys that mirror the download URL.
    assert set(fake_storage.objects) == {storage_key(), manifest.storage_key}
    assert fake_storage.objects[storage_key()] == BINARY
    assert fake_storage.objects[manifest.storage_key] == manifest_payload()

    # The signed payload is the exact documented format.
    assert verifier.payloads == [
        f"{VERSION}|{firmware_url()}|{hashlib.md5(BINARY).hexdigest()}|{len(BINARY)}".encode()
    ]


def test_stored_manifest_bytes_are_the_published_bytes(session: Session, fake_storage: Any) -> None:
    device_type = make_device_type(session)
    service = make_service(session, fake_storage, AcceptingVerifier())
    release = service.create_release(
        device_type_id=device_type.id,
        version=VERSION,
        artifact=ArtifactUpload(filename=FILENAME, data=BINARY),
        manifest_bytes=manifest_payload(),
    )
    assert service.stored_manifest_bytes(release.id) == manifest_payload()


def test_upload_without_manifest_cannot_be_published(session: Session, fake_storage: Any) -> None:
    device_type = make_device_type(session)
    service = make_service(session, fake_storage, AcceptingVerifier())
    release = service.create_release(
        device_type_id=device_type.id,
        version=VERSION,
        artifact=ArtifactUpload(filename=FILENAME, data=BINARY),
    )
    assert release.status is FirmwareReleaseStatus.UPLOADED
    assert release.manifest is None

    with pytest.raises(DomainError) as excinfo:
        service.stored_manifest_bytes(release.id)
    assert excinfo.value.code == "manifest_required"

    with pytest.raises(DomainError) as excinfo:
        service.publish_release(release.id)
    assert excinfo.value.code == "invalid_release_transition"


@pytest.mark.parametrize(
    ("artifact", "expected_code"),
    [
        (ArtifactUpload(filename=FILENAME, data=b""), "artifact_empty"),
        (
            ArtifactUpload(filename=FILENAME, data=b"x" * (4 * 1024 * 1024 + 1)),
            "artifact_too_large",
        ),
        (ArtifactUpload(filename="../evil.bin", data=BINARY), "invalid_filename"),
        (ArtifactUpload(filename=".hidden", data=BINARY), "invalid_filename"),
    ],
)
def test_artifact_rejections(
    session: Session, fake_storage: Any, artifact: Any, expected_code: str
) -> None:
    device_type = make_device_type(session)
    with pytest.raises(DomainError) as excinfo:
        make_service(session, fake_storage).create_release(
            device_type_id=device_type.id,
            version=VERSION,
            artifact=artifact,
            manifest_bytes=manifest_payload(),
        )
    assert excinfo.value.code == expected_code
    assert fake_storage.objects == {}


def test_invalid_version_is_rejected(session: Session, fake_storage: Any) -> None:
    device_type = make_device_type(session)
    with pytest.raises(DomainError) as excinfo:
        make_service(session, fake_storage).create_release(
            device_type_id=device_type.id,
            version="v2.6.1",
            artifact=ArtifactUpload(filename=FILENAME, data=BINARY),
        )
    assert excinfo.value.code == "invalid_version"


def test_duplicate_version_is_a_conflict(session: Session, fake_storage: Any) -> None:
    device_type = make_device_type(session)
    service = make_service(session, fake_storage, AcceptingVerifier())
    service.create_release(
        device_type_id=device_type.id,
        version=VERSION,
        artifact=ArtifactUpload(filename=FILENAME, data=BINARY),
        manifest_bytes=manifest_payload(),
    )
    with pytest.raises(DomainError) as excinfo:
        service.create_release(
            device_type_id=device_type.id,
            version=VERSION,
            artifact=ArtifactUpload(filename=FILENAME, data=BINARY),
            manifest_bytes=manifest_payload(),
        )
    assert excinfo.value.code == "version_exists"


def test_missing_or_inactive_device_type(session: Session, fake_storage: Any) -> None:
    with pytest.raises(DomainError) as excinfo:
        make_service(session, fake_storage).create_release(
            device_type_id=4242,
            version=VERSION,
            artifact=ArtifactUpload(filename=FILENAME, data=BINARY),
        )
    assert excinfo.value.code == "device_type_not_found"

    inactive = make_device_type(session, is_active=False)
    with pytest.raises(DomainError) as excinfo:
        make_service(session, fake_storage).create_release(
            device_type_id=inactive.id,
            version=VERSION,
            artifact=ArtifactUpload(filename=FILENAME, data=BINARY),
        )
    assert excinfo.value.code == "device_type_inactive"


@pytest.mark.parametrize(
    ("manifest", "expected_code"),
    [
        (b"not json", "manifest_invalid"),
        (json.dumps({}, separators=(",", ":")).encode(), "manifest_invalid"),
        (manifest_payload(extra_key="x"), "manifest_invalid"),
        (manifest_payload(url="https://cdn.example.com/other.bin"), "manifest_url_mismatch"),
        (manifest_payload(version="2.6.2"), "manifest_version_mismatch"),
        (manifest_payload(md5="0" * 32), "manifest_md5_mismatch"),
        (manifest_payload(size=len(BINARY) + 1), "manifest_size_mismatch"),
        (manifest_payload(signature=""), "missing_signature"),
        (manifest_payload(signature="%%%"), "invalid_signature"),
    ],
)
def test_manifest_cross_check_rejections(
    session: Session, fake_storage: Any, manifest: bytes, expected_code: str
) -> None:
    device_type = make_device_type(session)
    with pytest.raises(DomainError) as excinfo:
        make_service(session, fake_storage).create_release(
            device_type_id=device_type.id,
            version=VERSION,
            artifact=ArtifactUpload(filename=FILENAME, data=BINARY),
            manifest_bytes=manifest,
        )
    assert excinfo.value.code == expected_code
    assert fake_storage.objects == {}
    assert session.execute(select(FirmwareRelease)).scalars().all() == []


def test_rejecting_verifier_blocks_the_upload(session: Session, fake_storage: Any) -> None:
    device_type = make_device_type(session)
    with pytest.raises(DomainError) as excinfo:
        make_service(session, fake_storage, RejectingVerifier()).create_release(
            device_type_id=device_type.id,
            version=VERSION,
            artifact=ArtifactUpload(filename=FILENAME, data=BINARY),
            manifest_bytes=manifest_payload(),
        )
    assert excinfo.value.code == "invalid_signature"


def test_publish_without_a_verifier_is_allowed_and_recorded(
    session: Session, fake_storage: Any
) -> None:
    """The device verifies the signature; server-side verification is optional."""
    device_type = make_device_type(session)
    service = make_service(session, fake_storage)  # no verifier configured
    release = service.create_release(
        device_type_id=device_type.id,
        version=VERSION,
        artifact=ArtifactUpload(filename=FILENAME, data=BINARY),
        manifest_bytes=manifest_payload(),
    )
    assert release.status is FirmwareReleaseStatus.VALIDATED
    assert release.manifest is not None
    # Recorded as not independently verified, which is not the same as invalid.
    assert release.manifest.signature_verified is False

    published = service.publish_release(release.id)
    assert published.status is FirmwareReleaseStatus.PUBLISHED
    assert published.published_at is not None


def test_verifying_deployment_refuses_an_unverified_manifest(
    session: Session, fake_storage: Any
) -> None:
    """A release stored before verification was configured must be re-uploaded."""
    device_type = make_device_type(session)
    release = make_service(session, fake_storage).create_release(
        device_type_id=device_type.id,
        version=VERSION,
        artifact=ArtifactUpload(filename=FILENAME, data=BINARY),
        manifest_bytes=manifest_payload(),
    )

    with pytest.raises(DomainError) as excinfo:
        make_service(session, fake_storage, AcceptingVerifier()).publish_release(release.id)
    assert excinfo.value.code == "signature_unverified"


def test_publish_deprecate_archive_lifecycle(session: Session, fake_storage: Any) -> None:
    device_type = make_device_type(session)
    service = make_service(session, fake_storage, AcceptingVerifier())
    release = service.create_release(
        device_type_id=device_type.id,
        version=VERSION,
        artifact=ArtifactUpload(filename=FILENAME, data=BINARY),
        manifest_bytes=manifest_payload(),
    )

    released = service.mark_draft(release.id)
    assert released.status is FirmwareReleaseStatus.DRAFT

    published = service.publish_release(release.id)
    assert published.status is FirmwareReleaseStatus.PUBLISHED
    assert published.published_at is not None

    with pytest.raises(DomainError) as excinfo:
        service.publish_release(release.id)
    assert excinfo.value.code == "invalid_release_transition"

    assert service.deprecate_release(release.id).status is FirmwareReleaseStatus.DEPRECATED
    assert service.archive_release(release.id).status is FirmwareReleaseStatus.ARCHIVED
    with pytest.raises(DomainError) as excinfo:
        service.archive_release(release.id)
    assert excinfo.value.code == "invalid_release_transition"


def test_publish_refuses_a_tampered_artifact(session: Session, fake_storage: Any) -> None:
    device_type = make_device_type(session)
    service = make_service(session, fake_storage, AcceptingVerifier())
    release = service.create_release(
        device_type_id=device_type.id,
        version=VERSION,
        artifact=ArtifactUpload(filename=FILENAME, data=BINARY),
        manifest_bytes=manifest_payload(),
    )
    fake_storage.objects[storage_key()] = b"tampered"

    with pytest.raises(DomainError) as excinfo:
        service.publish_release(release.id)
    assert excinfo.value.code == "artifact_mismatch"


def test_upload_rejects_same_length_storage_corruption(session: Session, fake_storage: Any) -> None:
    device_type = make_device_type(session)
    storage = WrappedStorage(fake_storage)

    original_put = storage.put_bytes

    def corrupt_after_write(object_name: str, data: bytes, content_type: str) -> None:
        original_put(object_name, data, content_type)
        if object_name == storage_key():
            fake_storage.objects[object_name] = b"x" * len(data)

    storage.put_bytes = corrupt_after_write  # type: ignore[method-assign]

    with pytest.raises(DomainError) as excinfo:
        make_service(session, storage, AcceptingVerifier()).create_release(
            device_type_id=device_type.id,
            version=VERSION,
            artifact=ArtifactUpload(filename=FILENAME, data=BINARY),
            manifest_bytes=manifest_payload(),
        )

    assert excinfo.value.code == "storage_verification_failed"
    assert fake_storage.objects == {}


def test_storage_failure_leaves_no_metadata_and_no_objects(
    session: Session, fake_storage: Any
) -> None:
    device_type = make_device_type(session)
    storage = WrappedStorage(fake_storage, fail_on_put=2)  # the manifest write fails

    with pytest.raises(RuntimeError):
        make_service(session, storage, AcceptingVerifier()).create_release(
            device_type_id=device_type.id,
            version=VERSION,
            artifact=ArtifactUpload(filename=FILENAME, data=BINARY),
            manifest_bytes=manifest_payload(),
        )

    assert fake_storage.objects == {}
    assert fake_storage.removed == [storage_key()]
    assert session.execute(select(FirmwareRelease)).scalars().all() == []


def test_verification_failure_compensates(session: Session, fake_storage: Any) -> None:
    device_type = make_device_type(session)
    storage = WrappedStorage(fake_storage, truncate=True)  # stored length differs

    with pytest.raises(DomainError) as excinfo:
        make_service(session, storage, AcceptingVerifier()).create_release(
            device_type_id=device_type.id,
            version=VERSION,
            artifact=ArtifactUpload(filename=FILENAME, data=BINARY),
            manifest_bytes=manifest_payload(),
        )
    assert excinfo.value.code == "storage_verification_failed"
    assert fake_storage.objects == {}
    assert session.execute(select(FirmwareRelease)).scalars().all() == []


def test_release_not_found(session: Session, fake_storage: Any) -> None:
    service = make_service(session, fake_storage)
    with pytest.raises(DomainError) as excinfo:
        service.get_release(9999)
    assert excinfo.value.code == "release_not_found"


def test_list_releases_filters(session: Session, fake_storage: Any) -> None:
    device_type = make_device_type(session)
    service = make_service(session, fake_storage, AcceptingVerifier())
    for version in ("2.6.0", "2.6.1"):
        service.create_release(
            device_type_id=device_type.id,
            version=version,
            artifact=ArtifactUpload(filename=FILENAME, data=BINARY),
            manifest_bytes=manifest_payload(version=version, url=firmware_url(version)),
        )

    rows, total = service.list_releases(device_type_id=device_type.id)
    assert total == 2
    assert [row.version for row in rows] == ["2.6.1", "2.6.0"]

    rows, total = service.list_releases(
        device_type_id=device_type.id, release_status=FirmwareReleaseStatus.PUBLISHED
    )
    assert (rows, total) == ([], 0)

    rows, total = service.list_releases(version="2.6.0")
    assert total == 1
    assert rows[0].artifact is not None
