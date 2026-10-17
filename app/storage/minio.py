from __future__ import annotations

import logging
from collections.abc import Iterator
from io import BytesIO

from minio import Minio
from urllib3 import PoolManager, Timeout
from urllib3.response import BaseHTTPResponse

from app.core.config import Settings
from app.storage.base import StorageUnavailableError

logger = logging.getLogger(__name__)


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
        http_client = None
        if settings.minio_secure and settings.minio_ca_cert:
            http_client = PoolManager(
                timeout=Timeout(connect=5.0, read=60.0),
                cert_reqs="CERT_REQUIRED",
                ca_certs=settings.minio_ca_cert,
            )
        self.client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key.get_secret_value(),
            secret_key=settings.minio_secret_key.get_secret_value(),
            secure=settings.minio_secure,
            http_client=http_client,
        )

    def is_healthy(self) -> bool:
        try:
            self.client.bucket_exists(self.bucket)
        except Exception:
            return False
        return True

    def ensure_bucket(self) -> None:
        try:
            if not self.client.bucket_exists(self.bucket):
                self.client.make_bucket(self.bucket, location=self.region)
        except Exception as exc:
            self._log_failure("ensure_bucket", exc)
            raise StorageUnavailableError("object storage bucket is unavailable") from exc

    def put_bytes(self, object_name: str, data: bytes, content_type: str) -> None:
        try:
            self.client.put_object(
                self.bucket,
                object_name,
                BytesIO(data),
                length=len(data),
                content_type=content_type,
            )
        except Exception as exc:
            self._log_failure("put_object", exc, object_name)
            raise StorageUnavailableError("object storage write failed") from exc

    def get_bytes(self, object_name: str) -> bytes:
        try:
            response = self.client.get_object(self.bucket, object_name)
        except Exception as exc:
            self._log_failure("get_object", exc, object_name)
            raise StorageUnavailableError("object storage read failed") from exc
        try:
            try:
                return response.read()
            except Exception as exc:
                self._log_failure("read_object", exc, object_name)
                raise StorageUnavailableError("object storage read failed") from exc
        finally:
            response.close()
            response.release_conn()

    def open_stream(self, object_name: str) -> MinioObjectStream:
        try:
            response = self.client.get_object(self.bucket, object_name)
        except Exception as exc:
            self._log_failure("open_stream", exc, object_name)
            raise StorageUnavailableError("object storage stream failed") from exc
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
        try:
            self.client.remove_object(self.bucket, object_name)
        except Exception as exc:
            self._log_failure("remove_object", exc, object_name)
            raise StorageUnavailableError("object storage cleanup failed") from exc

    def _log_failure(self, operation: str, exc: Exception, object_name: str | None = None) -> None:
        fields: dict[str, object] = {
            "operation": operation,
            "bucket": self.bucket,
            "exception_type": type(exc).__name__,
        }
        if object_name is not None:
            fields["object_name"] = object_name
        logger.exception("object_storage_operation_failed", extra={"extra_fields": fields})
