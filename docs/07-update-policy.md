# OTA Management Platform — Update Policy

## 1. Purpose

The Update Policy subsystem determines which firmware release a device is allowed to receive.

This is the core business logic of the OTA platform.

Policy operates strictly inside the device type's boundary. A release catalog is
scoped to one device type, and a device can only ever be offered releases of its
own device type, so no policy can cross device types, change a device's device
type, or alter the OTA path. The path model is
`docs/16-update-path-and-publication.md`.

---

# 2. Default Policy

If no restrictive policy applies:

```text
Device
  |
  v
Latest published compatible release
```

The latest eligible release is offered.

---

# 3. Policy Scope

Initial implementation supports:

```text
Global Default
Device Type
Specific Device
```

Precedence:

```text
Device
  >
Device Type
  >
Global Default
```

---

# 4. Device-Specific Pin

Example:

```text
Device: 10432
Target: 2.5.8
```

Behavior:

```text
2.5.7 → offer 2.5.8
2.5.8 → no newer offer
2.6.0 → no downgrade offer
```

No policy may make the current firmware install the older pinned version.

---

# 5. Version Range

Example:

```text
Minimum: 2.5.5
Maximum: 2.5.9
```

Only releases within the range are eligible.

---

# 6. OTA Disabled

A policy may explicitly disable OTA.

```text
ota_enabled = false
```

No firmware shall be offered.

---

# 7. Downgrade

Downgrade is forbidden by default.

Example:

```text
Current: 2.7.0
Target: 2.6.0
```

The current firmware cannot install a downgrade even if:

```text
allow_downgrade = true
```

`allow_downgrade` is reserved for a future firmware protocol and must not
cause the current backend to offer an older release.

---

# 8. Version Comparison

Firmware versions must be compared semantically.

Example:

```text
2.10.0 > 2.9.0
```

String comparison must not be used.

---

# 9. Policy Resolution

The decision process:

```text
Load Device
    |
Check Device State
    |
Check OTA Enabled
    |
Find Device Policy
    |
If absent → Find Device Type Policy
    |
If absent → Global Default
    |
Load Published Releases
    |
Filter Compatible Releases
    |
Apply Policy
    |
Apply Version Rules
    |
Select Target
```

---

# 10. No Update

The domain returns a no-offer decision when:

```text
Device inactive
OTA disabled
No published compatible release
Known version already satisfies policy
Policy blocks available releases
```

For device OTA, a no-offer decision is represented by the explicit 404 policy
defined in `docs/15-implementation-decisions.md`; it is not a firmware-defined
“no update” success response. A 200 manifest may only contain an allowed
release, and the response carries the reason so the dashboard can explain why.

The request path naming a different device type or platform also produces a
404, but that is a provisioning/authorization outcome rather than a policy
decision and must be labelled distinctly (`device_type_mismatch`) so it is not
misread as "the fleet is up to date" or as a policy block.

---

# 11. Decision Result

The domain should produce:

```text
UpdateDecision
```

with:

```text
device_id
known_version nullable
known_version_source nullable
update_available
target_version
release_id
policy_id
reason
```

---

# 12. Decision Reasons

Recommended values:

```text
NO_UPDATE
LATEST_AVAILABLE
PINNED_VERSION
VERSION_RANGE
OTA_DISABLED
DEVICE_INACTIVE
NO_ELIGIBLE_RELEASE
POLICY_BLOCKED
```

---

# 13. Policy Priority

Policies of the same scope must have explicit priority.

Database insertion order must never determine behavior.

---

# 14. Time-Bound Policy

Policies may optionally support:

```text
starts_at
ends_at
```

Example:

```text
Do not offer 2.7.0
until 2026-10-01
```

This is useful for future staged rollout.

---

# 15. Policy Conflict

If multiple policies produce conflicting target versions:

1. narrower scope wins
2. higher explicit priority wins
3. ambiguous configuration must be rejected rather than silently guessed

---

# 16. Dashboard Preview

The dashboard should eventually expose a "Why?" explanation.

Example:

```text
Device: 10432

Known version (if available):
2.5.7

Applicable policy:
Device-specific pin

Target:
2.5.8

Decision:
UPDATE_AVAILABLE
```

This makes policy behavior explainable to administrators.

---

# 17. Future Rollout Compatibility

The policy architecture should support future:

```text
Device Groups
Release Channels
Percentage Rollout
Canary Deployment
Maintenance Windows
Geographic/Site Targeting
```

These are future extensions and are not required for the initial deployment.

---

# 18. Critical Principle

The selected release's device type is always the authenticated device's device
type; eligibility logic may not search across device types.

Update eligibility must be calculated in exactly one domain service:

```text
UpdateDecisionService
```

The following must not implement independent eligibility logic:

```text
Manifest endpoint
Dashboard
Firmware endpoint
Background jobs
```

All must use the same decision service.

---

# 19. Implementation Status

Implemented in the policy domain and service layer; the current policy contract
is documented in this section and its API behavior in `docs/09-api-specification.md`.

```text
app/domain/policy.py            definition rules, scope/priority resolution (pure)
app/services/policies.py        policy rows: create, edit, enable/disable, delete,
                                conflict detection, affected devices, resolution
app/services/update_decision.py the only consumer that turns rules into a decision
app/api/v1/policies.py          /api/v1/admin/policies admin surface
alembic 0005_update_policy     update_policies, with scope/type integrity checks
```

Implemented from this document: default policy, global / device-type / device
scope with `device > device_type > global` precedence, version pin, inclusive
version range, OTA disable, explicit priority, `starts_at`/`ends_at` windows,
conflict rejection, the decision result of §11 including `policy_id` and
`policy_scope`, and the §16 preview endpoint.

Two properties are enforced rather than assumed, and they are covered by tests:

* a policy can never cross device types or alter a device's OTA path — it has no
  field that could express either, and candidates are always drawn from the
  authenticated device's own device type (§18);
* a policy that excludes a version withholds its **bytes**, not only its offer,
  so a version cannot be installed from a manifest the device already holds
  (`docs/15-implementation-decisions.md` §10).

Downgrade protection is structural, not a flag: there is no `allow_downgrade`
column, and a pin below the known version yields `NO_UPDATE` rather than an older
image (§7).

Still out of scope, as future rollout features under §17: device groups, release
channels, percentage/canary rollout, maintenance windows, site targeting, and
server-initiated pushes (§17 stays as written: the policy architecture does not
block their addition, but none of them exists yet).
