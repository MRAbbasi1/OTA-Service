from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol


class ObjectStream(Protocol):
    """A bounded-memory reader for one object-store object."""

    size: int

    def iter_chunks(self, chunk_size: int) -> Iterator[bytes]: ...

    def close(self) -> None: ...


class ObjectStorage(Protocol):
    """Narrow interface used by services instead of the MinIO SDK directly."""

    def is_healthy(self) -> bool: ...

    def ensure_bucket(self) -> None: ...

    def put_bytes(self, object_name: str, data: bytes, content_type: str) -> None: ...

    def get_bytes(self, object_name: str) -> bytes: ...

    def open_stream(self, object_name: str) -> ObjectStream: ...

    def remove(self, object_name: str) -> None: ...
