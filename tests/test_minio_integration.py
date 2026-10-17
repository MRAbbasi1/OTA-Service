from __future__ import annotations

import os

import pytest
from pydantic import SecretStr

from app.core.config import get_settings
from app.storage.minio import MinioObjectStorage

pytestmark = pytest.mark.integration


def _settings():
    if os.getenv("RUN_MINIO_INTEGRATION") != "1":
        pytest.skip("set RUN_MINIO_INTEGRATION=1 to run the MinIO integration tests")
    settings = get_settings()
    return settings.model_copy(
        update={
            "minio_endpoint": os.getenv("MINIO_ENDPOINT", settings.minio_endpoint),
            "minio_access_key": SecretStr(
                os.getenv("MINIO_ACCESS_KEY", settings.minio_access_key.get_secret_value())
            ),
            "minio_secret_key": SecretStr(
                os.getenv("MINIO_SECRET_KEY", settings.minio_secret_key.get_secret_value())
            ),
            "minio_bucket": "ota-phase6-test",
            "minio_secure": False,
        }
    )


def test_minio_round_trip_and_cleanup() -> None:
    storage = MinioObjectStorage(_settings())
    storage.ensure_bucket()
    object_name = "phase6/round-trip.bin"
    payload = b"phase-6-minio"

    try:
        storage.put_bytes(object_name, payload, "application/octet-stream")
        assert storage.is_healthy()
        assert storage.get_bytes(object_name) == payload
        stream = storage.open_stream(object_name)
        try:
            assert stream.size == len(payload)
            assert b"".join(stream.iter_chunks(4)) == payload
        finally:
            stream.close()
    finally:
        storage.remove(object_name)
