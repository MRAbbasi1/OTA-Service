# OTA Server Contract

This document summarizes the authoritative backend obligations for device-facing OTA delivery to connected microcontrollers (ESP32-S3). Backend developers, firmware reviewers, operators, and test authors read it before changing OTA routes or delivery infrastructure. This document does not govern administrative UI behavior.

## Scope

- In scope: request headers, manifest and firmware responses, authentication semantics, signature binding, and transport constraints.
- Out of scope: release-management UI, policy authoring UX, and firmware source implementation.

## Required requests

```text
GET /api/v1/firmware/{device_type}/{platform}/manifest.json
GET /firmware/{device_type}/{platform}/v{version}/{filename}
```

Every request includes `X-Device-Serial`, `X-Device-Mac`, and `X-Device-Token`. The MAC is the raw eFuse-derived value, not the Ethernet network MAC.

## Manifest

The successful response is JSON with exactly `version`, `url`, `md5`, `size`, and `signature`. The signed payload is exactly `<version>|<url>|<md5>|<size>`. The device requires a non-empty signature and accepts only a strictly newer numeric version.

## Firmware response

- `Content-Type` is `application/octet-stream`.
- `Content-Length` equals the signed manifest size and the emitted byte count.
- No response redirects to object storage or another URL.
- Chunked transfer encoding is not supported.

## Status semantics

| Status      | Meaning to the device                                                               |
| ----------- | ----------------------------------------------------------------------------------- |
| `200`       | Continue parsing or downloading                                                     |
| `401`       | Invalid token; three consecutive failures trigger a 24-hour automatic-check lockout |
| `403`       | Definitive forbidden state; immediate 24-hour automatic-check lockout               |
| Other `4xx` | Permanent for the current poll; no retry                                            |
| `429`       | Rate limited; no retry during the current poll                                      |
| `5xx`       | Transient failure; retry with backoff                                               |

No-offer behavior currently uses `404`; because the device treats non-authentication `4xx` as non-retryable, temporary infrastructure failures must use `5xx`.

## Open Questions

None.
