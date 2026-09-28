"""Create the first administrator.

This is the only way an account comes into existence without an existing
administrator, and it is a deployment command rather than an API endpoint, so it
requires access to the environment the service already runs in
(`docs/15-implementation-decisions.md` §8).

Properties that matter operationally:

* **idempotent** — safe to run on every deploy; if any account exists it reports
  that and changes nothing, so it cannot reset a password an operator set later;
* **one-time credentials from the environment** — `BOOTSTRAP_ADMIN_EMAIL` and
  `BOOTSTRAP_ADMIN_PASSWORD`, which should be removed from the deployment's
  secret set once it has run;
* **audited** — the creation is recorded with `system` as the actor;
* **silent about secrets** — the password is never echoed, logged, or recorded.

Usage:

```bash
BOOTSTRAP_ADMIN_EMAIL=admin@example.com BOOTSTRAP_ADMIN_PASSWORD='...' uv run ota-admin
```
"""

from __future__ import annotations

import logging
import os
import sys

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.models import AdminRole
from app.db.session import Database
from app.domain.errors import DomainError
from app.services.admin_users import AdminUserService
from app.services.audit import AuditService

logger = logging.getLogger("app.cli.bootstrap_admin")

EMAIL_ENV = "BOOTSTRAP_ADMIN_EMAIL"
PASSWORD_ENV = "BOOTSTRAP_ADMIN_PASSWORD"

EXIT_OK = 0
EXIT_INVALID = 2


def main() -> int:
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    email = os.environ.get(EMAIL_ENV, "").strip()
    password = os.environ.get(PASSWORD_ENV, "")
    if not email or not password:
        print(
            f"error: both {EMAIL_ENV} and {PASSWORD_ENV} are required, and neither is printed.",
            file=sys.stderr,
        )
        return EXIT_INVALID

    database = Database(get_settings())
    try:
        session = database.session()
        try:
            service = AdminUserService(session)
            try:
                admin, created = service.bootstrap_super_admin(email, password)
            except DomainError as exc:
                # Codes only: the values that failed validation are credentials.
                session.rollback()
                print(f"error: {exc.code}", file=sys.stderr)
                return EXIT_INVALID
            if created:
                AuditService(session).record(
                    "system",
                    "admin_bootstrapped",
                    "admin_user",
                    admin.id,
                    {"email": admin.email, "role": AdminRole.SUPER_ADMIN.value},
                )
                session.commit()
                logger.info(
                    "admin_bootstrapped",
                    extra={"extra_fields": {"admin_id": admin.id}},
                )
        finally:
            session.close()
    finally:
        database.dispose()

    if created:
        print(f"created SUPER_ADMIN {admin.email} (id {admin.id})")
        print(f"remove {EMAIL_ENV} and {PASSWORD_ENV} from the deployment's secrets now.")
    else:
        print(f"an administrator already exists ({admin.email}); nothing was changed.")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
