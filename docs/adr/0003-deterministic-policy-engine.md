# 3. Deterministic Update Policy Resolution Cascade

Date: 2026-10-17

## Status

Accepted

## Context

Managing firmware deployments across large, heterogeneous fleets requires granular control (canary releases, troubleshooting specific units, freezing updates during production maintenance, or phased rollouts). Ad-hoc query filtering or ambiguous policy matching leads to non-deterministic behavior, accidental downgrades, or conflicting update instructions.

## Decision

We enforce a deterministic, pure-domain priority cascade for resolving the governing update policy:

1. **Precedence Hierarchy**:
   - `Device Scope` (highest precedence — specific device override)
   - `Device Type Scope` (model-wide rules)
   - `Global Scope` (fleet-wide fallback)
2. **Within-Scope Conflict Prevention**:
   - Within any scope, higher `priority` integer values win.
   - Database uniqueness constraints and service validation prevent overlapping policies with identical priorities.
   - In case of an unresolvable conflict, the engine fails closed (`404 No Offer`) to prevent delivering an unintended binary.
3. **Immutability of Publication**:
   - Once a firmware release is marked `PUBLISHED`, its binary bytes, checksums, and version string are strictly immutable.
   - Accidental downgrades (`target_version <= known_version`) are permanently blocked by domain invariants.

## Consequences

- **Positive**: Zero ambiguity; update decisions are pure functions testable in memory without database or network I/O.
- **Positive**: Eliminates unintended firmware rollbacks or conflicting instructions.
- **Negative**: Operators must explicitly manage priority values when configuring multiple overlapping policies.
