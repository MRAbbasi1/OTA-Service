"""Liveness and readiness probes.

These are consumed by Nginx's upstream check, by Compose's `healthcheck`, and by
whatever runs the container in production, so they must be:

* **unauthenticated** — a probe has no session, and requiring one would mean an
  unhealthy deployment cannot be detected;
* **silent about internals** — the body names a dependency and whether it
  answered, and never a hostname, credential, or error string, because this route
  is reachable from anywhere that can reach the API;
* **separated** — liveness answers "is the process up" without touching a
  dependency, so a slow database cannot cause a restart loop; readiness answers
  "can this instance serve traffic" and does check them.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from app.db.session import Database
from app.storage.base import ObjectStorage

router = APIRouter(tags=["health"])


def _report(request: Request) -> tuple[Database, ObjectStorage]:
    return request.app.state.database, request.app.state.storage


@router.get("/health/live", summary="Liveness probe")
def live() -> dict[str, str]:
    """The process is running and able to answer. No dependency is touched."""
    return {"status": "ok"}


@router.get("/health/ready", summary="Readiness probe")
def ready(request: Request, response: Response) -> dict[str, object]:
    """Whether this instance can serve a request that needs the database and storage."""
    database, storage = _report(request)
    dependencies = {
        "database": "ok" if database.is_healthy() else "unavailable",
        "object_storage": "ok" if _storage_is_healthy(storage) else "unavailable",
    }
    healthy = all(value == "ok" for value in dependencies.values())
    if not healthy:
        # 503 so a load balancer removes this instance instead of routing to it.
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if healthy else "degraded", "dependencies": dependencies}


def _storage_is_healthy(storage: ObjectStorage) -> bool:
    try:
        return storage.is_healthy()
    except Exception:  # noqa: BLE001 - a readiness probe must never raise
        return False
