from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime

from app.api.v1.evidence import export_evidence
from app.api.v1.ota import _observed_stream
from app.db.models import DeviceCommunication
from app.services.communications import CommunicationRecorder
from app.services.ota import OtaResponse
from tests.conftest import FakeObjectStream, build_app, create_admin_account, login
from tests.test_ota_api import (
    ARTIFACT,
    CODE,
    FILENAME,
    PLATFORM,
    VERSION,
    create_type,
    enroll,
    headers,
    publish_release,
    upload_release,
)

PATH = f"/api/v1/firmware/{CODE}/{PLATFORM}/manifest.json"


def test_receipts_rejections_no_offer_and_restart_are_durable(client, fake_storage):
    kind = create_type(client)
    device = enroll(client, kind)
    assert client.get(PATH, headers=headers(device)).status_code == 404
    rejected = headers(device) | {"X-Device-Token": "NEVER_STORE_THIS_SECRET"}
    assert client.get(PATH, headers=rejected).status_code == 401
    result = client.get("/api/v1/admin/communications").json()
    assert result["total"] == 2
    bad, normal = result["items"]
    assert bad["outcome"] == "auth_rejected"
    assert bad["reason"] == "invalid_token"
    assert bad["device_id"] is None
    assert normal["device_id"] == device["id"]
    assert normal["outcome"] == "no_offer"
    assert "NEVER_STORE_THIS_SECRET" not in json.dumps(result)
    assert device["token"] not in json.dumps(result)
    assert normal["detail"]["response"]["body"]["code"] == "no_offer"
    # Fresh process/app instance with the same DB must see the same history.
    restarted = build_app(fake_storage)
    try:
        login(restarted, "super.admin@ota-service.test", "test-super-admin-password")
        assert restarted.get("/api/v1/admin/communications").json()["total"] == 2
    finally:
        restarted.__exit__(None, None, None)


def test_manifest_and_complete_binary_preserve_contract_and_evidence(client):
    kind = create_type(client)
    device = enroll(client, kind)
    publish_release(client, upload_release(client, kind))
    response = client.get(PATH, headers=headers(device))
    assert response.status_code == 200
    assert int(response.headers["content-length"]) == len(response.content)
    binary = client.get(
        f"/firmware/{CODE}/{PLATFORM}/v{VERSION}/{FILENAME}", headers=headers(device)
    )
    assert binary.content == ARTIFACT
    rows = client.get("/api/v1/admin/communications").json()["items"]
    assert [row["outcome"] for row in rows] == ["download_served", "offered"]
    assert rows[0]["bytes_sent"] == len(ARTIFACT)
    assert rows[0]["detail"]["response"]["body_capture"] == "binary_not_captured"
    assert rows[1]["detail"]["response"]["body"]["version"] == VERSION
    assert [e["event"] for e in rows[0]["detail"]["events"]] == [
        "request_received",
        "streaming",
        "download_served",
    ]


def test_unknown_identity_filter_snapshot_and_export(client):
    client.get(
        PATH,
        headers={
            "X-Device-Serial": "9988",
            "X-Device-Mac": "00:11:22:33:44:55",
            "X-Device-Token": "secret",
        },
    )
    first = client.get("/api/v1/admin/communications", params={"serial": "998"}).json()
    client.get(PATH)
    frozen = client.get(
        "/api/v1/admin/communications", params={"snapshot": first["snapshot"]}
    ).json()
    assert frozen["total"] == 1
    for format in ("csv", "json"):
        response = client.get(
            "/api/v1/admin/communications/export", params={"serial": "998", "format": format}
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["exported"] == 1
        assert "9988" in response.json()["content"]
        assert '"secret"' not in response.json()["content"]
    detail = client.get(f"/api/v1/admin/communications/{first['items'][0]['id']}").json()
    assert detail["device_id"] is None
    assert detail["reason"] == "unknown_device"


def test_full_bigint_serial_is_preserved_and_searchable(client):
    kind = create_type(client)
    device = enroll(client, kind, serial=10_000_000_001)
    client.get(PATH, headers=headers(device))
    response = client.get("/api/v1/admin/communications", params={"serial": "10000000001"})
    assert response.status_code == 200
    assert response.json()["items"][0]["claimed_serial"] == "10000000001"


def test_five_rejected_checks_are_visible_without_attributing_seen_to_device(client):
    kind = create_type(client)
    device = enroll(client, kind)
    bad = headers(device) | {"X-Device-Token": "INVALID_NEVER_RETAIN"}
    for _ in range(5):
        assert client.get(PATH, headers=bad).status_code == 401
    response = client.get(
        "/api/v1/admin/communications",
        params={"serial": "000" + headers(device)["X-Device-Serial"], "period": "24h"},
    ).json()
    assert response["total"] == 5
    assert all(row["device_id"] is None for row in response["items"])
    assert "INVALID_NEVER_RETAIN" not in json.dumps(response)
    trusted = client.get("/api/v1/admin/communications", params={"device_id": device["id"]}).json()
    assert trusted["total"] == 0
    detail = client.get(f"/api/v1/admin/devices/{device['id']}").json()
    assert detail["last_seen_at"] is None
    summary = client.get("/api/v1/admin/communications/summary").json()
    assert summary["total"] == 5
    assert summary["by_outcome"]["auth_rejected"] == 5
    assert summary["capture"] is None
    diagnostics = client.get("/api/v1/admin/communications/diagnostics").json()
    assert diagnostics["received_in_process"] == 5
    assert diagnostics["status"] == "ready"


def test_wrong_ota_route_and_method_are_recorded_before_route_resolution(client):
    request_headers = {"X-Device-Serial": "5566", "X-Device-Token": "HIDE_QUERY_AND_HEADERS"}
    assert (
        client.get(
            "/api/v1/firmware/type/platform/wrong?token=HIDE_QUERY_AND_HEADERS",
            headers=request_headers,
        ).status_code
        == 404
    )
    assert client.post(PATH, headers=request_headers).status_code == 405
    rows = client.get("/api/v1/admin/communications", params={"serial": "5566"}).json()["items"]
    assert len(rows) == 2
    assert {row["status_code"] for row in rows} == {404, 405}
    assert all(row["outcome"] == "request_rejected" for row in rows)
    assert all(row["detail"]["request"]["route"] is None for row in rows)
    assert "HIDE_QUERY_AND_HEADERS" not in json.dumps(rows)


def test_invalid_mac_and_out_of_range_serial_do_not_raise_internal_errors(client):
    kind = create_type(client)
    device = enroll(client, kind)
    assert client.get(PATH, headers=headers(device) | {"X-Device-Mac": "broken"}).status_code == 403
    assert (
        client.get(
            PATH, headers=headers(device) | {"X-Device-Serial": "9999999999999999999"}
        ).status_code
        == 403
    )
    rows = client.get("/api/v1/admin/communications").json()["items"]
    assert len(rows) == 2
    assert all(row["outcome"] == "auth_rejected" for row in rows)


def test_capture_failure_is_visible_and_recovers_after_successful_write(client):
    def fail(*args, **kwargs):
        raise RuntimeError("PRIVATE_CREDENTIAL_FAILURE")

    CommunicationRecorder(
        fail,
        request_id=None,
        kind="manifest",
        serial="123",
        mac=None,
        token_present=False,
        parameters={},
        observe=client.app.state.monitoring.record_capture,
    )
    diagnostics = client.get("/api/v1/admin/communications/diagnostics")
    assert diagnostics.headers["cache-control"] == "no-store"
    assert diagnostics.json()["status"] == "degraded"
    assert diagnostics.json()["write_failures_in_process"] == 1
    assert "PRIVATE_CREDENTIAL_FAILURE" not in diagnostics.text
    # Dependency availability and historical recording health are distinct checks.
    assert client.get("/api/v1/admin/monitoring/status").json()["recording"] is None
    client.get(PATH)
    assert client.get("/api/v1/admin/communications/diagnostics").json()["status"] == "ready"


def test_normal_evidence_reads_do_not_run_recording_diagnostics(client, monkeypatch):
    from app.api.v1 import communications

    def unexpected(*args, **kwargs):
        raise AssertionError("Recording diagnostics must be explicitly requested")

    client.get(PATH)
    monkeypatch.setattr(communications, "recording_health", unexpected)
    for suffix in ("", "/summary", "/export", "/1"):
        assert client.get("/api/v1/admin/communications" + suffix).status_code == 200
    assert client.get("/api/v1/admin/monitoring/status").status_code == 200


def test_communication_window_and_order_are_validated(client):
    assert (
        client.get("/api/v1/admin/communications", params={"since": "2026-01-01"}).status_code
        == 422
    )
    assert client.get("/api/v1/admin/communications", params={"sort": "bad"}).status_code == 422
    assert (
        client.get(
            "/api/v1/admin/communications",
            params={"since": "2026-02-01T00:00:00Z", "until": "2026-01-01T00:00:00Z"},
        ).status_code
        == 422
    )


def test_communications_enforce_permissions(anonymous_client):
    for path in ("", "/summary", "/diagnostics", "/export", "/1"):
        assert anonymous_client.get("/api/v1/admin/communications" + path).status_code == 401
    create_admin_account(
        anonymous_client.app, "viewer@test.example", "viewer-test-password", "viewer"
    )
    login(anonymous_client, "viewer@test.example", "viewer-test-password")
    for path in ("", "/summary", "/diagnostics", "/export", "/1"):
        assert anonymous_client.get("/api/v1/admin/communications" + path).status_code == 403


def test_interrupted_stream_never_becomes_completed(client):
    observer = CommunicationRecorder(
        client.app.state.session_factory,
        request_id=None,
        kind="firmware",
        serial="123",
        mac=None,
        token_present=True,
        parameters={},
    )
    stream = FakeObjectStream(b"a" * 150_000)
    result = OtaResponse(200, stream=stream, content_length=150_000)
    observer.response(200, b"", streaming=True, size=150_000)
    chunks = _observed_stream(result, observer)
    next(chunks)
    chunks.close()
    assert stream.closed
    row = client.get(f"/api/v1/admin/communications/{observer.id}").json()
    assert row["outcome"] == "transfer_incomplete"
    assert row["bytes_sent"] < row["bytes_expected"]


def test_export_refuses_silent_truncation(client):
    with client.app.state.session_factory.begin() as session:
        session.add_all(
            [
                DeviceCommunication(
                    request_id="00000000-0000-0000-0000-000000000000",
                    kind="manifest",
                    outcome="received",
                    detail={},
                    created_at=datetime.now(UTC),
                )
                for _ in range(1001)
            ]
        )
    response = client.get("/api/v1/admin/communications/export")
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "export_limit_exceeded"


def test_logging_failure_is_contained_on_receipt_and_completion(client):
    def fail(*args, **kwargs):
        raise RuntimeError("private failure")

    recorder = CommunicationRecorder(
        fail,
        request_id=None,
        kind="manifest",
        serial=None,
        mac=None,
        token_present=False,
        parameters={},
    )
    assert recorder.id is None
    working = CommunicationRecorder(
        client.app.state.session_factory,
        request_id=None,
        kind="manifest",
        serial="123",
        mac=None,
        token_present=False,
        parameters={},
    )
    working.factory = fail
    working.response(404, b'{"code":"no_offer"}', streaming=False, size=19)
    # Failed persistence must leave the receipt unconfirmed, never invent success.
    row = client.get(f"/api/v1/admin/communications/{working.id}").json()
    assert row["outcome"] == "received"
    assert row["completed_at"] is None


def test_storage_failure_is_retained_as_service_error(client, fake_storage):
    kind = create_type(client)
    device = enroll(client, kind)
    publish_release(client, upload_release(client, kind))
    fake_storage.objects.clear()
    assert client.get(PATH, headers=headers(device)).status_code == 503
    row = client.get("/api/v1/admin/communications").json()["items"][0]
    assert row["outcome"] == "server_error"
    assert row["reason"] == "STORAGE_UNAVAILABLE"
    assert row["device_id"] == device["id"]
    detail = client.get(f"/api/v1/admin/devices/{device['id']}").json()
    assert detail["last_seen_at"] is not None
    assert detail["last_manifest_check_at"] is not None


def test_no_offer_is_not_reported_as_generic_request_failure(client):
    kind = create_type(client)
    device = enroll(client, kind)
    response = client.get(PATH, headers=headers(device))
    request_id = response.headers["X-Request-ID"]
    events = client.get("/api/v1/admin/monitoring/events", params={"request_id": request_id}).json()
    assert all(event["event"] != "request_failed" for event in events["items"])


def test_audit_export_is_separate_and_sanitized(client):
    create_type(client)
    response = client.get("/api/v1/admin/audit-events/export", params={"format": "json"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    rows = json.loads(response.json()["content"])
    assert any(row["action"] == "device_type_created" for row in rows)
    assert all("outcome" not in row for row in rows)


def test_receipts_survive_unfinished_process_and_block_purge(client):
    kind = create_type(client)
    device = enroll(client, kind, activate=False)
    client.get(PATH, headers=headers(device))
    detail = client.get(f"/api/v1/admin/devices/{device['id']}").json()
    assert not detail["can_purge"]
    assert client.delete(f"/api/v1/admin/devices/{device['id']}").status_code == 409


def test_shared_csv_export_neutralizes_formulas_without_changing_json():
    rows = [{"actor": "  =DANGEROUS()", "detail": {"text": "<script>inert</script>"}}]
    exported = export_evidence(rows, format="csv", name="test", snapshot=1)
    parsed = list(csv.DictReader(io.StringIO(exported.content)))
    assert parsed[0]["actor"] == "'  =DANGEROUS()"
    assert json.loads(parsed[0]["detail"]) == rows[0]["detail"]
    raw = export_evidence(rows, format="json", name="test", snapshot=1)
    assert json.loads(raw.content) == rows
