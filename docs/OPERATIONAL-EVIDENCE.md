# Operational evidence: architecture and investigation

## Acceptance case

Five unsuccessful device requests must produce five distinguishable receipts when they reach the API's OTA prefixes and PostgreSQL can persist them. Neither a missing update offer nor a failed authentication is an absence of traffic. The browser must show new evidence without a reload, navigation away/back, or a live switch. Production receipt of the user's original five attempts is not established by repository or local test evidence.

## Sources and truthful meaning

| Question | Authoritative source | Persistence / boundary |
| --- | --- | --- |
| What arrived, what was returned, why? | PostgreSQL device_communications | One receipt per API request under the two OTA prefixes, before handler/dependency resolution; completion updates the same record. |
| Which device was actually authenticated? | device_id on the receipt; Device.last_seen_at | Set only after all identity/token/status checks pass. Claimed serial/MAC are not identity. |
| Did a manifest check occur? | Device.last_manifest_check_at | Last authenticated decision time, including a later storage failure; not a count of requests. |
| How many requests failed or succeeded? | GET /communications/summary | Server-UTC rolling 24-hour counts of persisted receipts, including rejected identities and invalid routes/methods. |
| Which version was offered or emitted? | UpdateAttempt + receipt decision/delivery snapshot | Deduplicated offers and served downloads are not all HTTP requests, device receipt, flash or boot confirmation. |
| Who changed a resource? | PostgreSQL audit_events | Administrative mutation and audit insertion commit together. Device scope includes newly correlated credential actions. |
| Is this API process healthy? | monitoring/status and source=system faults | Current dependencies and a bounded 500-event/sample process buffer; resets at restart. Recording diagnostics are a separate explicit snapshot. |
| Did Nginx reject or fail before the API? | Private structured device edge access log | Not ingested into API history. May contain only an unverified numeric serial; no raw URI/query or credentials. |
| Did DNS/TLS/connectivity fail before HTTP? | Device diagnostics and private edge/TLS diagnostics | Not observable as an API receipt; never inferred from an empty table. |

PostgreSQL history survives app/container restart in its persistent volume. There is no TTL, clear-history action, automatic archive or purge. Backups, capacity monitoring and an approved retention/restore policy remain required operational responsibilities. The process ring is intentionally transient, not a second durable log database.

## Fixes and design boundaries

- Receipt capture moved from matched handlers to the existing request middleware. Wrong OTA subpaths and unsupported methods now yield request_rejected evidence; normal matched 404 no-offer remains no_offer. Wire statuses, response bytes, signatures and Content-Length are unchanged.
- Empty, unavailable and incomplete history are separate states. Actual read failures return 503 communication_history_unavailable on list, summary, detail and export, rather than a false empty success. The recording diagnostics endpoint reports unavailable/degraded recording separately from dependency availability; a database ping alone never proves recording health.
- Capture diagnostics report received receipts, failed writes, last persisted/failure times and process start. They are process-local. Readable/ready means the table is readable and no later failed write is observed in this process, not a lossless guarantee or a synthetic write probe.
- Receipt failure does not turn a valid OTA response into a different status/body. An unfinished receipt remains unconfirmed. Database outages can create gaps; there is no hidden fallback queue.
- Valid authenticated contact and check timestamps are committed on storage/preparation failures too. Unauthenticated attempts do not update last seen.
- Invalid MAC syntax and out-of-bigint serial values follow the existing forbidden authentication semantics rather than accidental database/domain 500 errors.
- Administrative mutations no longer commit first and audit later. Firmware upload includes its audit event in the existing commit/rollback/object-cleanup boundary. Audit failure rolls back the mutation; upload failure also discards newly uploaded objects.
- Token actions carry a safe device_id for device history. Revoke checks token ownership against the URL device, like rotate already did. Older uncorrelated credential events are not falsely backfilled.
- Reviewed policy updates and release lifecycle transitions include safe before/after fields. Arbitrary prose, credentials and old unknown values are not reconstructed.
- Request correlation uses UUIDs. API request logs use route templates, not raw paths. Exception diagnostics retain class and up to 12 file/line/function locations without exception messages, SQL parameters, source lines or locals. Production Uvicorn access logging is disabled; the application and reviewed Nginx log supply safe correlation.
- System faults include transient OTA preparation, storage-stream-open and integrity/configuration failures, not just selected stream failures.

No collector, queue, Redis, tracing SDK, push transport, generic telemetry framework or client-side decision engine was added. Request UUID plus safe event timeline is the current trace mechanism; metrics describe either persisted receipt counts or an explicitly labeled process sample.

## Shared read contract

Existing logs:read authorization is unchanged. Communications accepts device_id (authenticated linkage), device_type_id (authenticated enrolled type), release_id (recorded decision/delivery), policy_id (recorded manifest decision), serial prefix (unverified claims), kind, outcome, request_id, sort, snapshot and time filters. Resource links supply identifiers; operators do not type them.

period=1h|24h|7d|all is resolved using server UTC. Explicit timezone-aware since/until still work. A held historical page/export uses the applied since and insertion ceiling; it is not a transaction snapshot of later completion updates. Serial prefix search uses canonical numeric serials, including searches pasted with leading zeroes.

GET /communications/summary returns total, by_outcome, last_received_at and since/until. The legacy capture/recording fields on list, summary and monitoring/status are now optional and null: ordinary reads no longer execute a diagnostic query in a second database session. Actual history queries retain their explicit unavailable failure mapping. Dependency status describes dependencies, not recording guarantees.

GET /communications/diagnostics requires logs:read and returns RecordingHealth with Cache-Control: no-store. It runs one bounded latest-receipt read plus an in-memory snapshot of this API process's capture counters. Unreadable history is a successful diagnostic response with status=unavailable and schema_available=false, not an empty healthy result. Session/capability and transport failures remain ordinary HTTP errors. This is a read-only check, never a synthetic device request or write test. The dashboard issues it only after Run checks; it has no polling, focus/reconnect refetch, mutation invalidation or automatic retry. Leaving the route discards its UI result. Re-run explicitly to replace the timestamped snapshot. Release the API before the dashboard; older APIs return an honest unsupported/error state, never fabricated zero counts.

CSV and JSON exports retain the existing 1,000-match ceiling, explicit over-limit failure, sanitization, no-store responses and capability checks. Per-resource administrative links use the same audit/export endpoint. JSON evidence is inert text; CSV formula-like cells are escaped.

## Freshness and browsing

The visible newest communications/overview update every 5 seconds, resource data and first-page admin history every 10 seconds, dependency checks (including the shell public probe) every 30 seconds. Shared foreground-query handling gates initial reads, intervals and invalidations by document visibility and browser online state. Hiding the tab or going offline cancels in-flight reads using their consumed AbortSignals. Route unmount releases the observer and cancels its request when no other consumer remains. Below-fold request overview and per-resource evidence load/poll only while intersecting the viewport; disabling one observer does not cancel a query still required by another visible observer. No timer for an unmounted route remains.

Returning to a visible/online current screen reconciles stale data through TanStack Query's existing cache and request deduplication. Resource/current communication and service-event queries use three-second freshness; historical pages use infinite stale time to hold position (explicit invalidation remains authoritative). The shell and dependency probes use 15-second freshness. No new scheduler, global data store or cross-tab leader election is introduced. A visible side-by-side window remains eligible; merely losing keyboard focus does not disable a displayed operations console. Closing the app/tab stops its browser work. This UI polling is not an always-on production monitoring agent. Client cancellation cannot undo server work already executed; it stops the outstanding browser read and future scheduling, not a committed action.

GETs have a 10-second deadline; failed reads show interrupted/stale or unknown, not a successful zero. “Last checked” is the time the browser successfully fetched a response, not the time a device sent a request. Diagnostics separately label their server checked_at, latest stored receipt and process start. This is bounded polling, not a subsecond push/availability guarantee.

Older pages and alternate communication sorting stay stable. Inspection holds list rows while detail and incoming activity are fetched. Forms keep drafts while server data refreshes. Policy submissions send only edited fields, avoiding overwriting untouched values from older defaults. Concurrent changes to the same field still require operator review; this release does not introduce versioned optimistic locking.

The dashboard separates persistent request counts from Devices checked and deduplicated offers/downloads. Device/type/release/policy details expose scoped request history and administrative history when capabilities allow them. Device history offers a separate claimed-serial search to investigate unauthenticated failures.

Foreground polling must fit the security budget: read requests are independently bounded at 300/minute/account; unsafe administrative requests retain 120/minute/account. Login and device limits are unchanged. Reads cannot exhaust the write bucket; 429 includes Retry-After. A conservative device-detail estimate is about 50 reads/minute if all its evidence is visible, before navigation/actions; offscreen evidence and hidden tabs use fewer. Multiple visible windows each have their own polling/cache and still consume the shared account limit. This is not a fleet-throughput benchmark.

## Investigation runbook

1. Open Recording diagnostics on Service health or Device communications and select Run checks. If recording is unavailable/degraded, check the deployed API version, Alembic head (0009 or later), table/read/write permissions, PostgreSQL reachability and safe communication_record_failed events. The snapshot does not update itself; re-run after remediation. A green SELECT 1 or public health probe is insufficient.
2. Open Device communications with all outcomes and All retained history. Search the canonical serial prefix. If necessary clear every resource/request filter. Invalid/missing serial headers cannot match a serial search; inspect unfiltered receipts.
3. Inspect each request UUID, received time, method/route, authenticated identity, HTTP status/reason, request-time decision and transfer evidence. Do not confuse an intentional no_offer with a network failure.
4. If no API receipt exists, inspect the private Nginx device access log for status, upstream_status, route class, claimed serial and edge_id/request_id. An empty upstream status identifies an edge-only response. The API canonicalizes the edge's 32-hex UUID to hyphenated form.
5. If no corresponding HTTP edge request exists, verify configured HTTPS hostname/path, device clock/TLS trust, DNS and connectivity from the device. Neither React polling nor a database migration can recover a request that never reached HTTP.
6. Follow resource administrative history to identify policy, lifecycle, version-assertion or credential changes. Historical decisions must not be recomputed from today's rules.
7. Export the narrow time/device/request evidence and share it only through an authorized support channel. Never export credentials or treat server emission as installation success.

## Rollout and rollback

No new database migration is introduced in this follow-up; the existing additive 0009 table is required. Release backend/API first, verify history and recording health, then release the generated-schema dashboard. Recreate the API container to apply its access-log option. The paired Nginx templates are identical and must be rendered/reviewed and nginx -t checked before host reload. No deployment is performed by this repository audit.

The device edge log is private operational data at /var/log/nginx/ota-device-access.log. Configure restricted ownership/permissions, disk monitoring, and log rotation under the host's approved retention policy before enabling it. No host log deletion or retention period was silently selected. Logs outside this reviewed template, including Nginx error logs, require separate host security review.

Rollback dashboard before API. Keep 0009 and its data; do not downgrade/drop a populated history table to roll back application changes. Recover backups under the existing PostgreSQL restore process, not a dashboard button.

## Verification principles

Acceptance includes five failed checks, automatic browser visibility against real PostgreSQL/API, unavailable schema, recording failure/recovery, trusted identity boundaries, stored decision correlation after policy changes, audit rollback, upload cleanup, token ownership, read/write rate isolation, secret-free diagnostic formatting, byte-level OTA contract tests, exports, accessibility and narrow viewport containment.

These are controlled-local proofs. Production routing, migration state, real TLS/device timing, disk permissions/rotation, expected fleet load and the original five device attempts require deployment-side evidence. No production result or zero-defect guarantee is inferred.

Design references: [OWASP Logging Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html) for data minimization and failure handling; [OpenTelemetry signals](https://opentelemetry.io/docs/concepts/signals/) for distinguishing logs, metrics and traces. These inform boundaries; no new observability platform is required here.

Browser lifecycle references: [TanStack window focus](https://tanstack.com/query/v5/docs/framework/react/guides/window-focus-refetching), [query cancellation](https://tanstack.com/query/v5/docs/framework/react/guides/query-cancellation) and [disabled/manual queries](https://tanstack.com/query/v5/docs/framework/react/guides/disabling-queries). The wrapper supplies scope/visibility eligibility, not a second fetching engine.
