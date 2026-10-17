## Description

Please include a summary of the change, the motivation behind it, and which problem it solves.

Closes #(issue)

## Type of Change

- [ ] Bug fix (non-breaking change which fixes an issue)
- [ ] New feature (non-breaking change which adds functionality)
- [ ] Breaking change (fix or feature that would cause existing functionality to not work as expected)
- [ ] Documentation update
- [ ] Performance improvement or code refactoring

## Architectural Compliance

- [ ] The device-facing OTA wire protocol (headers, status codes, Content-Length) remains intact.
- [ ] No secrets, real IP addresses, or production credentials are included.
- [ ] Existing Alembic migrations were not altered (new migration created if schema changed).
- [ ] Published firmware release immutability is preserved.

## Verification Checklist

- [ ] `uv run ruff check .` passed
- [ ] `uv run ruff format --check .` passed
- [ ] `uv run mypy app` passed with 0 type errors
- [ ] `uv run pytest` passed all relevant test suites
- [ ] Relevant documentation updated
