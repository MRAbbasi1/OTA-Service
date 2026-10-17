# Device communication evidence

## Operational follow-up — 2026-10-16

Receipt capture now precedes route/dependency resolution for OTA prefixes, including rejected methods/subpaths. Persistent summary, linked-resource filters, server-time periods, recording-health diagnostics, atomic audit and operational triage are specified in [OPERATIONAL-EVIDENCE.md](OPERATIONAL-EVIDENCE.md). This follow-up requires existing migration 0009 but adds no migration; it does not backfill missing history.

Recording diagnostics now runs only through explicit GET /communications/diagnostics, independently of normal history and dependency-status reads. It is a read-only snapshot with no polling or synthetic write. Browser reads require a mounted visible/online page; hidden/offline transitions cancel unused requests and related evidence outside the viewport pauses. The shell public probe uses 30-second polling rather than three seconds. See the shared specification for lifecycle, scope and backend-first rollout.

This is the implementation and acceptance contract for the 2026-10-16 observability redesign. The operator needs to answer which device called, what it requested, what the server returned, why, and whether a firmware response finished. Device communications, administrative audit, and process health are separate models and workspaces.

## Scope and boundaries

- `device_communications` is PostgreSQL-backed request/response history for the two existing device OTA routes. Receipt, prepared response, and transfer result share one exchange ID and request UUID.
- `audit_events` remains the independent administrative change trail. Its records are not device telemetry. Before/after evidence and resource/request correlation remain available.
- `/monitoring/status` remains current dependency health and bounded process metrics. `/monitoring/events?source=system` selects reviewed service faults, excluding routine device and administrator activity.
- No device headers, manifest bytes, decision rules, response statuses, signatures, content lengths, redirects, firmware bytes, or installation-reporting semantics change.
- No collector, queue, additional runtime dependency, or frontend policy engine is introduced.

## Operator scenarios

| Scenario | Evidence and interaction |
| --- | --- |
| Was a device heard from? | Search its serial prefix and a time range. New exchanges appear automatically. An empty result states only that no request was recorded, not that the device is offline. |
| Why was no update offered? | Inspect the recorded reason and decision snapshot from the original request, not a decision recalculated using today's policy. HTTP 404 no-offer is a neutral domain result. |
| Why was access rejected? | Show the safe operator-only rejection reason; claimed serial/MAC remain unverified unless authentication succeeded. Credentials never appear. |
| Did a download finish? | Show prepared/unconfirmed, full server emission, or incomplete transfer with expected/emitted byte counts. None is installation or end-device receipt confirmation. |
| Did an administrator change something? | Follow the authenticated device to its own admin history; inspect actor/time/before/after independently. Request UUID can connect a specific admin action to service faults. |
| Share evidence with support | Export matching exchanges as CSV/JSON or copy/download an individual exchange. Export administrative evidence independently. |
| Restart during a request | The stored receipt or prepared response remains unconfirmed. No synthetic completion or historical backfill is invented. |

## Record lifecycle

`received` is committed before calling the OTA service, using a short independent transaction. After the service resolves the request it transitions to `offered`, `no_offer`, `auth_rejected`, `rate_limited`, `server_error`, or `streaming` (response prepared). Firmware iteration subsequently transitions to `download_served` only after the expected byte count is yielded and the existing generator completes; closure, exception, or size disagreement records `transfer_incomplete`.

`duration_ms` measures server work up to the latest recorded observation, including transfer iteration for a terminal firmware result. It is not network RTT or proof of flash/boot. `bytes_sent` measures bytes yielded by the application, not acknowledged client receipt. A stopped process cannot finish its own receipt; the UI labels unfinished states unconfirmed instead of leaving an authoritative “currently downloading” assertion.

Writes use the existing database pool with a separate session so rollback of a domain transaction cannot erase a recorded receipt. Recording failures emit `communication_record_failed` without exception prose or credentials and do not replace the device's response. If the database cannot record a request, history has a gap; the process fault view/private logs are the evidence. This is best-effort operational evidence, not a lossless journal across database failure.

Each request adds a receipt insert and response update; a streamed response adds a final transfer update. These synchronous short transactions add database work and can wait on the existing pool. Functional tests are not a fleet-scale throughput benchmark: staging must check request latency, pool capacity, and database growth under the expected polling rate before production rollout.

An authenticated `device_id` reference is stored only after all device authentication checks pass. Unauthenticated attempts retain a bounded numeric claimed serial and syntactically valid claimed MAC without attributing them to an enrolled device. Device identity edits and exceptional purge are blocked by authenticated communication history, both in eligibility responses and mutation checks.

## Data minimization

Capture is explicit, not a generic body/header interceptor:

- request: GET, route template, reviewed bounded path parameters, claimed numeric serial/MAC, and token-present boolean;
- response: HTTP status, reviewed JSON fields (`version`, `url`, `md5`, `size`, `signature`, `code`, `reason`), selected version and reason;
- manifest decision: reason, policy ID/scope, release ID and known-version provenance;
- timeline: static state names and server UTC timestamps;
- binary: expected/emitted counts only.

Passwords, device tokens, session/CSRF cookies, arbitrary headers, query strings, exception text, stack traces, storage keys, and binary payloads are not copied. Invalid or oversized identity/path values become null. Manifest response detail is a reviewed-field representation, not a claim of byte-identical capture; the existing firmware manifest endpoint remains authoritative for exact signed bytes. JSON is rendered as inert text. CSV values are protected against spreadsheet formula execution.

## Read and export API

`GET /api/v1/admin/communications` requires existing `logs:read` (currently Super Admin). Filters: numeric serial prefix, manifest/firmware kind, explicit outcome, timezone-aware inclusive since/until, request UUID, snapshot ceiling, and newest/oldest/slowest order. Pagination defaults to 50, maximum 100. Stable ordering includes the exchange ID. Responses include the applied since value and maximum visible exchange ID. Older pages pin both the ID ceiling and time-window start; this prevents ordinary new arrivals shifting pages. This is an insertion ceiling, not a database transaction snapshot of subsequent transfer-state changes.

`GET /communications/{id}` returns the authorized stored exchange. `GET /communications/export?format=csv|json` exports the applied filters and ID ceiling. Export is bounded to 1,000 rows; excess returns `422 export_limit_exceeded` with no silently truncated file. The frontend asks for a narrower range/device. `GET /audit-events/export` provides the independent admin export with `audit:read`, identical sanitization and export limit. Every evidence read/export is `Cache-Control: no-store`.

## Dashboard behavior

- `/communications`: one filter toolbar, one table, one bounded details dialog and CSV/JSON actions. Defaults to the last 24 hours/newest first. Outcome meaning is conveyed with text and semantic color.
- Newest communications poll every five seconds in the foreground, with a ten-second request deadline, and refetch on restored focus/connectivity. Older pages and alternative sorting preserve browsing position. No enable-live action is required.
- Inspection holds table rows while the list continues fetching; new arrivals are indicated without replacing the selected row. In-progress detail refetches separately. Closing returns focus to the originating row and reveals current results.
- `/audit`: admin-only filters/table/detail/export. First page refreshes every ten seconds; inspection/older pages hold evidence. No health cards, device stream, histogram, or loose shortcut strip.
- `/monitoring`: dependency state every 30 seconds and a five-second foreground refresh of service faults. Sample metrics are secondary diagnostics. Failure shows Unknown/stale; previous successful data is never asserted fresh.
- Polling is bounded and foreground-only, not a push/subsecond channel. Retry controls are recovery paths, not prerequisites for receiving new activity.

## Retention and operations

Migration `0009_device_communications` adds a table and indexes; it does not rewrite historical audit/attempt rows. Exchanges accumulate in PostgreSQL's existing persistent volume and survive API/container restarts. Existing in-memory evidence cannot be backfilled after it was discarded. Every response/detail payload is field/size bounded, and list/export responses are bounded independently.

Retention is explicitly keep-until-archived: no TTL, scheduled deletion, dashboard clear button, or automatic purge is enabled. Audit and existing OTA history retain their existing non-deletion rules. Track database size and back it up through the existing PostgreSQL procedure. A timed destructive cleanup/archival policy needs a separately specified retention period and restore contract; this release does not silently choose or execute one. Raw container-log retention is a different host concern and is not represented as durable exchange history.

Deploy backend migration and API before the dashboard. Roll back the dashboard first; keep the additive table when rolling back application code so evidence remains available. The migration downgrade drops the new table and its data and is destructive on a populated environment; the downgrade proof is executed only against disposable test services. No production deployment is part of repository verification.

## Acceptance evidence

Tests cover durable reads after app recreation, no-offer classification, successful manifest and binary bytes/headers, rejection identity semantics, partial transfer, storage outage, recording failure, filters/window validation/stable browsing, authorization, no-store, export limits and sanitization, purge protection, automatic UI refresh, held inspection rows, stale state, keyboard/focus, responsive containment, and accessible dialogs. Full migration, static, API, component, intercepted browser, controlled-real-backend, and build results are recorded in `QUALITY.md` and the paired dashboard verification record.
