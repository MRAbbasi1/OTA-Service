from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import sessionmaker
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.api.v1.admin_auth import admins_router, auth_router
from app.api.v1.communications import router as communications_router
from app.api.v1.dashboard import audit_router, dashboard_router
from app.api.v1.devices import router as devices_router
from app.api.v1.firmware import router as firmware_router
from app.api.v1.health import router as health_router
from app.api.v1.monitoring import router as monitoring_router
from app.api.v1.ota import router as ota_router
from app.api.v1.policies import router as policies_router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.core.monitoring import MonitoringBuffer
from app.core.request_context import request_id_context
from app.db.session import Database
from app.services.communications import CommunicationRecorder
from app.services.rate_limit import InMemoryRateLimiter
from app.storage.minio import MinioObjectStorage

logger = logging.getLogger(__name__)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        supplied_request_id = request.headers.get("X-Request-ID")
        try:
            request_id = (
                str(uuid.UUID(supplied_request_id)) if supplied_request_id else str(uuid.uuid4())
            )
        except (ValueError, AttributeError):
            request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        context_token = request_id_context.set(request_id)
        started_at = time.perf_counter()
        try:
            observer = None
            if request.url.path.startswith(("/api/v1/firmware/", "/firmware/")):
                manifest = request.url.path.startswith("/api/v1/firmware/")
                parts = request.url.path.split("/")[4 if manifest else 2 :]
                matched = (
                    re.fullmatch(
                        r"/api/v1/firmware/[^/]+/[^/]+/manifest\.json"
                        if manifest
                        else r"/firmware/[^/]+/[^/]+/v[^/]+/[^/]+",
                        request.url.path,
                    )
                    is not None
                )
                parameters = dict(
                    zip(
                        ("device_type", "platform")
                        if manifest
                        else ("device_type", "platform", "version", "filename"),
                        parts,
                        strict=False,
                    )
                )
                if not matched:
                    parameters = {}
                observer = await run_in_threadpool(
                    CommunicationRecorder,
                    request.app.state.session_factory,
                    request_id=request_id,
                    kind="manifest" if manifest else "firmware",
                    serial=request.headers.get("X-Device-Serial"),
                    mac=request.headers.get("X-Device-Mac"),
                    token_present=bool(request.headers.get("X-Device-Token")),
                    parameters=parameters,
                    method=request.method,
                    observe=request.app.state.monitoring.record_capture,
                )
                request.state.communication_observer = observer
            try:
                response = await call_next(request)
            except Exception as exc:
                logger.exception(
                    "unhandled_exception",
                    extra={
                        "request_id": request_id,
                        "extra_fields": {
                            "path": getattr(request.scope.get("route"), "path", "unmatched"),
                            "method": request.method,
                            "exception_type": type(exc).__name__,
                        },
                    },
                )
                response = JSONResponse(status_code=500, content={"code": "internal_error"})
            if observer is not None and not observer.responded:
                if response.status_code >= 500:
                    await run_in_threadpool(observer.failed)
                else:
                    await run_in_threadpool(
                        observer.rejected,
                        response.status_code,
                        "route_not_found"
                        if response.status_code == 404
                        else "request_not_accepted",
                    )
            response.headers["X-Request-ID"] = request_id
            if request.url.path.startswith("/api/v1/admin/"):
                response.headers["Cache-Control"] = "no-store"
            if not request.url.path.startswith(("/health", "/api/v1/admin/monitoring")):
                request.app.state.monitoring.record_request(
                    response.status_code,
                    round((time.perf_counter() - started_at) * 1000, 2),
                    request_id,
                    normal_no_offer=response.status_code == 404
                    and getattr(request.scope.get("route"), "name", None)
                    in {"get_manifest", "get_firmware"},
                )
            logger.info(
                "request_completed",
                extra={
                    "request_id": request_id,
                    "extra_fields": {
                        "method": request.method,
                        "path": getattr(request.scope.get("route"), "path", "unmatched"),
                        "status_code": response.status_code,
                        "duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
                    },
                },
            )
            return response
        finally:
            request_id_context.reset(context_token)


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
        # inheriting an implicit dependency (docs/15-implementation-decisions.md §7).
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
        logging.getLogger().removeHandler(app.state.monitoring)
        database.dispose()
        logger.info("application_stopped")

    app = FastAPI(
        title=runtime_settings.app_name,
        version=runtime_settings.app_version,
        lifespan=lifespan,
    )
    app.state.monitoring = MonitoringBuffer()
    logging.getLogger().addHandler(app.state.monitoring)

    app.add_middleware(RequestLoggingMiddleware)
    # Probes first: unauthenticated by design (app/api/v1/health.py).
    app.include_router(health_router)
    # Administrative surfaces: every route below carries an authentication,
    # capability, CSRF, and rate-limit dependency (app/api/v1/security.py).
    app.include_router(auth_router)
    app.include_router(admins_router)
    app.include_router(dashboard_router)
    app.include_router(audit_router)
    app.include_router(monitoring_router)
    app.include_router(communications_router)
    app.include_router(devices_router)
    app.include_router(firmware_router)
    app.include_router(policies_router)
    # Device-facing OTA routes authenticate the device, not an administrator.
    app.include_router(ota_router)
    return app


app = create_app()
