# AGENTS.md — Developer & AI Agent Operating Manual

This rulebook is the authoritative guide governing every automated or human-assisted change to **OTA-Service**.
All agents and developers must consult this document before modifying code, tests, database migrations, configuration, deployment manifests, or documentation.

---

## 1. Reading Order & Hierarchy of Authority

When picking up any task, read resources in this strict order:
1. `CONTRIBUTING.md` — Workflow, branch conventions, and commit standards.
2. `docs/TRD.md` — Technical Requirements Document.
3. `docs/PRD.md` — Product Requirements Document.
4. `docs/IMPLEMENTATION-PLAN.md` — Milestone inventory and completion evidence.
5. `docs/OTA-CONTRACT.md` — **Authoritative normative wire contract** for device endpoints.
6. `docs/DEVICE-INTEGRATION.md` — Device integration architecture, retry/backoff, and wire protocol.
7. `docs/RESOURCE-RULES.md` — Resource ownership, immutability, and audit rules.
8. `docs/adr/` — Architecture Decision Records explaining foundational design choices.
9. Domain-specific documents in `docs/` (`00` through `18`).
10. For changes consumed by the web UI, consult `../OTA-Service Dashboard/docs/API-INTEGRATION.md`.

> [!IMPORTANT]
> **Precedence Rule:** If any code or informal comment contradicts `docs/OTA-CONTRACT.md` regarding the device wire protocol, `docs/OTA-CONTRACT.md` wins unconditionally.

---

## 2. Non-Negotiable Architectural Invariants

| Invariant | Rule | Rationale & Failure Mode Avoided |
| :--- | :--- | :--- |
| **Silicon Identity** | Authenticate solely using `raw_efuse_mac`. Never use Ethernet Network MAC for auth. | Network MAC is software-configurable and variable across modules. eFuse MAC is burned into silicon hardware; confusing them causes device impersonation or false auth failure. |
| **No Redirects (`3xx`)** | Manifest and firmware endpoints must return `200` directly or appropriate error. Never issue `301`, `302`, `307`, or `308`. | Embedded HTTP clients (e.g., ESP32-S3) do not follow redirects. A `3xx` consumes the 5-attempt retry budget and locks out the device. |
| **Exact Content-Length** | Binary firmware delivery must include an exact `Content-Length` header matching file size. | Embedded OTA stream writers allocate flash buffers based on `Content-Length`. Missing headers or chunked transfer encoding (`Transfer-Encoding: chunked`) abort the update. |
| **404 for Path Mismatch** | When requested `{device_type}` or `{platform}` does not match authenticated device, return `404` (not `403`). | Embedded devices treat `403` as an immediate security violation triggering a 24-hour lockout. A typo in device URL should return `404` so it can be fixed without penalizing the device. |
| **Release Signature Model** | The CI/CD release pipeline signs the binary. The platform stores and serves the signed manifest verbatim. | The platform does not hold the production private signing key. It never re-signs binaries. Verification against an optional public key is a deployment screen, not an authority. |
| **Immutable Releases** | Published firmware binaries, manifests, and versions are strictly immutable. | Modifying a published artifact compromises fleet integrity and checksum verifications. |
| **Immutable Migrations** | Never modify an existing Alembic revision in `alembic/versions/`. | Modifying past migrations creates divergent migration graphs across production and developer environments. Always generate a new migration. |
| **Memory-Only Secrets** | Passwords and tokens exist only in transient request memory. | Credentials must never be logged, written to disk, included in query parameters, or cached in storage. |
| **No Public Storage Access** | Never expose MinIO/S3 object storage URLs directly to devices or operators. | All artifact downloads pass through authenticated backend delivery endpoints. |

---

## 3. Step-by-Step Extension Recipes (Cookbook)

Follow these composable, repeatable patterns for common development tasks.

### Recipe 1: Adding a New Admin API Endpoint
1. **Define Pydantic Schema**:
   Add request and response models in `app/api/v1/schemas.py`. Use strict types, `Field(..., min_length=..., max_length=...)`, and forbid extra fields where appropriate.
2. **Implement Service Logic**:
   Add transactional domain methods in the corresponding module under `app/services/` (e.g., `devices.py`, `firmware.py`, `policies.py`). Enforce business rules here, not in the route handler.
3. **Record Administrative Audit**:
   If the operation mutates state, emit an audit event via the audit service:
   ```python
   self._audit.log(
       actor_id=admin.id,
       action="device_status_updated",
       resource_type="device",
       resource_id=device.id,
       payload={"old_status": old, "new_status": new},
   )
   ```
4. **Expose Route in `app/api/v1/`**:
   Add the endpoint with explicit `dependencies=[Security(require_capability("devices:write"))]`.
5. **Add Test Evidence**:
   In `tests/test_admin_api.py` (or feature-specific test):
   - Test successful path with 200/201.
   - Test capability rejection (403 forbidden).
   - Test invalid payloads (422 validation error).
   - Verify audit log insertion.

### Recipe 2: Adding or Modifying Database Models & Migrations
1. **Edit Model**:
   Modify or add the SQLAlchemy declarative class in `app/db/models.py`. Ensure table names and foreign keys follow snake_case convention and cascade rules.
2. **Generate Migration**:
   ```bash
   uv run alembic revision --autogenerate -m "add_device_enrollment_flag"
   ```
3. **Review Migration Script**:
   Inspect the newly created file in `alembic/versions/`. Ensure:
   - Enum creation/dropping is deterministic and safe for PostgreSQL.
   - Downward migration (`downgrade()`) cleanly reverses every change.
4. **Validate Schema**:
   Run migration upgrade, downgrade, and upgrade cycle:
   ```bash
   uv run alembic upgrade head
   uv run alembic downgrade -1
   uv run alembic upgrade head
   ```

### Recipe 3: Extending Update Policy Engine
1. **Pure Domain Rule**:
   Add matching logic inside `app/domain/policy.py`. This layer must remain pure: zero database imports, zero I/O.
2. **Service Integration**:
   Call the domain evaluation from `app/services/update_decision.py`.
3. **Unit Test Coverage**:
   Add deterministic unit test cases in `tests/test_update_decision.py` verifying boundary conditions (e.g., version ordering, priority resolution, fallback behaviors).

---

## 4. Top 10 Agent & Developer Pitfalls (Anti-Patterns)

1. **Broad Exception Suppression**:
   *Anti-pattern:* `except Exception: pass` or logging and returning None.
   *Correct:* Catch specific exceptions (`DomainError`, `NotFoundError`, `StorageUnavailableError`) and map them to standard HTTP status codes.
2. **Timezone Naivety**:
   *Anti-pattern:* `datetime.utcnow()` (deprecated in Python 3.12+ and timezone-naive).
   *Correct:* `datetime.now(timezone.utc)`.
3. **Inferring Product Policy from Implementation Gaps**:
   *Anti-pattern:* Assuming an endpoint should return 404 because a service layer wasn't written yet.
   *Correct:* Consult `docs/PRD.md` and `docs/RESOURCE-RULES.md`. If behavior is unspecified, escalate to the project maintainers.
4. **Treating Download as Installation**:
   *Anti-pattern:* Marking a device's firmware status as "Updated" when `/firmware/...` binary is downloaded.
   *Correct:* A download only proves `download_served`. The firmware is not updated until the device re-checks in and reports the new version in its manifest query.
5. **Hardcoding Brand Names, Hostnames, or Credentials**:
   *Anti-pattern:* Using proprietary brand identifiers or internal IPs (e.g., `198.51.100.x`, `*.internal-brand.net`).
   *Correct:* Use `example.com`, RFC 5737 documentation ranges (`192.0.2.0/24`), or generic identifiers (`ota-service`).
6. **Bypassing Capability Checks**:
   *Anti-pattern:* Checking `admin.role == AdminRole.SUPER_ADMIN` in route logic.
   *Correct:* Check capabilities (`require_capability(...)`) through RBAC dependencies.
7. **Modifying In-Flight Signatures**:
   *Anti-pattern:* Re-formatting or pretty-printing manifest JSON before serving it.
   *Correct:* Serve the exact byte stream validated during upload to avoid breaking ECDSA DER cryptographic signatures.
8. **Breaking the URL Budget**:
   *Anti-pattern:* Allowing long device type codes or platform names.
   *Correct:* `len(code) + len(platform) <= 29` chars to fit the device's 96-character URL buffer.
9. **Writing Project Code Outside Target Directories**:
   *Anti-pattern:* Writing scripts or artifacts to `/tmp` or root folders.
   *Correct:* Place scratch scripts in designated scratch folders and project code strictly in `app/`.
10. **Committing Broken Tests**:
    *Anti-pattern:* Marking a PR or task ready when tests fail or are skipped.
    *Correct:* All quality gates (`ruff check`, `mypy`, `pytest`) must pass cleanly.

---

## 5. Development & Quality Commands

| Command | Purpose |
| :--- | :--- |
| `uv sync --all-groups --frozen` | Install locked development dependencies |
| `uv run ruff check .` | Lint all Python files |
| `uv run ruff format --check .` | Check code formatting |
| `uv run mypy app` | Run strict static type checking |
| `uv run pytest` | Run entire test suite (requires local Postgres/MinIO stack) |
| `uv run pytest tests/test_security_core.py tests/test_manifest.py tests/test_update_path.py tests/test_rate_limit.py` | Run pure in-memory unit tests |
| `uv run alembic upgrade head` | Apply database migrations |
| `uv run uvicorn app.main:app --reload` | Start development API server |

---

## 6. Escalation Protocol

If an agent or developer discovers an ambiguity:
1. Identify the exact conflict (e.g., schema vs PRD vs route implementation).
2. Document the safety, security, and device-compatibility impact.
3. Stop execution and request clarification. Never guess security or wire-contract behavior.
