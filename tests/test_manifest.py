from __future__ import annotations

import base64
import json

import pytest

from app.domain.errors import DomainError
from app.domain.manifest import (
    MAX_ARTIFACT_SIZE,
    MAX_MANIFEST_BYTES,
    is_der_ecdsa_signature,
    parse_manifest,
    serialize_manifest,
    signed_payload,
)

# The exact manifest documented in docs/OtaManager.md §10 (wire-level example),
# used here as a fixed structural vector. No cryptographic verification is
# possible yet: the production public key lives in the firmware repository.
DOCUMENTED_MANIFEST = (
    b'{"version":"2.5.0",'
    b'"url":"https://cdn.ota-service.example/firmware/bcs-controller/esp32-s3/v2.5.0/Controller.ino.bin",'
    b'"md5":"0898589d83793b639bf736f3e3d222c2",'
    b'"size":1240720,'
    b'"signature":"MEUCIA3pjuDY7q215q351W/FTH8qtm2EVWQVL51GRpU2YK1DAiEA+ptKGh8j1ziQ9+8KfTsJAsqMqUvDCWbFxKtu4ctf39E="}'
)


def manifest_bytes(**overrides: object) -> bytes:
    document = json.loads(DOCUMENTED_MANIFEST)
    document.update(overrides)
    return json.dumps(document, separators=(",", ":")).encode()


def test_documented_manifest_parses() -> None:
    manifest = parse_manifest(DOCUMENTED_MANIFEST)
    assert manifest.version == "2.5.0"
    assert manifest.size == 1240720
    assert manifest.md5 == "0898589d83793b639bf736f3e3d222c2"
    assert manifest.raw == DOCUMENTED_MANIFEST
    assert len(manifest.signature_bytes) == 71
    assert is_der_ecdsa_signature(manifest.signature_bytes)


def test_signed_payload_is_exact() -> None:
    assert signed_payload("2.6.1", "https://cdn/x.bin", "a" * 32, 1240720) == (
        b"2.6.1|https://cdn/x.bin|" + b"a" * 32 + b"|1240720"
    )


# A minimal DER ECDSA signature (SEQUENCE of two INTEGERs), structurally valid.
MINIMAL_DER = base64.b64encode(b"\x30\x06\x02\x01\x01\x02\x01\x01").decode()


def test_serialize_round_trips() -> None:
    raw = serialize_manifest(
        "2.6.1", "https://cdn.ota-service.example/f", "b" * 32, 42, MINIMAL_DER
    )
    assert raw == (
        b'{"version":"2.6.1","url":"https://cdn.ota-service.example/f","md5":"' + b"b" * 32 + b'",'
        b'"size":42,"signature":"' + MINIMAL_DER.encode() + b'"}'
    )
    assert parse_manifest(raw).size == 42


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not json",
        b"[]",
        b'"string"',
        b"{}",
        b'{"version":"a","url":"b","md5":"c","size":1}',
        manifest_bytes(extra="x"),
        manifest_bytes(version="v2.5.0"),
        manifest_bytes(version="10000.2000.30000"),  # 16 characters
        manifest_bytes(version="2.5"),
        manifest_bytes(url=""),
        manifest_bytes(url="/relative/path.bin"),
        manifest_bytes(url="ftp://cdn/x.bin"),
        manifest_bytes(url="https://cdn/" + "a" * 96),  # >= 96 characters
        manifest_bytes(md5="0898589D83793B639BF736F3E3D222C2"),
        manifest_bytes(md5="0898589d"),
        manifest_bytes(size="1240720"),
        manifest_bytes(size=True),
        manifest_bytes(size=0),
        manifest_bytes(size=MAX_ARTIFACT_SIZE + 1),
        b"x" * (MAX_MANIFEST_BYTES + 1),
    ],
)
def test_invalid_manifests_are_rejected(raw: bytes) -> None:
    with pytest.raises(DomainError) as excinfo:
        parse_manifest(raw)
    assert excinfo.value.code == "manifest_invalid"


def test_max_artifact_size_is_accepted() -> None:
    assert parse_manifest(manifest_bytes(size=MAX_ARTIFACT_SIZE)).size == MAX_ARTIFACT_SIZE


@pytest.mark.parametrize("signature", [None, "", 123])
def test_missing_signature_is_reported_distinctly(signature: object) -> None:
    with pytest.raises(DomainError) as excinfo:
        parse_manifest(manifest_bytes(signature=signature))
    assert excinfo.value.code == "missing_signature"


@pytest.mark.parametrize(
    "signature",
    [
        "not base64!!",
        base64.b64encode(b"\x01\x02\x03").decode(),
        base64.b64encode(bytes(range(64))).decode(),  # raw r||s, not DER
        base64.b64encode(b"\x30\x45\x02").decode(),  # truncated DER
        base64.b64encode(b"\x30\x06\x02\x01\x01\x02\x01\x01\x00").decode(),  # trailing byte
        base64.b64encode(b"\x30\x06\x02\x01\x01\x03\x01\x01").decode(),  # wrong tag for s
    ],
)
def test_structurally_invalid_signatures_are_rejected(signature: str) -> None:
    with pytest.raises(DomainError) as excinfo:
        parse_manifest(manifest_bytes(signature=signature))
    assert excinfo.value.code == "invalid_signature"
