from __future__ import annotations

from collections.abc import Callable, Iterator
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.base import metadata
from app.db.models import AdminRole
from app.db.session import Database
from app.main import create_app
from app.services.admin_users import AdminUserService

_TABLES = (
    "admin_users",
    "update_policies",
    "update_attempts",
    "firmware_manifests",
    "firmware_artifacts",
    "firmware_releases",
    "audit_events",
    "device_tokens",
    "devices",
    "device_types",
)


class FakeObjectStorage:
    """In-memory `ObjectStorage` so the suite never needs a running MinIO."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.removed: list[str] = []
        self.opened_streams: list[FakeObjectStream] = []
        self.get_bytes_calls = 0

    def is_healthy(self) -> bool:
        return True

    def ensure_bucket(self) -> None:
        return None

    def put_bytes(self, object_name: str, data: bytes, content_type: str) -> None:
        self.objects[object_name] = data

    def get_bytes(self, object_name: str) -> bytes:
        self.get_bytes_calls += 1
        return self.objects[object_name]

    def open_stream(self, object_name: str) -> FakeObjectStream:
        stream = FakeObjectStream(self.objects[object_name])
        self.opened_streams.append(stream)
        return stream

    def remove(self, object_name: str) -> None:
        self.removed.append(object_name)
        self.objects.pop(object_name, None)


class FakeObjectStream:
    def __init__(self, data: bytes) -> None:
        self._source = BytesIO(data)
        self.size = len(data)
        self.closed = False

    def iter_chunks(self, chunk_size: int) -> Iterator[bytes]:
        while chunk := self._source.read(chunk_size):
            yield chunk

    def close(self) -> None:
        self.closed = True
        self._source.close()


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    database = Database(get_settings())
    metadata.create_all(database.engine)  # no-op when Alembic already applied
    yield database.engine
    database.dispose()


@pytest.fixture
def db_session(engine: Engine) -> Iterator[Session]:
    """Clean-tables unit-test session rolled back after each test (never commits)."""
    _truncate(engine)
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def truncate_tables(engine: Engine) -> Iterator[None]:
    """Real truncation before and after; use with the app client fixture."""
    _truncate(engine)
    yield
    _truncate(engine)


@pytest.fixture
def committing_session(engine: Engine, truncate_tables: None) -> Iterator[Session]:
    """Session that may commit: for services that own their own transaction."""
    session = Session(bind=engine, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def fake_storage() -> FakeObjectStorage:
    return FakeObjectStorage()


def _truncate(engine: Engine) -> None:
    with engine.begin() as connection:
        for table in _TABLES:
            connection.execute(text(f"TRUNCATE TABLE {table} RESTART IDENTITY CASCADE"))


#: The account the `client` fixture logs in as. Tests that need a different role
#: create one through the API, which is also the path an operator uses.
SUPER_ADMIN_EMAIL = "super.admin@ota-service.test"
SUPER_ADMIN_PASSWORD = "test-super-admin-password"


def provision_super_admin(app: object) -> None:
    """Create the bootstrap account directly, as the deployment command does."""
    factory = app.state.session_factory  # type: ignore[attr-defined]
    session = factory()
    try:
        AdminUserService(session).bootstrap_super_admin(SUPER_ADMIN_EMAIL, SUPER_ADMIN_PASSWORD)
        session.commit()
    finally:
        session.close()


def build_app(fake_storage: FakeObjectStorage) -> TestClient:
    """A started app whose storage is in-memory, so no MinIO is required."""
    app = create_app()
    test_client = TestClient(app)
    test_client.__enter__()  # runs the lifespan
    app.state.storage = fake_storage
    return test_client


@pytest.fixture
def anonymous_client(
    engine: Engine, truncate_tables: None, fake_storage: FakeObjectStorage
) -> Iterator[TestClient]:
    """A started app with the bootstrap account present and nobody logged in.

    Used by the authentication tests, which must observe what an unauthenticated
    or freshly authenticated caller actually sees.
    """
    test_client = build_app(fake_storage)
    try:
        provision_super_admin(test_client.app)
        yield test_client
    finally:
        test_client.__exit__(None, None, None)


@pytest.fixture
def client(
    engine: Engine, truncate_tables: None, fake_storage: FakeObjectStorage
) -> Iterator[TestClient]:
    """Depends on truncate_tables so cleanup runs AFTER the app pool is closed.

    Storage is swapped for the in-memory fake after startup, so firmware tests
    exercise the real routing and services without a MinIO dependency, and the
    client is logged in as a `SUPER_ADMIN` with the CSRF header set, because
    every administrative route requires both. Tests about authentication itself
    use `anonymous_client` instead.
    """
    test_client = build_app(fake_storage)
    try:
        provision_super_admin(test_client.app)
        login(test_client, SUPER_ADMIN_EMAIL, SUPER_ADMIN_PASSWORD)
        yield test_client
    finally:
        test_client.__exit__(None, None, None)


@pytest.fixture
def login_as(
    client: TestClient,
) -> Callable[[str, str], dict[str, object]]:
    """Log the shared client in as another account and return the session body."""

    def _login(email: str, password: str) -> dict[str, object]:
        return login(client, email, password)

    return _login


def login(client: TestClient, email: str, password: str) -> dict[str, object]:
    """Log in and make the client usable for unsafe methods.

    The CSRF token is echoed in a default header, which is exactly what the
    dashboard frontend does; without it every POST/PATCH/DELETE is a 403.
    """
    response = client.post("/api/v1/admin/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    client.headers["X-CSRF-Token"] = str(body["csrf_token"])
    return body


def create_admin_account(app: object, email: str, password: str, role: str) -> None:
    """Create an additional administrative account for role tests."""
    factory = app.state.session_factory  # type: ignore[attr-defined]
    session = factory()
    try:
        AdminUserService(session).create(email=email, password=password, role=AdminRole(role))
        session.commit()
    finally:
        session.close()
