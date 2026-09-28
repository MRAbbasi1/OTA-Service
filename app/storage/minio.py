from __future__ import annotations

from collections.abc import Iterator
from io import BytesIO

from minio import Minio
from urllib3.response import BaseHTTPResponse

from app.core.config import Settings


class MinioObjectStream:
    """Owns and closes the HTTP response returned by MinIO."""

    def __init__(self, response: BaseHTTPResponse, size: int) -> None:
        self._response = response
        self.size = size

    def iter_chunks(self, chunk_size: int) -> Iterator[bytes]:
        yield from self._response.stream(amt=chunk_size, decode_content=False)

    def close(self) -> None:
        try:
            self._response.close()
        finally:
            self._response.release_conn()


class MinioObjectStorage:
    """MinIO adapter; business services must depend on `ObjectStorage`, not this SDK."""

    def __init__(self, settings: Settings) -> None:
        self.bucket = settings.minio_bucket
        self.region = settings.minio_region
        self.client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key.get_secret_value(),
            secret_key=settings.minio_secret_key.get_secret_value(),
            secure=settings.minio_secure,
        )

    def is_healthy(self) -> bool:
        try:
            self.client.bucket_exists(self.bucket)
        except Exception:
            return False
        return True

    def ensure_bucket(self) -> None:
        if not self.client.bucket_exists(self.bucket):
            self.client.make_bucket(self.bucket, location=self.region)

    def put_bytes(self, object_name: str, data: bytes, content_type: str) -> None:
        self.client.put_object(
            self.bucket,
            object_name,
            BytesIO(data),
            length=len(data),
            content_type=content_type,
        )

    def get_bytes(self, object_name: str) -> bytes:
        response = self.client.get_object(self.bucket, object_name)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def open_stream(self, object_name: str) -> MinioObjectStream:
        response = self.client.get_object(self.bucket, object_name)
        content_length = response.headers.get("Content-Length")
        if content_length is None:
            response.close()
            response.release_conn()
            raise OSError("MinIO object response is missing Content-Length")
        try:
            size = int(content_length)
        except ValueError as exc:
            response.close()
            response.release_conn()
            raise OSError("MinIO object response has an invalid Content-Length") from exc
        return MinioObjectStream(response, size)

    def remove(self, object_name: str) -> None:
        self.client.remove_object(self.bucket, object_name)
