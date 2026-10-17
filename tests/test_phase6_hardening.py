from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.v1.ota import _stream_chunks
from app.core.config import Settings
from app.db.models import FirmwareRelease
from app.services.firmware import ArtifactUpload
from app.services.ota import OtaResponse
from tests.conftest import FakeObjectStream
from tests.test_firmware_service import (
    BINARY,
    FILENAME,
    VERSION,
    make_device_type,
    make_service,
    manifest_payload,
)


class FailingStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.removed: list[str] = []

    def is_healthy(self) -> bool:
        return False

    def ensure_bucket(self) -> None:
        return None

    def put_bytes(self, object_name: str, data: bytes, content_type: str) -> None:
        self.objects[object_name] = data
        raise RuntimeError("storage write failed")

    def get_bytes(self, object_name: str) -> bytes:
        raise RuntimeError("storage read failed")

    def remove(self, object_name: str) -> None:
        self.removed.append(object_name)
        self.objects.pop(object_name, None)


def test_upload_failure_compensates_metadata_and_objects(
    committing_session: Session, truncate_tables: None
) -> None:
    storage = FailingStorage()
    device_type = make_device_type(committing_session)

    with pytest.raises(RuntimeError, match="storage write failed"):
        make_service(committing_session, storage).create_release(
            device_type_id=device_type.id,
            version=VERSION,
            artifact=ArtifactUpload(filename=FILENAME, data=BINARY),
            manifest_bytes=manifest_payload(),
        )

    assert committing_session.execute(select(FirmwareRelease)).scalars().all() == []
    assert storage.objects == {}
    assert storage.removed


def test_production_rejects_insecure_transport_and_defaults() -> None:
    values: dict[str, object] = {
        "environment": "production",
        "database_url": "postgresql+psycopg://ota:ota@localhost/ota",
        "minio_endpoint": "minio:9000",
        "minio_access_key": "access",
        "minio_secret_key": "secret",
        "ota_manifest_base_url": "http://api.ota-service.example",
        "ota_firmware_base_url": "https://cdn.ota-service.example",
        "admin_jwt_secret": "a" * 32,
        "minio_secure": True,
    }

    with pytest.raises(ValueError, match="OTA_MANIFEST_BASE_URL"):
        Settings(**values)

    values["ota_manifest_base_url"] = "https://api.ota-service.example"
    values["minio_secure"] = False
    with pytest.raises(ValueError, match="MINIO_SECURE"):
        Settings(**values)


def test_nginx_keeps_ota_responses_unredirected_and_non_chunked() -> None:
    config = Path("deploy/nginx/default.conf").read_text()

    assert "limit_req_status 429;" in config
    assert "location /api/v1/firmware/" in config
    assert "location /firmware/" in config
    assert "proxy_buffering off;" in config
    assert "proxy_pass http://api:8000;" in config
    assert "return 301" not in config
    assert "return 302" not in config


def test_production_host_nginx_is_additive_and_streams_firmware() -> None:
    config = Path("deploy/nginx/ota-service.conf.example").read_text()

    assert "server 127.0.0.1:18080;" in config
    assert "default_server" not in config
    servers = re.findall(r"(?ms)^server\s*\{(.*?)^\}", config)
    https_servers = [server for server in servers if "listen 443 ssl" in server]
    assert len(https_servers) == 3
    for server in https_servers:
        # The firmware requests HTTPS directly; its responses must not redirect.
        assert not re.search(r"\breturn\s+30[12378]\b", server)

    cdn = next(server for server in https_servers if "server_name <your-cdn-host>;" in server)
    assert "location /firmware/" in cdn
    assert "proxy_buffering off;" in cdn
    assert "proxy_request_buffering off;" in cdn
    assert "proxy_cache off;" in cdn
    assert "gzip off;" in cdn


def test_production_http_listener_only_redirects_to_https() -> None:
    config = Path("deploy/nginx/ota-service.conf.example").read_text()
    servers = re.findall(r"(?ms)^server\s*\{(.*?)^\}", config)
    http_servers = [server for server in servers if re.search(r"\blisten\s+80\s*;", server)]
    assert len(http_servers) == 1
    http_server = http_servers[0]
    assert "server_name <your-api-host> <your-cdn-host> <your-admin-host>;" in http_server
    assert "return 301 https://$host$request_uri;" in http_server
    assert "proxy_pass" not in http_server
    assert "root " not in http_server
    assert "alias " not in http_server


def test_production_dashboard_uses_a_prefix_and_rejects_root_paths() -> None:
    config = Path("deploy/nginx/ota-service.conf.example").read_text()
    servers = re.findall(r"(?ms)^server\s*\{(.*?)^\}", config)
    admin = next(server for server in servers if "server_name <your-admin-host>;" in server)
    assert "location ^~ /<your-dashboard-path>/assets/" in admin
    assert "location ^~ /<your-dashboard-path>/" in admin
    assert "try_files $uri =404;" in admin
    assert "try_files $uri /index.html =404;" in admin
    root = re.search(r"location\s+/\s*\{([^{}]*)\}", admin)
    assert root is not None
    assert root.group(1).strip() == "return 404;"


def test_production_compose_only_publishes_the_api_on_loopback() -> None:
    config = Path("compose.production.yaml").read_text()

    assert "127.0.0.1:${OTA_BIND_PORT:-18080}:8000" in config
    assert '"80:80"' not in config
    assert '"443:443"' not in config
    assert "\n  nginx:" not in config


def test_incomplete_firmware_stream_is_not_recorded_as_served() -> None:
    stream = FakeObjectStream(b"partial")
    completed: list[bool] = []
    response = OtaResponse(
        status_code=200,
        stream=stream,
        content_length=20,
        on_stream_complete=lambda: completed.append(True),
    )

    assert b"".join(_stream_chunks(response)) == b"partial"
    assert stream.closed is True
    assert completed == []
