"""Liveness and readiness probe behaviour.

A probe that lies is worse than no probe: it keeps an instance in the load
balancer that cannot serve traffic, and it must stay reachable without a session.

The public surface is also tested here: it is reachable from the Internet, so
its body must reveal nothing but reachability, and each hostname role must
report on exactly the dependencies it needs.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeObjectStorage

pytestmark = pytest.mark.usefixtures("truncate_tables")


class BrokenStorage(FakeObjectStorage):
    def is_healthy(self) -> bool:
        raise RuntimeError("object storage is unreachable")


def test_liveness_needs_no_session_and_touches_no_dependency(
    anonymous_client: TestClient,
) -> None:
    response = anonymous_client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_reports_both_dependencies(anonymous_client: TestClient) -> None:
    response = anonymous_client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "dependencies": {"database": "ok", "object_storage": "ok"},
    }


def test_readiness_fails_when_storage_is_down(anonymous_client: TestClient) -> None:
    anonymous_client.app.state.storage = BrokenStorage()

    response = anonymous_client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["dependencies"] == {"database": "ok", "object_storage": "unavailable"}


def test_readiness_never_leaks_an_internal_error(anonymous_client: TestClient) -> None:
    anonymous_client.app.state.storage = BrokenStorage()

    body = anonymous_client.get("/health/ready").text

    assert "RuntimeError" not in body and "unreachable" not in body
    assert "localhost" not in body and "minio" not in body.lower()


def test_readiness_fails_when_the_database_is_down(
    anonymous_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        anonymous_client.app.state.database, "is_healthy", lambda: False, raising=False
    )

    response = anonymous_client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["dependencies"]["database"] == "unavailable"


def _break_database(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client.app.state.database, "is_healthy", lambda: False, raising=False)


class TestPublicHealthSurface:
    """The single Internet-reachable health route: minimal and role-aware."""

    @pytest.mark.parametrize("role", ["api", "cdn", "ota"])
    def test_every_healthy_role_returns_exactly_the_minimal_body(
        self, anonymous_client: TestClient, role: str
    ) -> None:
        response = anonymous_client.get("/health", headers={"X-OTA-Role": role})

        assert response.status_code == 200
        assert response.content == b'{"status":"ok"}'

    @pytest.mark.parametrize("role", ["api", "cdn", "ota"])
    def test_the_body_never_names_a_dependency(
        self, anonymous_client: TestClient, role: str
    ) -> None:
        body = anonymous_client.get("/health", headers={"X-OTA-Role": role}).text.lower()

        for leak in ("database", "minio", "object_storage", "dependencies", "postgres"):
            assert leak not in body

    def test_api_role_does_not_check_object_storage(self, anonymous_client: TestClient) -> None:
        """Losing MinIO must not make the manifest host report itself unhealthy."""
        anonymous_client.app.state.storage = BrokenStorage()

        response = anonymous_client.get("/health", headers={"X-OTA-Role": "api"})

        assert response.status_code == 200
        assert response.content == b'{"status":"ok"}'

    def test_cdn_role_does_not_check_the_database(
        self, anonymous_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _break_database(anonymous_client, monkeypatch)

        response = anonymous_client.get("/health", headers={"X-OTA-Role": "cdn"})

        assert response.status_code == 200
        assert response.content == b'{"status":"ok"}'

    def test_api_role_fails_when_the_database_is_down(
        self, anonymous_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _break_database(anonymous_client, monkeypatch)

        response = anonymous_client.get("/health", headers={"X-OTA-Role": "api"})

        assert response.status_code == 503
        assert response.content == b'{"status":"unavailable"}'

    def test_cdn_role_fails_when_storage_is_down(self, anonymous_client: TestClient) -> None:
        anonymous_client.app.state.storage = BrokenStorage()

        response = anonymous_client.get("/health", headers={"X-OTA-Role": "cdn"})

        assert response.status_code == 503
        assert response.content == b'{"status":"unavailable"}'

    def test_ota_role_fails_when_either_dependency_is_down(
        self, anonymous_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        anonymous_client.app.state.storage = BrokenStorage()
        assert anonymous_client.get("/health", headers={"X-OTA-Role": "ota"}).status_code == 503

        anonymous_client.app.state.storage = FakeObjectStorage()
        _break_database(anonymous_client, monkeypatch)
        assert anonymous_client.get("/health", headers={"X-OTA-Role": "ota"}).status_code == 503

    def test_a_missing_role_header_defaults_to_the_strictest(
        self, anonymous_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert anonymous_client.get("/health").status_code == 200

        anonymous_client.app.state.storage = BrokenStorage()
        assert anonymous_client.get("/health").status_code == 503

        anonymous_client.app.state.storage = FakeObjectStorage()
        _break_database(anonymous_client, monkeypatch)
        assert anonymous_client.get("/health").status_code == 503

    def test_an_unknown_role_defaults_to_the_strictest(self, anonymous_client: TestClient) -> None:
        anonymous_client.app.state.storage = BrokenStorage()

        response = anonymous_client.get("/health", headers={"X-OTA-Role": "other"})

        assert response.status_code == 503

    def test_the_public_route_is_unauthenticated(self, anonymous_client: TestClient) -> None:
        assert anonymous_client.get("/health").status_code in {200, 503}


class TestInternalDetail:
    """The internal endpoint may name its dependencies; it is loopback only."""

    def test_detail_names_exactly_the_two_dependencies(self, anonymous_client: TestClient) -> None:
        response = anonymous_client.get("/health/detail")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["environment"] == anonymous_client.app.state.settings.environment
        assert body["app_version"]
        assert set(body["dependencies"]) == {"database", "object_storage"}
        assert body["dependencies"] == {"database": "ok", "object_storage": "ok"}

    def test_detail_reports_degraded_dependencies(self, anonymous_client: TestClient) -> None:
        anonymous_client.app.state.storage = BrokenStorage()

        body = anonymous_client.get("/health/detail").json()

        assert body["status"] == "degraded"
        assert body["dependencies"]["object_storage"] == "unavailable"
