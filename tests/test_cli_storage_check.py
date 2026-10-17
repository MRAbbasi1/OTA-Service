from __future__ import annotations

import pytest

from app.cli.storage_check import CANARY, check_storage, main
from app.storage.base import StorageUnavailableError
from tests.conftest import FakeObjectStorage


def test_storage_check_writes_reads_and_removes_canary() -> None:
    storage = FakeObjectStorage()

    check_storage(storage)

    assert len(storage.removed) == 1
    assert storage.removed[0].startswith("_health/")
    assert storage.objects == {}


def test_storage_check_detects_corrupt_read() -> None:
    storage = FakeObjectStorage()
    storage.get_bytes = lambda _key: CANARY[:-1]  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="integrity"):
        check_storage(storage)

    assert len(storage.removed) == 1


def test_cli_returns_failure_without_exposing_storage_detail(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class FailingStorage:
        def __init__(self, _settings: object) -> None:
            pass

        def ensure_bucket(self) -> None:
            raise StorageUnavailableError("private endpoint and credentials")

    monkeypatch.setattr("app.cli.storage_check.MinioObjectStorage", FailingStorage)

    assert main() == 1
    captured = capsys.readouterr()
    assert "private endpoint" not in captured.err
