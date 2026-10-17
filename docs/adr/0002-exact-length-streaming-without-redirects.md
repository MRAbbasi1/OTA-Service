# 2. Exact Content-Length Streaming Without HTTP Redirects

Date: 2026-10-17

## Status

Accepted

## Context

Typical cloud file hosting architectures issue pre-signed Amazon S3 / MinIO URLs or utilize global CDN redirects (HTTP 301/302). However:
1. Microcontroller HTTP client stacks (e.g., standard ESP-IDF / Arduino `HTTPClient`) often do not follow HTTP redirects or fail to preserve authentication headers across domain redirects.
2. Many embedded TCP/TLS buffers cannot handle chunked transfer encoding (`Transfer-Encoding: chunked`), requiring an explicit `Content-Length` header matching the flash partition size before flashing can begin.
3. Pre-signed URLs leak download permissions to unauthorized third parties once generated, bypassing real-time revocation checks.

## Decision

All firmware binaries are streamed directly through the application server from private MinIO storage:
1. The edge proxy (Nginx) disables buffering (`proxy_buffering off;`) and compression (`gzip off;`) for all `/firmware/` routes.
2. The FastAPI backend validates device identity and policy permissions synchronously, opens a stream from private MinIO, and sets an exact `Content-Length` header matching the signed manifest size.
3. No HTTP 3xx redirects are issued under any circumstance.

## Consequences

- **Positive**: 100% compatibility with constrained embedded HTTP clients and bootloaders.
- **Positive**: Revoked devices or disabled update policies are blocked instantly at download time, even if they possess a valid manifest.
- **Negative**: Firmware binary bandwidth passes through the application server rather than being directly offloaded to external blob storage.
