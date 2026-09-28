from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from sqlalchemy.orm import sessionmaker
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.api.v1.admin_auth import admins_router, auth_router
from app.api.v1.dashboard import audit_router, dashboard_router
from app.api.v1.devices import router as devices_router
from app.api.v1.firmware import router as firmware_router
from app.api.v1.health import router as health_router
from app.api.v1.ota import router as ota_router
from app.api.v1.policies import router as policies_router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import Database
from app.services.rate_limit import InMemoryRateLimiter
from app.storage.minio import MinioObjectStorage

logger = logging.getLogger(__name__)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        started_at = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request_completed",
            extra={
                "request_id": request_id,
                "extra_fields": {
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
                },
            },
        )
        return response


def create_app(settings: Settings | None = None) -> FastAPI:
    runtime_settings = settings or get_settings()
    configure_logging(runtime_settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = Database(runtime_settings)
        storage = MinioObjectStorage(runtime_settings)
        factory = sessionmaker(bind=database.engine, autoflush=False, expire_on_commit=False)
        app.state.database = database
        app.state.storage = storage
        app.state.session_factory = factory
        app.state.settings = runtime_settings
        # Per-process rate-limit state, shared by the device-facing endpoints.
        # An injectable object rather than a module-level singleton, so a
        # multi-replica deployment can replace it with a shared store instead of
        # inheriting an implicit dependency (docs/15 §7).
        app.state.rate_limiter = InMemoryRateLimiter()
        # Optional deployment guard. The device is the only verifier the OTA
        # contract requires (it holds the embedded public key) and the release
        # pipeline produces the signature, so `None` is the normal setting: the
        # platform stores and serves the signed manifest verbatim. A deployment
        # that wants the extra check wires a verifier here, and then verification
        # becomes mandatory for uploads.
        app.state.manifest_verifier = None
        logger.info(
            "application_started",
            extra={"extra_fields": {"environment": runtime_settings.environment}},
        )
        yield
        database.dispose()
        logger.info("application_stopped")

    app = FastAPI(
        title=runtime_settings.app_name,
        version=runtime_settings.app_version,
        lifespan=lifespan,
    )
    app.add_middleware(RequestLoggingMiddleware)
    # Probes first: unauthenticated by design (app/api/v1/health.py).
    app.include_router(health_router)
    # Administrative surfaces: every route below carries an authentication,
    # capability, CSRF, and rate-limit dependency (app/api/v1/security.py).
    app.include_router(auth_router)
    app.include_router(admins_router)
    app.include_router(dashboard_router)
    app.include_router(audit_router)
    app.include_router(devices_router)
    app.include_router(firmware_router)
    app.include_router(policies_router)
    # Device-facing OTA routes authenticate the device, not an administrator.
    app.include_router(ota_router)
    return app


app = create_app()
