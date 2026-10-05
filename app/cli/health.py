"""``ota-health``: reachability and readiness checks for an OTA-Service deployment.

Two audiences, one CLI, no secrets:

* **Public** — the minimal ``/health`` route on each configured hostname, plus
  the optional dashboard URL. No authentication, no dependency names, no
  version. Used by external monitors, by CI after a deploy, and by anyone who
  wants to know whether the three hostnames answer.
* **Internal** — the loopback endpoints (``/health/live``, ``/health/ready``,
  ``/health/detail``). Reachable only from the host or from inside the Compose
  network. Used by the deploy script and by an operator over SSH.

The CLI is deliberately dependency-free (standard library only) so it can run
inside the API image, on a workstation, on a CI runner, and on a Kubernetes
pod without a package manager. The configuration file carries only URLs; any
future bearer token, if one is introduced, would live in an environment
variable, never in this file.

Exit codes are the contract:

```text
0  every probe reported ok
1  at least one probe reported unavailable
2  the CLI could not run (bad config, missing file)
```

Usage examples::

    ota-health public --config deploy/health.toml
    ota-health public --config deploy/health.toml --format json
    ota-health internal --url http://127.0.0.1:18080
    ota-health internal --url http://127.0.0.1:18080 --format json --quiet
    ota-health version
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_UNHEALTHY = 1
EXIT_USAGE = 2

DEFAULT_TIMEOUT_SECONDS = 5.0
PACKAGE_NAME = "ota-service"


@dataclass(frozen=True)
class ProbeResult:
    name: str
    url: str
    status: str  # "ok" | "unavailable" | "error"
    http_code: int | None
    latency_ms: int
    detail: str | None = None


def probe(url: str, name: str, *, timeout: float) -> ProbeResult:
    """One HTTP GET, timed, never raises.

    A 200 is ``ok``; any other HTTP status is ``unavailable``; a transport
    failure is ``error``. The body is drained but never inspected, so the probe
    cannot fail on content that the contract does not define.
    """
    started = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            response.read(256)  # drain, but never look at the body
            code = int(response.status)
        latency_ms = int((time.monotonic() - started) * 1000)
        return ProbeResult(name, url, "ok" if code == 200 else "unavailable", code, latency_ms)
    except urllib.error.HTTPError as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        return ProbeResult(name, url, "unavailable", int(exc.code), latency_ms)
    except Exception as exc:  # noqa: BLE001 - a probe must never raise
        latency_ms = int((time.monotonic() - started) * 1000)
        return ProbeResult(name, url, "error", None, latency_ms, str(exc)[:120])


def load_config(path: str) -> dict[str, Any]:
    """Read the TOML config. Raises ``OSError`` or ``TOMLDecodeError``."""
    config_path = Path(path)
    if not config_path.is_file():
        raise FileNotFoundError(f"health config not found: {config_path}")
    with config_path.open("rb") as handle:
        return tomllib.load(handle)


def format_text(results: list[ProbeResult]) -> str:
    lines: list[str] = []
    for result in results:
        marker = "ok" if result.status == "ok" else "FAIL"
        code = str(result.http_code) if result.http_code is not None else "-"
        line = f"{result.name:<20}  {marker:<6}  ({code}, {result.latency_ms}ms)"
        if result.detail:
            line += f"  {result.detail}"
        lines.append(line)
    return "\n".join(lines)


def format_json(results: list[ProbeResult]) -> str:
    healthy = all(result.status == "ok" for result in results)
    return json.dumps(
        {
            "status": "ok" if healthy else "degraded",
            "probes": [asdict(result) for result in results],
        },
        indent=2,
        sort_keys=True,
    )


def emit(results: list[ProbeResult], *, fmt: str, quiet: bool) -> None:
    if quiet:
        return
    text = format_json(results) if fmt == "json" else format_text(results)
    print(text)


def _timeout(args: argparse.Namespace, config: dict[str, Any] | None) -> float:
    if args.timeout is not None:
        return float(args.timeout)
    thresholds = config.get("thresholds") if config else None
    if isinstance(thresholds, dict):
        value = thresholds.get("timeout_seconds")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return DEFAULT_TIMEOUT_SECONDS


def _public_targets(config: dict[str, Any]) -> list[tuple[str, str]]:
    """The probe targets, in output order: public hostnames, then dashboard."""
    public = config.get("public") or {}
    targets: list[tuple[str, str]] = []
    if isinstance(public, dict):
        targets.extend((name, url) for name, url in public.items() if isinstance(url, str) and url)
    dashboard = config.get("dashboard")
    if isinstance(dashboard, dict):
        dashboard_url = dashboard.get("url")
        if isinstance(dashboard_url, str) and dashboard_url:
            targets.append(("dashboard", dashboard_url))
    return targets


def cmd_public(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        print(f"ota-health: {exc}", file=sys.stderr)
        return EXIT_USAGE
    targets = _public_targets(config)
    if not targets:
        print("ota-health: no [public] or [dashboard] URLs configured", file=sys.stderr)
        return EXIT_USAGE
    timeout = _timeout(args, config)
    results = [probe(url, name, timeout=timeout) for name, url in targets]
    emit(results, fmt=args.format, quiet=args.quiet)
    return EXIT_OK if all(result.status == "ok" for result in results) else EXIT_UNHEALTHY


def cmd_internal(args: argparse.Namespace) -> int:
    base = args.url.rstrip("/")
    timeout = _timeout(args, None)
    endpoints = {
        "live": f"{base}/health/live",
        "ready": f"{base}/health/ready",
        "detail": f"{base}/health/detail",
    }
    results = [probe(url, name, timeout=timeout) for name, url in endpoints.items()]
    emit(results, fmt=args.format, quiet=args.quiet)
    return EXIT_OK if all(result.status == "ok" for result in results) else EXIT_UNHEALTHY


def cmd_version(_args: argparse.Namespace) -> int:
    """Print the installed application version.

    Read from package metadata rather than application settings: ``version``
    must work without a configured database or object storage, and the console
    script only exists when the project is installed.
    """
    try:
        print(metadata.version(PACKAGE_NAME))
    except metadata.PackageNotFoundError:
        print("unknown")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ota-health",
        description="Reachability and readiness checks for an OTA-Service deployment.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    public = sub.add_parser(
        "public",
        help="Probe the public /health on every configured hostname (and dashboard).",
    )
    public.add_argument("--config", required=True, help="Path to health.toml")
    public.add_argument("--format", choices=["text", "json"], default="text")
    public.add_argument("--timeout", type=float, default=None)
    public.add_argument("--quiet", action="store_true", help="Suppress output; exit code only.")
    public.set_defaults(func=cmd_public)

    internal = sub.add_parser(
        "internal",
        help="Probe the loopback health endpoints of a running instance.",
    )
    internal.add_argument(
        "--url",
        required=True,
        help="Base URL of the loopback API, e.g. http://127.0.0.1:18080",
    )
    internal.add_argument("--format", choices=["text", "json"], default="text")
    internal.add_argument("--timeout", type=float, default=None)
    internal.add_argument("--quiet", action="store_true")
    internal.set_defaults(func=cmd_internal)

    version = sub.add_parser("version", help="Print the deployed application version.")
    version.set_defaults(func=cmd_version)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except Exception as exc:  # noqa: BLE001 - a CLI must produce a stable exit code
        print(f"ota-health: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
