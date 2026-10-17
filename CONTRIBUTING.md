# Contributing to OTA-Service

Thank you for your interest in contributing to **OTA-Service**! This document provides guidelines for setting up your development environment, coding standards, running tests, and submitting contributions.

---

## Code of Conduct

We are committed to providing a welcoming, inclusive, and harassment-free environment for all contributors. Please treat everyone with respect and kindness.

---

## Development Setup

### Prerequisites

- **Python**: `>= 3.13`
- **Package Manager**: [Astral `uv`](https://docs.astral.sh/uv/)
- **Database & Storage**: [Docker & Docker Compose](https://www.docker.com) (for local PostgreSQL 18 & MinIO)

### Installation

```bash
# Fork & clone the repository
git clone https://github.com/<your-username>/ota-service.git
cd ota-service

# Create local environment configuration
cp .env.example .env

# Install dependencies into virtualenv using uv
uv sync --all-groups
```

### Starting Local Services

```bash
# Start PostgreSQL & MinIO
docker compose up -d

# Run database migrations
uv run alembic upgrade head

# Create an initial administrator account
uv run ota-admin create-admin --email admin@example.com --role SUPER_ADMIN
```

---

## Quality Gates & Verification

Before submitting any Pull Request, ensure all local verification checks pass:

```bash
# 1. Format code check
uv run ruff format --check .

# 2. Linting check
uv run ruff check .

# 3. Static type analysis (strict mypy)
uv run mypy app

# 4. Run test suite
uv run pytest

# 5. Build Docker container locally
docker build -t ota-service:test .
```

---

## Branching & Commit Conventions

We follow the [Conventional Commits](https://www.conventionalcommits.org/) specification:

```text
<type>(<scope>): <short imperative summary>

[optional body explaining why this change was made and any compatibility impacts]
```

### Allowed Types:
- `feat`: A new feature or endpoint
- `fix`: A bug fix
- `docs`: Documentation updates only
- `test`: Adding or correcting tests
- `refactor`: Code change that neither fixes a bug nor adds a feature
- `perf`: Performance improvement
- `chore`: Maintenance tasks, dependency updates

---

## Non-Negotiable Architectural Rules

When making changes to the codebase, adhere strictly to these core rules:
1. **Never alter the device OTA wire protocol** (headers, status codes, exact `Content-Length`, no-redirect policy) without cross-hardware review.
2. **Never authenticate devices by network/Ethernet MAC**; raw eFuse MAC is the immutable silicon identity anchor.
3. **Never commit secrets, real IP addresses, or production credentials**.
4. **Never edit an existing Alembic migration**; always create a new incremental migration.
5. **Never mutate published firmware releases**; published binaries are strictly immutable.

---

## Submitting a Pull Request

1. Create a descriptive branch from `main` (`git checkout -b feat/your-feature-name`).
2. Implement your changes along with corresponding unit and integration tests.
3. Verify all quality gates pass locally (`ruff`, `mypy`, `pytest`).
4. Push your branch to GitHub and open a Pull Request using the provided PR template.
