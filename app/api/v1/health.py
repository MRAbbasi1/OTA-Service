"""Health probes: two audiences, two surfaces.

* **Public** (``/health``, reachable through Nginx on each of the three
  hostnames): a minimal reachability signal. It returns exactly
  ``{"status":"ok"}`` or ``{"status":"unavailable"}``, names no dependency,
  carries no version, and reveals no internal host. Nginx marks the request
  with ``X-OTA-Role``, and the route checks only the dependencies that role
  needs: the manifest host needs the database, the delivery host needs object
  storage, and the admin host needs both.

  This is why losing MinIO does not make the manifest host report itself
  unhealthy: a load balancer should not remove an instance that can still do
  its job.

* **Internal** (``/health/live``, ``/health/ready``, ``/health/detail``,
  loopback only): the detailed view an operator or the deploy script needs. It
  names each dependency and its state, which is exactly what the public route
  must not expose. Nginx does not proxy these; reaching them requires being on
  the host or inside the Compose network.

The frontend SPA is deliberately not a dependency of this API. Nginx serves the
dashboard, not the API, so the API cannot truthfully report on it; the
``ota-health`` CLI checks the dashboard's own URL instead.

Probes are unauthenticated by design: a probe has no session, and requiring one
would mean an unhealthy instance cannot be detected. Neither surface ever
carries a credential, a hostname, or an error string.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from app.db.session import Database
from app.storage.base import ObjectStorage

router = APIRouter(tags=["health"])

_VALID_ROLES = frozenset({"api", "cdn", "ota"})
_MINIMAL_OK = b'{"status":"ok"}'
_MINIMAL_UNAVAILABLE = b'{"status":"unavailable"}'


def _state(request: Request) -> tuple[Database, ObjectStorage]:
    return request.app.state.database, request.app.state.storage


def _database_ok(database: Database) -> bool:
    try:
        return database.is_healthy()
    except Exception:  # noqa: BLE001 - a probe must never raise
        return False


def _storage_ok(storage: ObjectStorage) -> bool:
    try:
        return storage.is_healthy()
    except Exception:  # noqa: BLE001 - a probe must never raise
        return False


def _role(request: Request) -> str:
    """Which hostname the request came through.

    Set by the production Nginx on the proxied request, which overwrites the
    header unconditionally; a client cannot spoof it. A request with no role —
    an operator hitting the loopback port directly, or the container
    healthcheck — is treated as the strictest role, which checks everything.
    """
    requested = request.headers.get("X-OTA-Role", "").strip().lower()
    return requested if requested in _VALID_ROLES else "ota"


def _role_is_healthy(role: str, database: Database, storage: ObjectStorage) -> bool:
    if role == "api":
        return _database_ok(database)
    if role == "cdn":
        return _storage_ok(storage)
    return _database_ok(database) and _storage_ok(storage)


@router.get("/health", summary="Public reachability probe")
def health(request: Request) -> Response:
    """Minimal status. The caller learns nothing but whether to route here."""
    database, storage = _state(request)
    if _role_is_healthy(_role(request), database, storage):
        return Response(_MINIMAL_OK, media_type="application/json")
    return Response(
        _MINIMAL_UNAVAILABLE,
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        media_type="application/json",
    )


@router.get("/health/live", summary="Liveness probe (internal)")
def live() -> dict[str, str]:
    """The process is running and able to answer. No dependency is touched."""
    return {"status": "ok"}


@router.get("/health/ready", summary="Readiness probe (internal)")
def ready(request: Request, response: Response) -> dict[str, object]:
    """Whether this instance can serve a request that needs database and storage.

    Loopback-only: Nginx does not proxy this route, so the dependency names
    below are visible only to the operator, the deploy script, and the
    container healthcheck. The public ``/health`` route returns a minimal body
    on purpose.
    """
    database, storage = _state(request)
    dependencies = {
        "database": "ok" if _database_ok(database) else "unavailable",
        "object_storage": "ok" if _storage_ok(storage) else "unavailable",
    }
    healthy = all(value == "ok" for value in dependencies.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if healthy else "degraded", "dependencies": dependencies}


@router.get("/health/detail", summary="Detailed health view (internal)")
def detail(request: Request) -> dict[str, object]:
    """Everything an operator needs to diagnose an unhealthy instance.

    Loopback-only, and deliberately verbose: this is the answer to "the public
    probe says 503, why?". It never carries a credential or a signing key; it
    is reachable only from the host, which already holds the deployment's
    secrets.
    """
    database, storage = _state(request)
    settings = request.app.state.settings
    db_ok = _database_ok(database)
    storage_ok = _storage_ok(storage)
    return {
        "status": "ok" if db_ok and storage_ok else "degraded",
        "environment": settings.environment,
        "app_version": settings.app_version,
        "dependencies": {
            "database": "ok" if db_ok else "unavailable",
            "object_storage": "ok" if storage_ok else "unavailable",
        },
    }
