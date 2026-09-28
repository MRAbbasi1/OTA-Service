"""Signed OTA manifest rules.

The device verifies exactly one payload shape over one exact JSON key set
(``docs/OtaManager.md`` §§5–6, 10):

```text
signPayload = "version|url|md5|size"
keys        = version, url, md5, size, signature
```

Any deviation makes the manifest unusable, so parsing here is deliberately
strict. The manifest is treated as untrusted input that must be verified against
the artifact and against the URL the backend composed — never as configuration.

Cryptographic verification is injected through :class:`ManifestVerifier`. The
production public key and a fixed conformance vector come from the firmware
repository, so verification is unavailable until that material exists; a release
whose manifest was never cryptographically verified must not be published.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Protocol

from app.domain.errors import DomainError
from app.domain.version import is_valid_version

MANIFEST_KEYS = frozenset({"version", "url", "md5", "size", "signature"})
MD5_PATTERN_LENGTH = 32

# The device parses the body into a 1024-byte Arduino JSON document.
MAX_MANIFEST_BYTES = 1024

# Parser bounds from docs/OtaManager.md §5.
MAX_URL_LENGTH = 96  # the url field must be *shorter* than 96 characters
MIN_ARTIFACT_SIZE = 1
MAX_ARTIFACT_SIZE = 4 * 1024 * 1024  # 4 MiB


@dataclass(frozen=True)
class Manifest:
    """A validated manifest; ``raw`` is the exact byte sequence stored and served."""

    version: str
    url: str
    md5: str
    size: int
    signature: str
    raw: bytes

    @property
    def signature_bytes(self) -> bytes:
        return base64.b64decode(self.signature)


class ManifestVerifier(Protocol):
    """Verifies the manifest signature with the firmware's production public key."""

    def verify(self, payload: bytes, signature: bytes) -> bool: ...


def signed_payload(version: str, url: str, md5: str, size: int) -> bytes:
    """The exact payload the device reconstructs; ``size`` is an unsigned decimal."""
    return f"{version}|{url}|{md5}|{size}".encode()


def serialize_manifest(version: str, url: str, md5: str, size: int, signature: str) -> bytes:
    """Compact serialization with the exact firmware key set and order."""
    return json.dumps(
        {
            "version": version,
            "url": url,
            "md5": md5,
            "size": size,
            "signature": signature,
        },
        separators=(",", ":"),
    ).encode()


def parse_manifest(raw: bytes) -> Manifest:
    """Strictly parse and structurally validate a manifest.

    Raises :class:`DomainError` with ``manifest_invalid``, ``missing_signature``,
    or ``invalid_signature``.
    """
    if not raw or len(raw) > MAX_MANIFEST_BYTES:
        raise DomainError("manifest_invalid")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise DomainError("manifest_invalid") from None
    if not isinstance(document, dict) or set(document) != MANIFEST_KEYS:
        raise DomainError("manifest_invalid")

    version = document["version"]
    if not isinstance(version, str) or not is_valid_version(version):
        raise DomainError("manifest_invalid")

    url = document["url"]
    if (
        not isinstance(url, str)
        or not url.startswith(("http://", "https://"))
        or len(url) >= MAX_URL_LENGTH
    ):
        raise DomainError("manifest_invalid")

    md5 = document["md5"]
    if not isinstance(md5, str) or not _is_hex(md5, MD5_PATTERN_LENGTH):
        raise DomainError("manifest_invalid")

    size = document["size"]
    # bool is an int subclass; the device reads a JSON number, never a boolean.
    if isinstance(size, bool) or not isinstance(size, int):
        raise DomainError("manifest_invalid")
    if not MIN_ARTIFACT_SIZE <= size <= MAX_ARTIFACT_SIZE:
        raise DomainError("manifest_invalid")

    signature = document["signature"]
    if not isinstance(signature, str) or not signature:
        raise DomainError("missing_signature")
    try:
        signature_bytes = base64.b64decode(signature, validate=True)
    except (binascii.Error, ValueError):
        raise DomainError("invalid_signature") from None
    if not is_der_ecdsa_signature(signature_bytes):
        raise DomainError("invalid_signature")

    return Manifest(
        version=version,
        url=url,
        md5=md5,
        size=size,
        signature=signature,
        raw=raw,
    )


def is_der_ecdsa_signature(signature: bytes) -> bool:
    """Structural screen for a DER-encoded ECDSA signature.

    The documented sample signature decodes to ``30 45 02 20 … 02 21 …``, i.e.
    DER (not raw ``r || s``, and not the JOSE concatenation). This check rejects
    garbage and the wrong encodings; cryptographic verification is the real gate
    and happens separately through :class:`ManifestVerifier`.
    """
    if not signature or signature[0] != 0x30:
        return False
    try:
        body_length, index = _read_der_length(signature, 1)
    except ValueError:
        return False
    if body_length != len(signature) - index:
        return False
    end = index + body_length
    for _ in range(2):  # r, then s
        if index >= end or signature[index] != 0x02:
            return False
        try:
            integer_length, index = _read_der_length(signature, index + 1)
        except ValueError:
            return False
        if integer_length < 1 or integer_length > 33 or index + integer_length > end:
            return False
        index += integer_length
    return index == end


def _read_der_length(data: bytes, index: int) -> tuple[int, int]:
    if index >= len(data):
        raise ValueError("truncated DER length")
    first = data[index]
    index += 1
    if first < 0x80:
        return first, index
    byte_count = first & 0x7F
    if byte_count == 0 or byte_count > 4 or index + byte_count > len(data):
        raise ValueError("invalid DER length")
    if data[index] == 0:  # non-minimal length encoding
        raise ValueError("non-minimal DER length")
    return int.from_bytes(data[index : index + byte_count], "big"), index + byte_count


def _is_hex(value: str, length: int) -> bool:
    return len(value) == length and all(character in "0123456789abcdef" for character in value)
