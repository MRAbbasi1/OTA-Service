from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import Response
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.api.v1.deps import get_session
from app.api.v1.security import enforce_login_rate_limit, set_session_cookies
from app.core.config import Settings
from app.main import create_app
from tests.conftest import FakeObjectStorage


def _production_settings() -> Settings:
    return Settings(
        environment="production",
        database_url="postgresql+psycopg://user:password@localhost/ota",
        minio_endpoint="minio:9000",
        minio_access_key="access",
        minio_secret_key="secret",
        ota_manifest_base_url="https://api.ota-service.example",
        ota_firmware_base_url="https://cdn.ota-service.example",
        admin_jwt_secret="a-random-production-session-secret-of-32-chars",
        minio_secure=True,
    )


def _dependencies(route: APIRoute) -> list[Any]:
    found: list[Any] = []

    def visit(dependant: Any) -> None:
        for child in dependant.dependencies:
            found.append(child.call)
            visit(child)

    visit(route.dependant)
    return found


def test_every_admin_route_has_an_authentication_or_capability_guard() -> None:
    admin_routes = [
        route
        for included_router in create_app().routes
        for route in getattr(getattr(included_router, "original_router", None), "routes", ())
        if isinstance(route, APIRoute) and route.path.startswith("/api/v1/admin/")
    ]
    intentionally_public = {
        ("/api/v1/admin/auth/login", "POST"),
        ("/api/v1/admin/auth/logout", "POST"),
    }

    assert admin_routes
    for route in admin_routes:
        if all((route.path, method) in intentionally_public for method in route.methods or ()):
            continue
        guards = [
            dependency
            for dependency in _dependencies(route)
            if getattr(dependency, "__module__", None) == "app.api.v1.security"
            and getattr(dependency, "__name__", "") in {"dependency", "authenticated_admin"}
        ]
        assert guards, f"{sorted(route.methods or [])} {route.path} has no admin guard"


def test_production_session_cookies_are_secure() -> None:
    settings = _production_settings()
    response = Response()

    set_session_cookies(
        response,
        settings=settings,
        token="session-secret",
        csrf_token="csrf-secret",
        max_age_seconds=1800,
    )

    headers = [value.decode() for key, value in response.raw_headers if key == b"set-cookie"]
    session_cookie = next(value for value in headers if value.startswith("ota_admin_session="))
    csrf_cookie = next(value for value in headers if value.startswith("ota_admin_csrf="))
    assert "Secure" in session_cookie
    assert "HttpOnly" in session_cookie
    assert "SameSite=lax" in session_cookie
    assert "Secure" in csrf_cookie
    assert "HttpOnly" not in csrf_cookie
    assert "SameSite=lax" in csrf_cookie


def test_login_rate_limit_uses_proxy_address_not_forwarded_chain() -> None:
    class RecordingLimiter:
        key: str | None = None

        def allow(self, key: str, limit: object) -> bool:
            self.key = key
            return True

    from starlette.requests import Request

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/admin/auth/login",
            "headers": [
                (b"x-real-ip", b"203.0.113.8"),
                (b"x-forwarded-for", b"198.51.100.9, 192.0.2.17"),
            ],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
            "scheme": "http",
            "query_string": b"",
        }
    )
    limiter = RecordingLimiter()

    enforce_login_rate_limit(request, "admin@example.test", limiter, _production_settings())

    assert limiter.key is not None
    assert "203.0.113.8" in limiter.key
    assert "198.51.100.9" not in limiter.key
    assert "192.0.2.17" not in limiter.key


def test_production_proxy_overwrites_forwarded_identity_headers() -> None:
    config = Path("deploy/nginx/ota-service.conf.example").read_text()
    compose = Path("compose.production.yaml").read_text()

    assert "proxy_set_header X-Real-IP $remote_addr;" in config
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in config
    assert "$proxy_add_x_forwarded_for" not in config
    assert "--forwarded-allow-ips=*" not in compose
    assert "--proxy-headers" not in compose


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/admin/auth/me",
        "/api/v1/firmware/bcs-controller-v1/esp32-s3/manifest.json",
    ],
)
def test_untrusted_origin_never_receives_cors_permission(
    path: str,
) -> None:
    response = TestClient(create_app()).options(
        path,
        headers={
            "Origin": "https://attacker.invalid",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert response.status_code == 405
    assert "access-control-allow-origin" not in {key.lower() for key in response.headers}
    assert "access-control-allow-credentials" not in {key.lower() for key in response.headers}


def test_production_nginx_configures_security_headers_on_error_responses() -> None:
    config = Path("deploy/nginx/ota-service.conf.example").read_text()

    for header in (
        'add_header Strict-Transport-Security "max-age=31536000" always;',
        "add_header X-Content-Type-Options nosniff always;",
        "add_header X-Frame-Options DENY always;",
        "add_header Referrer-Policy no-referrer always;",
    ):
        assert header in config


def test_sql_looking_search_value_cannot_expand_device_results(
    client: TestClient,
) -> None:
    device_type = client.post(
        "/api/v1/admin/device-types",
        json={"code": "bcs-controller", "name": "Smart Controller", "platform": "esp32-s3"},
    )
    assert device_type.status_code == 201
    device = client.post(
        "/api/v1/admin/devices",
        json={
            "device_type_id": device_type.json()["id"],
            "serial_number": 10432,
            "raw_efuse_mac": "7C:9E:BD:12:34:56",
        },
    )
    assert device.status_code == 201

    response = client.get(
        "/api/v1/admin/devices",
        params={"search": "10432' OR 1=1 --"},
    )

    assert response.status_code == 200
    assert response.json()["total"] == 0
    assert response.json()["items"] == []


def test_minio_failure_response_does_not_leak_internal_details(
    client: TestClient,
    fake_storage: FakeObjectStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device_type = client.post(
        "/api/v1/admin/device-types",
        json={"code": "bcs-controller", "name": "Smart Controller", "platform": "esp32-s3"},
    )
    assert device_type.status_code == 201
    internal_secret = "minio.internal:9000 PRIVATE_ACCESS_KEY"

    def fail_write(object_name: str, data: bytes, content_type: str) -> None:
        raise RuntimeError(internal_secret)

    monkeypatch.setattr(fake_storage, "put_bytes", fail_write)
    client.raise_server_exceptions = False
    response = client.post(
        "/api/v1/admin/firmware/releases",
        data={"device_type_id": str(device_type.json()["id"]), "version": "2.6.1"},
        files={"artifact": ("firmware.bin", b"firmware", "application/octet-stream")},
    )

    assert response.status_code == 500
    assert internal_secret not in response.text
    assert "Traceback" not in response.text


def test_database_failure_response_does_not_leak_internal_details(
    client: TestClient,
) -> None:
    internal_secret = "postgres.internal:5432 user=ota password=private"

    def fail_session() -> None:
        raise RuntimeError(internal_secret)

    client.app.dependency_overrides[get_session] = fail_session
    client.raise_server_exceptions = False
    try:
        response = client.get("/api/v1/admin/devices")
    finally:
        client.app.dependency_overrides.pop(get_session, None)

    assert response.status_code == 500
    assert internal_secret not in response.text
    assert "Traceback" not in response.text
