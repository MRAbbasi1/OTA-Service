"""One-shot write/read/delete verification for the configured object store."""

from __future__ import annotations

import hashlib
import sys
import uuid

from app.core.config import get_settings
from app.storage.base import ObjectStorage
from app.storage.minio import MinioObjectStorage

CANARY = b"ota-storage-round-trip-v1"


def check_storage(storage: ObjectStorage) -> None:
    object_name = f"_health/{uuid.uuid4()}.bin"
    written = False
    try:
        storage.ensure_bucket()
        storage.put_bytes(object_name, CANARY, "application/octet-stream")
        written = True
        stored = storage.get_bytes(object_name)
        if (
            len(stored) != len(CANARY)
            or hashlib.sha256(stored).digest() != hashlib.sha256(CANARY).digest()
        ):
            raise RuntimeError("object storage round-trip integrity check failed")
    finally:
        if written:
            storage.remove(object_name)


def main() -> int:
    try:
        check_storage(MinioObjectStorage(get_settings()))
    except Exception as exc:  # noqa: BLE001 - stable CLI failure boundary
        print(f"ota-storage-check: unavailable ({type(exc).__name__})", file=sys.stderr)
        return 1
    print("ota-storage-check: write/read/delete OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
