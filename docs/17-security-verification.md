# Security Verification

This document records what automated tests prove about the current FastAPI
backend, where their evidence comes from, and what still needs a deployed
environment or a frontend. It is a test-coverage record, not a claim that
application security can be proven by a single test suite.

`PASS` identifies an automated regression assertion for the stated backend
behavior; it does not replace a passing CI run or the deployment checks listed
below. The controls themselves are defined in `docs/08-security.md`; the
production configuration they depend on is specified in
`docs/13-deployment.md`.

## Verification Matrix

| Area                            | Implementation evidence                                                                       | Regression evidence                                                                                                                                                                                     | Result / boundary                                                                                                                                                                                                            |
| ------------------------------- | --------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Authentication                  | Admin session dependencies and device-specific OTA authentication                             | `tests/test_admin_auth.py`, `tests/test_security_core.py`, `tests/test_ota_api.py`                                                                                                                      | **PASS** — protected admin routes reject missing/invalid sessions; OTA routes retain separate device authentication.                                                                                                         |
| Session validity and revocation | JWT expiry/claims plus database-backed session version                                        | `tests/test_security_core.py`, `tests/test_admin_auth.py`                                                                                                                                               | **PASS** — expired, tampered, changed-password, changed-role, and deactivated-account sessions are rejected.                                                                                                                 |
| Cookie attributes               | `set_session_cookies` derives `Secure` from production settings                               | `tests/test_admin_auth.py`, `tests/test_security_verification.py::test_production_session_cookies_are_secure`                                                                                           | **PASS** — session cookie is `HttpOnly`, both cookies are `SameSite=Lax`, and both are `Secure` in production.                                                                                                               |
| Authorization                   | Capability dependencies on administrative endpoints                                           | `tests/test_admin_auth.py`, `tests/test_security_verification.py::test_every_admin_route_has_an_authentication_or_capability_guard`                                                                     | **PASS** — route inventory checks every registered admin endpoint other than the deliberately public login and CSRF-checked logout endpoints. Capability denials are covered separately.                                     |
| CSRF                            | Double-submit CSRF token on unsafe cookie-authenticated requests                              | `tests/test_admin_auth.py`                                                                                                                                                                              | **PASS** — missing/mismatched tokens are rejected and valid tokens are accepted.                                                                                                                                             |
| Login rate limiting             | Per-address-and-email application limit keyed from Nginx-overwritten source identity          | `tests/test_admin_auth.py`, `tests/test_security_verification.py::test_login_rate_limit_uses_proxy_address_not_forwarded_chain`                                                                         | **PASS, with deployment boundary** — application ignores `X-Forwarded-For`; production Nginx overwrites identity headers, and Uvicorn trusts no arbitrary forwarded headers. The API port must remain loopback-only.         |
| XSS boundary                    | API returns JSON, not rendered HTML                                                           | `tests/test_admin_api.py::TestDeviceTypes::test_html_looking_description_remains_json_data`                                                                                                             | **BACKEND PASS / FRONTEND REQUIRED** — this proves the API treats the text as JSON data, not that a future UI safely inserts it into the DOM. Frontend escaping and DOM-sink tests require the separate frontend repository. |
| CORS                            | No permissive CORS middleware or origin allow-list is installed                               | `tests/test_security_verification.py::test_untrusted_origin_never_receives_cors_permission`                                                                                                             | **PASS** — untrusted origins receive no `Access-Control-Allow-Origin` or credentials grant. CORS is browser-enforced response sharing, not server-side request authentication.                                               |
| SQL/ORM injection               | SQLAlchemy expressions and bound values are used for user-controlled search                   | `tests/test_security_verification.py::test_sql_looking_search_value_cannot_expand_device_results`                                                                                                       | **PASS** — SQL-looking input remains a search value and does not expand the result set.                                                                                                                                      |
| Storage boundary                | MinIO is private and device download requests authenticate and authorize before object access | `tests/test_ota_api.py`, `tests/test_firmware_api.py`, `tests/test_minio_integration.py`                                                                                                                | **PASS** — API responses do not hand devices object URLs or redirects; unauthorized delivery is covered.                                                                                                                     |
| Error leakage                   | Unexpected storage and database failures are handled by the generic HTTP 500 response         | `tests/test_security_verification.py::test_minio_failure_response_does_not_leak_internal_details`, `tests/test_security_verification.py::test_database_failure_response_does_not_leak_internal_details` | **PASS** — the response omits injected endpoint, credential, and traceback markers. Operational logs remain subject to the separate secret-redaction requirements.                                                           |
| Security headers                | Production host Nginx adds HSTS, `nosniff`, frame denial, and referrer policy with `always`   | `tests/test_security_verification.py::test_production_nginx_configures_security_headers_on_error_responses`, `tests/test_phase6_hardening.py`                                                           | **PASS, verified in production** — the configuration is pinned by the regression test, and the headers were confirmed on success **and** error responses through the public listener of a production deployment.             |

## Interpretation

The current backend checks are sufficient to claim regression coverage for its
authentication, sessions, RBAC, CSRF, rate limiting, CORS response policy,
bound-query behavior, storage boundary, and generic error response. They do not
prove that a frontend is safe from XSS. Runtime behavior through the production
proxy — security headers on success and error responses, reachability, and
firewall integration — was verified separately in a production deployment.

The FastAPI API is not a rendered HTML application. Returning a string such as
`<script>...</script>` inside `application/json` proves only that this backend
does not execute or render it. Any consuming browser UI must use safe text
rendering (for example, framework interpolation) and must not pass untrusted
values to HTML injection sinks. That frontend verification belongs in the
frontend repository.

The login rate-limit identity depends on the documented network boundary:
production Compose publishes the API only on `127.0.0.1`; the host Nginx proxy
overwrites `X-Real-IP` and `X-Forwarded-For`; Uvicorn does not process arbitrary
proxy headers. Changing any of these three conditions requires updating this
contract and re-verifying it before deployment.

## Verified in Production

All items below were verified in a production deployment of this stack.

| Item                                            | Result                                                                                                                                                                               |
| ----------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Security headers on success and error responses | **Verified** — HSTS, `X-Content-Type-Options`, `X-Frame-Options`, and `Referrer-Policy` are present through the public listener on both OTA hostnames, including on error responses. |
| API reachability                                | **Verified** — the API answers only through the public proxy; the container publish stays on loopback.                                                                               |
| Storage reachability                            | **Verified** — MinIO is not publicly reachable.                                                                                                                                      |
| Host firewall integration                       | **Verified** — UFW is active and `ufw-docker` is installed, so container publishes cannot bypass UFW.                                                                                |
| Ban handling on the host                        | **Verified** — fail2ban runs, and CI runner bans are cleared with `fail2ban-client unban --all`.                                                                                     |
| Health endpoints from the Internet              | **Verified** — the role-aware `/health` route answers `200` on all three hostnames; `/health/live`, `/health/ready`, and `/health/detail` answer `404` from the public listener and remain reachable only on loopback. |
| Health body carries no internal detail          | **Verified** — the public `/health` body is exactly `{"status":"ok"}` or `{"status":"unavailable"}`; the string `database`, `minio`, `object_storage`, or `dependencies` never appears.                                |
| Device routes are absent from the admin hostname | **Verified** — `/api/v1/firmware/*` and `/firmware/*` answer `404` on `ota.*`.                                                                                                        |
| Administrative API is reachable only on the administrative hostname | **Verified** — `/api/v1/admin/*` returns `404` on `api.*` and `cdn.*`, and `401` (without session) on `ota.*`. |

The host-side configuration these items depend on is specified in "Firewall" in
`docs/13-deployment.md`, and the commands used to re-check them are in "Security
Hygiene" in `docs/18-operations.md`.

## Production Acceptance — Remaining Work

1. Send a request with forged `X-Real-IP` and `X-Forwarded-For` values through
   the public listener and confirm login throttling still keys on the socket peer
   address observed by the proxy.
2. Test the consuming frontend separately for safe rendering and DOM XSS sinks.
3. Confirm the hostname routing matrix against the deployed proxy:
   `/api/v1/admin/*` answers `404` on `api.*` and `cdn.*`, and the frontend SPA
   on `ota.*` reaches `/api/v1/admin/*` on its own origin.

These are deployment and frontend acceptance checks, not claims made by the
FastAPI TestClient suite; the automated suite cannot substitute for them.
