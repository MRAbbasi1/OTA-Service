"""Liveness and readiness probe behaviour.

A probe that lies is worse than no probe: it keeps an instance in the load
balancer that cannot serve traffic, and it must stay reachable without a session.
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
