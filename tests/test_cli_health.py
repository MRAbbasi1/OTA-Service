"""Tests for the ota-health CLI.

The CLI is the project's single source of truth for "is this deployment
healthy?"; these tests pin its exit codes, its output shapes, and its refusal
to raise on a network failure. Nothing here makes a real network call.
"""

from __future__ import annotations

import json
import urllib.error
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import pytest

from app.cli import health

CONFIG = '[public]\napi = "https://api/health"\ncdn = "https://cdn/health"\n'
DASHBOARD_SECTION = '\n[dashboard]\nurl = "https://ota/"\n'


class FakeResponse:
    def __init__(self, status: int, body: bytes = b"") -> None:
        self.status = status
        self._body = body

    def read(self, _size: int) -> bytes:
        return self._body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _http_error(url: str, code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "error", {}, None)  # type: ignore[arg-type]


def _args(path: str, **overrides: object) -> Namespace:
    values: dict[str, object] = {"config": path, "format": "text", "timeout": 1.0, "quiet": True}
    values.update(overrides)
    return Namespace(**values)


class TestProbe:
    def test_a_200_is_ok(self) -> None:
        with patch("urllib.request.urlopen", return_value=FakeResponse(200)):
            result = health.probe("https://x/health", "x", timeout=1)
        assert result.status == "ok"
        assert result.http_code == 200
        assert result.latency_ms >= 0

    @pytest.mark.parametrize("code", [404, 500, 503])
    def test_a_non_200_http_status_is_unavailable(self, code: int) -> None:
        with patch("urllib.request.urlopen", side_effect=_http_error("https://x/health", code)):
            result = health.probe("https://x/health", "x", timeout=1)
        assert result.status == "unavailable"
        assert result.http_code == code

    def test_a_network_error_is_error(self) -> None:
        with patch("urllib.request.urlopen", side_effect=ConnectionError("boom")):
            result = health.probe("https://x/health", "x", timeout=1)
        assert result.status == "error"
        assert result.http_code is None
        assert result.detail is not None and "boom" in result.detail

    def test_probe_never_raises(self) -> None:
        with patch("urllib.request.urlopen", side_effect=RuntimeError("unexpected")):
            result = health.probe("https://x/health", "x", timeout=1)
        assert result.status == "error"


class TestFormatting:
    def test_text_marks_unavailable_as_fail(self) -> None:
        results = [
            health.ProbeResult("api", "u", "ok", 200, 42),
            health.ProbeResult("cdn", "u", "unavailable", 503, 41),
        ]
        out = health.format_text(results)
        assert "api" in out and "ok" in out
        assert "cdn" in out and "FAIL" in out

    def test_text_renders_one_line_per_probe(self) -> None:
        names = ("api", "cdn", "ota", "dashboard")
        results = [health.ProbeResult(name, "u", "ok", 200, 1) for name in names]
        lines = health.format_text(results).splitlines()
        assert len(lines) == 4
        assert [line.split()[0] for line in lines] == list(names)

    def test_json_reports_overall_status_and_probes(self) -> None:
        healthy = json.loads(health.format_json([health.ProbeResult("api", "u", "ok", 200, 42)]))
        degraded = json.loads(
            health.format_json([health.ProbeResult("api", "u", "unavailable", 503, 42)])
        )
        assert healthy["status"] == "ok"
        assert degraded["status"] == "degraded"
        assert degraded["probes"][0]["http_code"] == 503


class TestPublicCommand:
    def test_returns_zero_when_all_probes_succeed(self, tmp_path: Path) -> None:
        config = tmp_path / "health.toml"
        config.write_text(CONFIG)
        with patch("urllib.request.urlopen", return_value=FakeResponse(200)):
            assert health.cmd_public(_args(str(config))) == health.EXIT_OK

    def test_returns_one_when_any_probe_fails(self, tmp_path: Path) -> None:
        config = tmp_path / "health.toml"
        config.write_text(CONFIG)

        def side_effect(url: str, timeout: float) -> FakeResponse:
            if "cdn" in url:
                raise _http_error(url, 503)
            return FakeResponse(200)

        with patch("urllib.request.urlopen", side_effect=side_effect):
            assert health.cmd_public(_args(str(config))) == health.EXIT_UNHEALTHY

    def test_a_network_error_is_unhealthy_not_a_crash(self, tmp_path: Path) -> None:
        config = tmp_path / "health.toml"
        config.write_text(CONFIG)
        with patch("urllib.request.urlopen", side_effect=ConnectionError("boom")):
            assert health.cmd_public(_args(str(config))) == health.EXIT_UNHEALTHY

    def test_missing_config_is_a_usage_error(self, tmp_path: Path) -> None:
        assert health.cmd_public(_args(str(tmp_path / "absent.toml"))) == health.EXIT_USAGE

    def test_invalid_toml_is_a_usage_error(self, tmp_path: Path) -> None:
        config = tmp_path / "health.toml"
        config.write_text("[public\nbroken")
        assert health.cmd_public(_args(str(config))) == health.EXIT_USAGE

    def test_an_empty_config_is_a_usage_error(self, tmp_path: Path) -> None:
        config = tmp_path / "health.toml"
        config.write_text("[public]\n")
        assert health.cmd_public(_args(str(config))) == health.EXIT_USAGE

    def test_the_dashboard_is_probed_when_configured(self, tmp_path: Path) -> None:
        config = tmp_path / "health.toml"
        config.write_text(CONFIG + DASHBOARD_SECTION)
        probed: list[str] = []

        def side_effect(url: str, timeout: float) -> FakeResponse:
            probed.append(url)
            return FakeResponse(200)

        with patch("urllib.request.urlopen", side_effect=side_effect):
            assert health.cmd_public(_args(str(config))) == health.EXIT_OK
        assert probed == ["https://api/health", "https://cdn/health", "https://ota/"]

    def test_a_failing_dashboard_fails_the_run(self, tmp_path: Path) -> None:
        config = tmp_path / "health.toml"
        config.write_text(CONFIG + DASHBOARD_SECTION)

        def side_effect(url: str, timeout: float) -> FakeResponse:
            if url.endswith("ota/"):
                raise _http_error(url, 503)
            return FakeResponse(200)

        with patch("urllib.request.urlopen", side_effect=side_effect):
            assert health.cmd_public(_args(str(config))) == health.EXIT_UNHEALTHY

    def test_the_dashboard_is_skipped_when_absent(self, tmp_path: Path) -> None:
        config = tmp_path / "health.toml"
        config.write_text(CONFIG)
        probed: list[str] = []

        def side_effect(url: str, timeout: float) -> FakeResponse:
            probed.append(url)
            return FakeResponse(200)

        with patch("urllib.request.urlopen", side_effect=side_effect):
            assert health.cmd_public(_args(str(config))) == health.EXIT_OK
        assert probed == ["https://api/health", "https://cdn/health"]

    def test_quiet_suppresses_output(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config = tmp_path / "health.toml"
        config.write_text(CONFIG)
        with patch("urllib.request.urlopen", return_value=FakeResponse(200)):
            health.cmd_public(_args(str(config)))
        assert capsys.readouterr().out == ""

    def test_json_format_is_emitted(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config = tmp_path / "health.toml"
        config.write_text(CONFIG)
        with patch("urllib.request.urlopen", return_value=FakeResponse(200)):
            health.cmd_public(_args(str(config), format="json", quiet=False))
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "ok"
        assert len(payload["probes"]) == 2


class TestInternalCommand:
    def test_returns_zero_when_all_endpoints_answer(self) -> None:
        probed: list[str] = []

        def side_effect(url: str, timeout: float) -> FakeResponse:
            probed.append(url)
            return FakeResponse(200)

        with patch("urllib.request.urlopen", side_effect=side_effect):
            code = health.cmd_internal(
                Namespace(url="http://127.0.0.1:18080", format="text", timeout=1.0, quiet=True)
            )
        assert code == health.EXIT_OK
        assert probed == [
            "http://127.0.0.1:18080/health/live",
            "http://127.0.0.1:18080/health/ready",
            "http://127.0.0.1:18080/health/detail",
        ]

    def test_returns_one_when_any_endpoint_fails(self) -> None:
        def side_effect(url: str, timeout: float) -> FakeResponse:
            if url.endswith("/health/detail"):
                raise _http_error(url, 503)
            return FakeResponse(200)

        with patch("urllib.request.urlopen", side_effect=side_effect):
            code = health.cmd_internal(
                Namespace(url="http://127.0.0.1:18080", format="json", timeout=1.0, quiet=True)
            )
        assert code == health.EXIT_UNHEALTHY

    def test_a_trailing_slash_on_the_base_url_is_tolerated(self) -> None:
        probed: list[str] = []

        def side_effect(url: str, timeout: float) -> FakeResponse:
            probed.append(url)
            return FakeResponse(200)

        with patch("urllib.request.urlopen", side_effect=side_effect):
            health.cmd_internal(
                Namespace(url="http://127.0.0.1:8000/", format="text", timeout=1.0, quiet=True)
            )
        assert probed[0] == "http://127.0.0.1:8000/health/live"


class TestParser:
    def test_public_requires_a_config(self) -> None:
        parser = health.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["public"])

    def test_internal_requires_a_url(self) -> None:
        parser = health.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["internal"])

    def test_version_has_no_required_arguments(self) -> None:
        parser = health.build_parser()
        args = parser.parse_args(["version"])
        assert args.command == "version"


class TestMain:
    def test_missing_config_exits_with_usage_code(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "sys.argv", ["ota-health", "public", "--config", str(tmp_path / "absent.toml")]
        )
        assert health.main() == health.EXIT_USAGE

    def test_version_exits_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["ota-health", "version"])
        assert health.main() == health.EXIT_OK
