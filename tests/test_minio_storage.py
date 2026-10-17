from __future__ import annotations

from typing import Any

import pytest

from app.storage.base import StorageUnavailableError
from app.storage.minio import MinioObjectStorage


class FailingClient:
    def bucket_exists(self, bucket: str) -> bool:
        raise OSError("private endpoint and credentials")

    def put_object(self, *args: Any, **kwargs: Any) -> None:
        raise OSError("private endpoint and credentials")

    def get_object(self, *args: Any, **kwargs: Any) -> None:
        raise OSError("private endpoint and credentials")

    def remove_object(self, *args: Any, **kwargs: Any) -> None:
        raise OSError("private endpoint and credentials")


def failing_storage() -> MinioObjectStorage:
    storage = object.__new__(MinioObjectStorage)
    storage.bucket = "test-bucket"
    storage.region = "us-east-1"
    storage.client = FailingClient()  # type: ignore[assignment]
    return storage


def test_health_returns_false_without_leaking_the_sdk_error() -> None:
    assert failing_storage().is_healthy() is False


@pytest.mark.parametrize(
    "operation",
    [
        lambda storage: storage.ensure_bucket(),
        lambda storage: storage.put_bytes("release.bin", b"bytes", "application/octet-stream"),
        lambda storage: storage.get_bytes("release.bin"),
        lambda storage: storage.open_stream("release.bin"),
        lambda storage: storage.remove("release.bin"),
    ],
)
def test_sdk_failures_are_normalized(operation: Any) -> None:
    with pytest.raises(StorageUnavailableError) as excinfo:
        operation(failing_storage())

    assert "private endpoint" not in str(excinfo.value)
