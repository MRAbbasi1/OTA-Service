# 4. HttpOnly Session Cookies and Double-Submit CSRF Protection

Date: 2026-10-17

## Status

Accepted

## Context

Single Page Applications (SPAs) frequently store JWTs in browser `localStorage` or `sessionStorage` and send them via `Authorization: Bearer <token>` headers. This pattern is vulnerable to Cross-Site Scripting (XSS) attacks, as any injected script can read the token and exfiltrate administrative session credentials.

## Decision

Administrative session state is secured via **HttpOnly cookies paired with double-submit CSRF protection**:

1. **Session Cookie**:
   - The JWT is stored in an `HttpOnly`, `SameSite=Lax` cookie (`ota_admin_session`).
   - JavaScript has zero access to the cookie content.
   - Enforced `Secure` flag in production environments.
2. **Double-Submit CSRF**:
   - A cryptographically random CSRF token is issued in a separate non-HttpOnly cookie (`ota_admin_csrf`).
   - The frontend reads this token and sends it in the `X-CSRF-Token` HTTP header on all mutating requests (`POST`, `PATCH`, `DELETE`).
   - The backend validates that the header matches the cookie token before processing state changes.
3. **Instant Revocation**:
   - Admin user records track an integer `session_version`. Incrementing this version (e.g. on password change or manual logout) invalidates all active session tokens immediately.

## Consequences

- **Positive**: Total immunity to credential exfiltration via XSS.
- **Positive**: Full protection against Cross-Site Request Forgery (CSRF).
- **Negative**: Requires same-origin or explicit reverse-proxy architecture in production; cross-origin API hosting without shared cookie domain is disallowed.
