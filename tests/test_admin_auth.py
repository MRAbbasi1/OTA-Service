"""Administrative authentication, session, and authorization tests.

These tests are the security contract for the admin surface: an unauthenticated
caller gets nothing, a cookie-only request cannot change state, a weaker role
cannot perform a stronger role's action, and every credential failure ends live
sessions immediately.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.engine import Engine

from app.cli import bootstrap_admin
from app.db.models import AdminRole, AuditEvent
from app.services.rate_limit import ADMIN_LOGIN_RATE_LIMIT
from tests.conftest import (
    SUPER_ADMIN_EMAIL,
    SUPER_ADMIN_PASSWORD,
    create_admin_account,
    login,
)

pytestmark = pytest.mark.usefixtures("truncate_tables")

NEW_PASSWORD = "a-much-longer-replacement-password"
WEAK_PASSWORD = "short"

VIEWER = ("viewer@ota-service.test", "viewer-password-123")
FLEET = ("fleet@ota-service.test", "fleet-password-1234")
FIRMWARE = ("firmware@ota-service.test", "firmware-password-123")


def audit_actions(engine: Engine, action: str) -> list[str]:
    with engine.connect() as connection:
        rows = connection.execute(
            select(AuditEvent.action).where(AuditEvent.action == action)
        ).all()
    return [row[0] for row in rows]


# The administrative routes a caller must not reach without a session; one
# representative route per capability is enough, and the capability tests below
# cover the rest.
PROTECTED_ROUTES = [
    ("GET", "/api/v1/admin/devices", "devices:read"),
    ("POST", "/api/v1/admin/devices", "devices:write"),
    ("GET", "/api/v1/admin/device-types", "devices:read"),
    ("GET", "/api/v1/admin/firmware/releases", "firmware:read"),
    ("POST", "/api/v1/admin/firmware/releases/1/publish", "firmware:write"),
    ("GET", "/api/v1/admin/policies", "policies:read"),
    ("POST", "/api/v1/admin/policies", "policies:write"),
    ("GET", "/api/v1/admin/dashboard/summary", "dashboard:read"),
    ("GET", "/api/v1/admin/audit-events", "audit:read"),
    ("GET", "/api/v1/admin/admins", "admins:manage"),
    ("GET", "/api/v1/admin/auth/me", "authenticated"),
]


class TestUnauthenticatedAccess:
    @pytest.mark.parametrize(("method", "path", "capability"), PROTECTED_ROUTES)
    def test_every_admin_route_requires_a_session(
        self, anonymous_client: TestClient, method: str, path: str, capability: str
    ) -> None:
        response = anonymous_client.request(method, path, json={})

        assert response.status_code == 401, (path, capability)
        assert response.json()["detail"]["code"] == "unauthenticated"
        assert response.headers["www-authenticate"] == "Cookie"

    def test_the_device_ota_routes_still_authenticate_the_device_only(
        self, anonymous_client: TestClient
    ) -> None:
        """Device authentication is a separate mechanism and must stay separate."""
        response = anonymous_client.get("/api/v1/firmware/bcs-controller-v1/esp32-s3/manifest.json")

        assert response.status_code == 401
        assert response.json() == {"code": "unauthorized"}


class TestLogin:
    def test_login_sets_an_httponly_session_cookie_and_a_readable_csrf_cookie(
        self, anonymous_client: TestClient
    ) -> None:
        response = anonymous_client.post(
            "/api/v1/admin/auth/login",
            json={"email": SUPER_ADMIN_EMAIL, "password": SUPER_ADMIN_PASSWORD},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["admin"]["email"] == SUPER_ADMIN_EMAIL
        assert body["admin"]["role"] == AdminRole.SUPER_ADMIN.value
        assert body["csrf_header"] == "X-CSRF-Token"
        assert body["csrf_token"]

        cookies = response.headers.get_list("set-cookie")
        session_cookie = next(c for c in cookies if c.startswith("ota_admin_session="))
        csrf_cookie = next(c for c in cookies if c.startswith("ota_admin_csrf="))
        assert "HttpOnly" in session_cookie
        assert "HttpOnly" not in csrf_cookie  # the client must be able to read it
        assert "SameSite=lax" in session_cookie
        # Development is served over http, so Secure would make the cookie unusable.
        assert "Secure" not in session_cookie
        # The token never appears in the body, only in the HttpOnly cookie.
        assert body["admin"].keys().isdisjoint({"password", "token", "access_token"})

    def test_an_email_is_case_insensitive(self, anonymous_client: TestClient) -> None:
        response = anonymous_client.post(
            "/api/v1/admin/auth/login",
            json={"email": SUPER_ADMIN_EMAIL.upper(), "password": SUPER_ADMIN_PASSWORD},
        )

        assert response.status_code == 200
        assert response.json()["admin"]["email"] == SUPER_ADMIN_EMAIL

    @pytest.mark.parametrize(
        ("email", "password"),
        [
            (SUPER_ADMIN_EMAIL, "wrong-password-entirely"),
            ("nobody@ota-service.test", SUPER_ADMIN_PASSWORD),
        ],
    )
    def test_every_credential_failure_is_the_same_generic_401(
        self, anonymous_client: TestClient, email: str, password: str
    ) -> None:
        response = anonymous_client.post(
            "/api/v1/admin/auth/login", json={"email": email, "password": password}
        )

        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "invalid_credentials"
        assert "set-cookie" not in {key.lower() for key in response.headers}

    def test_an_inactive_account_cannot_log_in(self, anonymous_client: TestClient) -> None:
        create_admin_account(anonymous_client.app, *VIEWER, role="viewer")
        login(anonymous_client, SUPER_ADMIN_EMAIL, SUPER_ADMIN_PASSWORD)
        viewer_id = anonymous_client.get("/api/v1/admin/admins").json()["items"][1]["id"]
        anonymous_client.post(f"/api/v1/admin/admins/{viewer_id}/active", json={"is_active": False})

        response = anonymous_client.post(
            "/api/v1/admin/auth/login", json={"email": VIEWER[0], "password": VIEWER[1]}
        )

        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "invalid_credentials"

    def test_failures_then_successes_are_both_audited(
        self, anonymous_client: TestClient, engine: Engine
    ) -> None:
        anonymous_client.post(
            "/api/v1/admin/auth/login",
            json={"email": SUPER_ADMIN_EMAIL, "password": "wrong"},
        )
        login(anonymous_client, SUPER_ADMIN_EMAIL, SUPER_ADMIN_PASSWORD)

        assert audit_actions(engine, "admin_login_failed") == ["admin_login_failed"]
        assert audit_actions(engine, "admin_login") == ["admin_login"]

    def test_the_attempted_email_is_audited_but_never_the_password(
        self, anonymous_client: TestClient, engine: Engine
    ) -> None:
        anonymous_client.post(
            "/api/v1/admin/auth/login",
            json={"email": "probe@ota-service.test", "password": "guess-me-not-logged"},
        )

        with engine.connect() as connection:
            rows = connection.execute(
                select(AuditEvent.actor, AuditEvent.detail).where(
                    AuditEvent.action == "admin_login_failed"
                )
            ).all()

        assert rows[0][0] == "probe@ota-service.test"
        assert "guess-me-not-logged" not in str(rows[0][1])

    def test_repeated_failures_hit_the_documented_limit(self, anonymous_client: TestClient) -> None:
        payload = {"email": SUPER_ADMIN_EMAIL, "password": "wrong"}
        for _ in range(ADMIN_LOGIN_RATE_LIMIT.limit):
            assert (
                anonymous_client.post("/api/v1/admin/auth/login", json=payload).status_code == 401
            )

        response = anonymous_client.post("/api/v1/admin/auth/login", json=payload)

        assert response.status_code == 429
        assert response.json()["detail"]["code"] == "rate_limited"

    def test_the_limit_does_not_lock_out_a_different_account(
        self, anonymous_client: TestClient
    ) -> None:
        """Keyed by address *and* email, so one attack cannot silence everyone."""
        create_admin_account(anonymous_client.app, *VIEWER, role="viewer")
        for _ in range(ADMIN_LOGIN_RATE_LIMIT.limit):
            anonymous_client.post(
                "/api/v1/admin/auth/login",
                json={"email": SUPER_ADMIN_EMAIL, "password": "wrong"},
            )

        response = anonymous_client.post(
            "/api/v1/admin/auth/login", json={"email": VIEWER[0], "password": VIEWER[1]}
        )

        assert response.status_code == 200


class TestSession:
    def test_me_reports_the_role_and_its_capabilities(self, client: TestClient) -> None:
        response = client.get("/api/v1/admin/auth/me")

        assert response.status_code == 200
        body = response.json()
        assert body["email"] == SUPER_ADMIN_EMAIL
        assert "admins:manage" in body["capabilities"]
        assert "firmware:write" in body["capabilities"]

    def test_logout_clears_the_cookies_and_ends_the_session(
        self, anonymous_client: TestClient
    ) -> None:
        login(anonymous_client, SUPER_ADMIN_EMAIL, SUPER_ADMIN_PASSWORD)

        response = anonymous_client.post("/api/v1/admin/auth/logout")

        assert response.status_code == 200
        assert response.json() == {"status": "logged_out"}
        assert "ota_admin_session=" in response.headers["set-cookie"]
        assert anonymous_client.get("/api/v1/admin/auth/me").status_code == 401

    def test_logout_without_a_session_is_not_an_error(self, anonymous_client: TestClient) -> None:
        assert anonymous_client.post("/api/v1/admin/auth/logout").status_code == 200

    def test_a_tampered_cookie_is_rejected(self, anonymous_client: TestClient) -> None:
        login(anonymous_client, SUPER_ADMIN_EMAIL, SUPER_ADMIN_PASSWORD)
        token = anonymous_client.cookies["ota_admin_session"]

        anonymous_client.cookies.set("ota_admin_session", token[:-3] + "aaa")

        assert anonymous_client.get("/api/v1/admin/auth/me").status_code == 401

    def test_a_changed_password_ends_every_other_session(
        self, anonymous_client: TestClient
    ) -> None:
        """No waiting for the token to expire: the session version moves."""
        login(anonymous_client, SUPER_ADMIN_EMAIL, SUPER_ADMIN_PASSWORD)
        stolen_cookie = anonymous_client.cookies["ota_admin_session"]
        csrf = anonymous_client.headers["X-CSRF-Token"]

        changed = anonymous_client.post(
            "/api/v1/admin/auth/password",
            json={"current_password": SUPER_ADMIN_PASSWORD, "new_password": NEW_PASSWORD},
        )
        assert changed.status_code == 200, changed.text

        # The old token is now worthless, even though it has not expired.
        anonymous_client.cookies.set("ota_admin_session", stolen_cookie)
        anonymous_client.headers["X-CSRF-Token"] = csrf
        assert anonymous_client.get("/api/v1/admin/auth/me").status_code == 401

        fresh = anonymous_client.post(
            "/api/v1/admin/auth/login",
            json={"email": SUPER_ADMIN_EMAIL, "password": NEW_PASSWORD},
        )
        assert fresh.status_code == 200

    def test_a_password_change_requires_the_current_password(
        self, anonymous_client: TestClient
    ) -> None:
        login(anonymous_client, SUPER_ADMIN_EMAIL, SUPER_ADMIN_PASSWORD)

        response = anonymous_client.post(
            "/api/v1/admin/auth/password",
            json={"current_password": "not-my-password", "new_password": NEW_PASSWORD},
        )

        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "invalid_password"

    def test_a_weak_password_is_rejected_before_anything_is_hashed(
        self, anonymous_client: TestClient
    ) -> None:
        """The request schema enforces the length floor; the domain re-checks it."""
        login(anonymous_client, SUPER_ADMIN_EMAIL, SUPER_ADMIN_PASSWORD)

        response = anonymous_client.post(
            "/api/v1/admin/auth/password",
            json={"current_password": SUPER_ADMIN_PASSWORD, "new_password": WEAK_PASSWORD},
        )

        assert response.status_code == 422
        assert "new_password" in response.text
        # The old password still works, so nothing was replaced.
        anonymous_client.cookies.clear()
        assert (
            anonymous_client.post(
                "/api/v1/admin/auth/login",
                json={"email": SUPER_ADMIN_EMAIL, "password": SUPER_ADMIN_PASSWORD},
            ).status_code
            == 200
        )

    def test_a_deactivated_account_loses_access_immediately(
        self, anonymous_client: TestClient
    ) -> None:
        create_admin_account(anonymous_client.app, *FLEET, role="fleet_manager")
        fleet_session = login(anonymous_client, *FLEET)
        fleet_cookie = anonymous_client.cookies["ota_admin_session"]
        assert fleet_session["admin"]["email"] == FLEET[0]

        login(anonymous_client, SUPER_ADMIN_EMAIL, SUPER_ADMIN_PASSWORD)
        admins = anonymous_client.get("/api/v1/admin/admins").json()["items"]
        fleet_id = next(row["id"] for row in admins if row["email"] == FLEET[0])
        deactivated = anonymous_client.post(
            f"/api/v1/admin/admins/{fleet_id}/active", json={"is_active": False}
        )
        assert deactivated.status_code == 200, deactivated.text

        # The fleet manager's still-unexpired token no longer resolves to an account.
        anonymous_client.cookies.set("ota_admin_session", fleet_cookie)
        anonymous_client.headers["X-CSRF-Token"] = str(fleet_session["csrf_token"])
        assert anonymous_client.get("/api/v1/admin/devices").status_code == 401


class TestCsrf:
    def test_an_unsafe_request_without_the_header_is_rejected(
        self, anonymous_client: TestClient
    ) -> None:
        login(anonymous_client, SUPER_ADMIN_EMAIL, SUPER_ADMIN_PASSWORD)
        del anonymous_client.headers["X-CSRF-Token"]

        response = anonymous_client.post(
            "/api/v1/admin/device-types",
            json={"code": "bcs-controller", "name": "Controller", "platform": "esp32-s3"},
        )

        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "csrf_failed"

    def test_a_mismatched_header_is_rejected(self, anonymous_client: TestClient) -> None:
        login(anonymous_client, SUPER_ADMIN_EMAIL, SUPER_ADMIN_PASSWORD)
        anonymous_client.headers["X-CSRF-Token"] = "not-the-cookie-value"

        response = anonymous_client.post(
            "/api/v1/admin/devices/1/status", json={"status": "active"}
        )

        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "csrf_failed"

    def test_safe_methods_need_no_header(self, anonymous_client: TestClient) -> None:
        login(anonymous_client, SUPER_ADMIN_EMAIL, SUPER_ADMIN_PASSWORD)
        del anonymous_client.headers["X-CSRF-Token"]

        assert anonymous_client.get("/api/v1/admin/devices").status_code == 200

    def test_the_csrf_cookie_alone_is_not_enough(self, anonymous_client: TestClient) -> None:
        """A cross-site attacker can send cookies but cannot read them."""
        login(anonymous_client, SUPER_ADMIN_EMAIL, SUPER_ADMIN_PASSWORD)
        assert anonymous_client.cookies.get("ota_admin_csrf")
        del anonymous_client.headers["X-CSRF-Token"]

        response = anonymous_client.post("/api/v1/admin/policies", json={"name": "x"})

        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "csrf_failed"


class TestAuthorization:
    @pytest.mark.parametrize(
        ("role", "allowed", "denied"),
        [
            ("viewer", "GET /api/v1/admin/devices", "POST /api/v1/admin/devices"),
            ("viewer", "GET /api/v1/admin/dashboard/summary", "GET /api/v1/admin/audit-events"),
            (
                "fleet_manager",
                "POST /api/v1/admin/devices",
                "POST /api/v1/admin/firmware/releases/1/publish",
            ),
            ("fleet_manager", "POST /api/v1/admin/policies", "GET /api/v1/admin/admins"),
            (
                "firmware_manager",
                "GET /api/v1/admin/firmware/releases",
                "POST /api/v1/admin/devices",
            ),
            (
                "firmware_manager",
                "POST /api/v1/admin/firmware/releases/1/publish",
                "POST /api/v1/admin/policies",
            ),
            ("super_admin", "GET /api/v1/admin/admins", None),
        ],
    )
    def test_a_role_can_do_exactly_what_its_capabilities_say(
        self,
        anonymous_client: TestClient,
        role: str,
        allowed: str,
        denied: str | None,
    ) -> None:
        email = f"{role}@ota-service.test"
        password = f"{role}-password-1234"
        create_admin_account(anonymous_client.app, email, password, role=role)
        login(anonymous_client, email, password)

        method, path = allowed.split(" ", 1)
        assert anonymous_client.request(method, path, json={}).status_code not in {401, 403}

        if denied is not None:
            method, path = denied.split(" ", 1)
            response = anonymous_client.request(method, path, json={})
            assert response.status_code == 403, (role, denied)
            assert response.json()["detail"]["code"] == "insufficient_capability"

    def test_only_a_super_admin_can_create_an_administrator(
        self, anonymous_client: TestClient
    ) -> None:
        create_admin_account(anonymous_client.app, *FLEET, role="fleet_manager")
        login(anonymous_client, *FLEET)

        response = anonymous_client.post(
            "/api/v1/admin/admins",
            json={
                "email": "new@ota-service.test",
                "password": "new-admin-password",
                "role": "viewer",
            },
        )

        assert response.status_code == 403
        assert response.json()["detail"]["required"] == "admins:manage"

    def test_a_duplicate_email_is_a_conflict(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/admin/admins",
            json={
                "email": SUPER_ADMIN_EMAIL,
                "password": "another-long-password",
                "role": "viewer",
            },
        )

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "admin_email_exists"

    def test_the_last_super_admin_cannot_be_deactivated_or_demoted(
        self, client: TestClient
    ) -> None:
        me = client.get("/api/v1/admin/auth/me").json()

        deactivate = client.post(
            f"/api/v1/admin/admins/{me['id']}/active", json={"is_active": False}
        )
        demote = client.post(f"/api/v1/admin/admins/{me['id']}/role", json={"role": "viewer"})

        assert deactivate.status_code == 409
        assert deactivate.json()["detail"]["code"] == "last_super_admin"
        assert demote.status_code == 409
        assert demote.json()["detail"]["code"] == "last_super_admin"

    def test_a_second_super_admin_can_be_created_and_a_role_change_is_audited(
        self, client: TestClient, engine: Engine
    ) -> None:
        created = client.post(
            "/api/v1/admin/admins",
            json={
                "email": "second@ota-service.test",
                "password": "second-admin-pass",
                "role": "viewer",
            },
        )
        assert created.status_code == 201, created.text

        changed = client.post(
            f"/api/v1/admin/admins/{created.json()['id']}/role", json={"role": "super_admin"}
        )

        assert changed.status_code == 200
        assert changed.json()["role"] == "super_admin"
        assert audit_actions(engine, "admin_created") == ["admin_created"]
        assert audit_actions(engine, "admin_role_changed") == ["admin_role_changed"]

    def test_an_admin_creation_never_echoes_the_password(self, client: TestClient) -> None:
        secret = "never-echo-this-password"
        response = client.post(
            "/api/v1/admin/admins",
            json={"email": "quiet@ota-service.test", "password": secret, "role": "viewer"},
        )

        assert response.status_code == 201
        assert secret not in response.text

    def test_a_created_admin_cannot_be_created_twice_with_different_casing(
        self, client: TestClient
    ) -> None:
        client.post(
            "/api/v1/admin/admins",
            json={
                "email": "Mixed.Case@ota-service.test",
                "password": "mixed-case-password",
                "role": "viewer",
            },
        )

        response = client.post(
            "/api/v1/admin/admins",
            json={
                "email": "mixed.case@ota-service.test",
                "password": "mixed-case-password",
                "role": "viewer",
            },
        )

        assert response.status_code == 409

    def test_an_invalid_email_is_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/admin/admins",
            json={"email": "not-an-email", "password": "long-enough-password", "role": "viewer"},
        )

        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "invalid_email"


class TestBootstrapCommand:
    def test_it_creates_the_first_administrator_with_an_audit_record(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(bootstrap_admin.EMAIL_ENV, "bootstrap@ota-service.test")
        monkeypatch.setenv(bootstrap_admin.PASSWORD_ENV, "bootstrap-password-1")

        assert bootstrap_admin.main() == bootstrap_admin.EXIT_OK

        with engine.connect() as connection:
            assert connection.execute(
                select(AuditEvent.action).where(AuditEvent.action == "admin_bootstrapped")
            ).all()

    def test_it_is_idempotent_and_does_not_reset_an_existing_password(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv(bootstrap_admin.EMAIL_ENV, SUPER_ADMIN_EMAIL)
        monkeypatch.setenv(bootstrap_admin.PASSWORD_ENV, SUPER_ADMIN_PASSWORD)

        assert bootstrap_admin.main() == bootstrap_admin.EXIT_OK
        assert bootstrap_admin.main() == bootstrap_admin.EXIT_OK

        assert "nothing was changed" in capsys.readouterr().out
        with engine.connect() as connection:
            rows = connection.execute(
                select(AuditEvent.action).where(AuditEvent.action == "admin_bootstrapped")
            ).all()
        assert len(rows) == 1  # the second run records nothing

    def test_it_refuses_to_run_without_credentials(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv(bootstrap_admin.EMAIL_ENV, raising=False)
        monkeypatch.delenv(bootstrap_admin.PASSWORD_ENV, raising=False)

        assert bootstrap_admin.main() == bootstrap_admin.EXIT_INVALID
        assert bootstrap_admin.PASSWORD_ENV in capsys.readouterr().err

    def test_it_refuses_a_weak_password_without_printing_it(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv(bootstrap_admin.EMAIL_ENV, "bootstrap@ota-service.test")
        monkeypatch.setenv(bootstrap_admin.PASSWORD_ENV, WEAK_PASSWORD)

        assert bootstrap_admin.main() == bootstrap_admin.EXIT_INVALID
        captured = capsys.readouterr()
        assert "weak_password" in captured.err
        assert WEAK_PASSWORD not in captured.err + captured.out
