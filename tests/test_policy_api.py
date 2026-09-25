"""Admin policy API and the dashboard preview.

The most important test here is the last class: the preview endpoint and the
device endpoint must return the same decision for the same device state, because
that is the whole reason eligibility lives in one service
(`docs/07-update-policy.md`).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import AuditEvent, PolicyScope, PolicyType, UpdatePolicy
from app.domain.errors import DomainError
from app.domain.update_path import firmware_path, manifest_path
from app.services.policies import PolicyService
from tests.conftest import SUPER_ADMIN_EMAIL
from tests.test_ota_api import (
    CODE,
    FILENAME,
    PLATFORM,
    absolute_firmware_url,
    create_type,
    enroll,
    headers,
    publish_release,
    upload_release,
)

pytestmark = pytest.mark.usefixtures("truncate_tables")

OLDER = "2.5.8"
NEWER = "2.6.0"
KNOWN = "2.5.7"
SERIAL_2 = 10433


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    connection = engine.connect()
    try:
        yield Session(bind=connection, expire_on_commit=False)
    finally:
        connection.close()


def create_policy(client: TestClient, **fields: Any) -> dict:
    payload: dict[str, Any] = {"name": "Test policy", "scope": "global", "policy_type": "disable"}
    payload.update(fields)
    response = client.post("/api/v1/admin/policies", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def pin(client: TestClient, version: str, **fields: Any) -> dict:
    return create_policy(client, policy_type="pin", target_version=version, **fields)


def publish(client: TestClient, device_type: dict, version: str) -> dict:
    return publish_release(client, upload_release(client, device_type, version=version))


@pytest.fixture
def device_type(client: TestClient) -> dict:
    return create_type(client)


@pytest.fixture
def device(client: TestClient, device_type: dict) -> dict:
    return enroll(client, device_type)


@pytest.fixture
def known_device(client: TestClient, device: dict) -> dict:
    response = client.post(
        f"/api/v1/admin/devices/{device['id']}/firmware-version", json={"version": KNOWN}
    )
    assert response.status_code == 200, response.text
    return device


@pytest.fixture
def catalog(client: TestClient, device_type: dict) -> dict[str, dict]:
    return {OLDER: publish(client, device_type, OLDER), NEWER: publish(client, device_type, NEWER)}


class TestPolicyCrud:
    def test_create_a_global_pin(self, client: TestClient) -> None:
        policy = pin(client, OLDER, name="Hold the fleet")

        assert policy["scope"] == PolicyScope.GLOBAL.value
        assert policy["policy_type"] == PolicyType.PIN.value
        assert policy["target_version"] == OLDER
        assert policy["priority"] == 0
        assert policy["is_active"] is True
        # The actor is the authenticated administrator, not a literal or a form field.
        assert policy["created_by"] == SUPER_ADMIN_EMAIL

    @pytest.mark.parametrize(
        ("fields", "code"),
        [
            ({"policy_type": "pin"}, "policy_target_required"),
            (
                {"policy_type": "pin", "target_version": OLDER, "min_version": "2.5.0"},
                "policy_version_forbidden",
            ),
            ({"policy_type": "range"}, "policy_target_required"),
            (
                {"policy_type": "range", "min_version": NEWER, "max_version": OLDER},
                "policy_range_invalid",
            ),
            ({"policy_type": "disable", "target_version": OLDER}, "policy_version_forbidden"),
            ({"policy_type": "pin", "target_version": "latest"}, "invalid_version"),
            (
                {"policy_type": "pin", "target_version": OLDER, "device_id": 1},
                "policy_scope_target_forbidden",
            ),
            (
                {"scope": "device", "policy_type": "pin", "target_version": OLDER},
                "policy_scope_target_required",
            ),
            (
                {"scope": "device_type", "policy_type": "pin", "target_version": OLDER},
                "policy_scope_target_required",
            ),
            (
                {
                    "policy_type": "disable",
                    "starts_at": "2026-10-02T00:00:00Z",
                    "ends_at": "2026-10-01T00:00:00Z",
                },
                "policy_window_invalid",
            ),
        ],
    )
    def test_incoherent_policies_are_rejected(
        self, client: TestClient, fields: dict, code: str
    ) -> None:
        response = client.post(
            "/api/v1/admin/policies", json={"name": "bad", "scope": "global", **fields}
        )

        assert response.status_code == 422, response.text
        assert response.json()["detail"]["code"] == code

    def test_a_policy_must_target_something_that_exists(
        self, client: TestClient, device_type: dict
    ) -> None:
        missing_type = client.post(
            "/api/v1/admin/policies",
            json={
                "name": "ghost",
                "scope": "device_type",
                "policy_type": "pin",
                "target_version": OLDER,
                "device_type_id": 999,
            },
        )
        missing_device = client.post(
            "/api/v1/admin/policies",
            json={
                "name": "ghost",
                "scope": "device",
                "policy_type": "pin",
                "target_version": OLDER,
                "device_id": 999,
            },
        )

        assert missing_type.status_code == 404
        assert missing_device.status_code == 404

    def test_two_policies_at_one_scope_and_priority_conflict(
        self, client: TestClient, device: dict
    ) -> None:
        """The service must refuse to guess; ambiguity is an operator's decision."""
        pin(client, OLDER, scope="device", device_id=device["id"])

        response = client.post(
            "/api/v1/admin/policies",
            json={
                "name": "conflicting",
                "scope": "device",
                "policy_type": "range",
                "min_version": NEWER,
                "device_id": device["id"],
            },
        )

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "policy_conflict"

    def test_a_higher_priority_at_the_same_scope_is_allowed(
        self, client: TestClient, device: dict
    ) -> None:
        pin(client, OLDER, scope="device", device_id=device["id"], priority=1)

        override = pin(client, NEWER, scope="device", device_id=device["id"], priority=2)

        assert override["priority"] == 2

    def test_the_same_priority_at_another_scope_is_allowed(
        self, client: TestClient, device_type: dict
    ) -> None:
        pin(client, OLDER)

        scoped = pin(client, NEWER, scope="device_type", device_type_id=device_type["id"])

        assert scoped["priority"] == 0

    def test_disjoint_windows_at_one_priority_do_not_conflict(
        self, client: TestClient, device: dict
    ) -> None:
        pin(
            client,
            OLDER,
            scope="device",
            device_id=device["id"],
            starts_at="2026-09-01T00:00:00Z",
            ends_at="2026-09-10T00:00:00Z",
        )

        later = pin(
            client,
            NEWER,
            scope="device",
            device_id=device["id"],
            starts_at="2026-09-10T00:00:00Z",
            ends_at="2026-09-20T00:00:00Z",
        )

        assert later["id"] > 0

    def test_list_and_filter(self, client: TestClient, device: dict) -> None:
        pin(client, OLDER, name="global")
        pin(client, NEWER, name="per device", scope="device", device_id=device["id"])

        everything = client.get("/api/v1/admin/policies").json()
        scoped = client.get("/api/v1/admin/policies", params={"scope": "device"}).json()
        by_device = client.get("/api/v1/admin/policies", params={"device_id": device["id"]}).json()

        assert everything["total"] == 2
        assert [row["name"] for row in scoped["items"]] == ["per device"]
        assert by_device["total"] == 1

    def test_get_and_missing_policy(self, client: TestClient) -> None:
        policy = pin(client, OLDER)

        assert client.get(f"/api/v1/admin/policies/{policy['id']}").status_code == 200
        assert client.get("/api/v1/admin/policies/424242").status_code == 404


class TestPolicyEditing:
    def test_a_target_change_takes_effect(self, client: TestClient, device: dict) -> None:
        policy = pin(client, OLDER)

        updated = client.patch(
            f"/api/v1/admin/policies/{policy['id']}", json={"target_version": NEWER}
        )

        assert updated.status_code == 200, updated.text
        assert updated.json()["target_version"] == NEWER

    def test_scope_and_type_cannot_be_edited(self, client: TestClient) -> None:
        """Rejected outright rather than ignored, so an operator is never told a
        policy changed scope when it did not."""
        policy = pin(client, OLDER)

        for payload in (
            {"scope": "device"},
            {"policy_type": "disable"},
            {"device_id": 1},
            {"device_type_id": 1},
        ):
            response = client.patch(f"/api/v1/admin/policies/{policy['id']}", json=payload)
            assert response.status_code == 422, payload

        assert client.get(f"/api/v1/admin/policies/{policy['id']}").json()["scope"] == "global"

    def test_the_service_also_refuses_an_immutable_change(
        self, client: TestClient, engine: Engine
    ) -> None:
        policy = pin(client, OLDER)

        with session_scope(engine) as session, pytest.raises(DomainError) as exc:
            PolicyService(session).update(policy["id"], {"scope": PolicyScope.DEVICE})

        assert exc.value.code == "policy_immutable"

    def test_an_edit_that_creates_ambiguity_is_rejected(
        self, client: TestClient, device: dict
    ) -> None:
        pin(client, OLDER, scope="device", device_id=device["id"], priority=1)
        second = pin(client, NEWER, scope="device", device_id=device["id"], priority=2)

        response = client.patch(f"/api/v1/admin/policies/{second['id']}", json={"priority": 1})

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "policy_conflict"

    def test_an_edit_is_revalidated(self, client: TestClient) -> None:
        policy = create_policy(
            client, policy_type="range", min_version="2.5.0", max_version="2.5.9"
        )

        response = client.patch(
            f"/api/v1/admin/policies/{policy['id']}", json={"max_version": "2.4.0"}
        )

        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "policy_range_invalid"

    def test_a_policy_can_be_disabled_and_re_enabled(
        self, client: TestClient, device: dict
    ) -> None:
        policy = pin(client, OLDER, scope="device", device_id=device["id"])

        off = client.post(
            f"/api/v1/admin/policies/{policy['id']}/active", json={"is_active": False}
        )
        assert off.status_code == 200 and off.json()["is_active"] is False
        # The priority slot is free again while the policy is off.
        assert pin(client, NEWER, scope="device", device_id=device["id"])["id"] > 0

        on = client.post(f"/api/v1/admin/policies/{policy['id']}/active", json={"is_active": True})
        assert on.status_code == 409  # re-enabling would recreate the ambiguity

    def test_a_policy_can_be_deleted(self, client: TestClient) -> None:
        policy = pin(client, OLDER)

        deleted = client.delete(f"/api/v1/admin/policies/{policy['id']}")

        assert deleted.status_code == 200
        assert client.get(f"/api/v1/admin/policies/{policy['id']}").status_code == 404


class TestAffectedDevices:
    def test_a_device_policy_reaches_exactly_one_device(
        self, client: TestClient, device_type: dict, device: dict
    ) -> None:
        enroll(client, device_type, serial=SERIAL_2, mac="7C:9E:BD:00:41:01")
        policy = pin(client, OLDER, scope="device", device_id=device["id"])

        affected = client.get(f"/api/v1/admin/policies/{policy['id']}/devices").json()

        assert affected["total"] == 1
        assert [row["id"] for row in affected["items"]] == [device["id"]]

    def test_a_device_type_policy_reaches_that_type_only(
        self, client: TestClient, device_type: dict, device: dict
    ) -> None:
        other_type = create_type(client, code="bcs-sensor-v1")
        enroll(client, other_type, serial=55501, mac="7C:9E:BD:55:50:01")
        policy = pin(client, OLDER, scope="device_type", device_type_id=device_type["id"])

        affected = client.get(f"/api/v1/admin/policies/{policy['id']}/devices").json()

        assert affected["total"] == 1
        assert [row["device_type_id"] for row in affected["items"]] == [device_type["id"]]

    def test_a_global_policy_reports_its_blast_radius(
        self, client: TestClient, device_type: dict, device: dict
    ) -> None:
        enroll(client, device_type, serial=SERIAL_2, mac="7C:9E:BD:00:41:01")
        policy = create_policy(client, policy_type="disable")

        first_page = client.get(
            f"/api/v1/admin/policies/{policy['id']}/devices", params={"page_size": 1}
        ).json()

        assert first_page["total"] == 2
        assert len(first_page["items"]) == 1


class TestDecisionPreview:
    def test_preview_explains_the_latest_release(
        self, client: TestClient, device: dict, catalog: dict[str, dict]
    ) -> None:
        decision = client.get(f"/api/v1/admin/devices/{device['id']}/update-decision").json()

        assert decision["update_available"] is True
        assert decision["reason"] == "LATEST_AVAILABLE"
        assert decision["target_version"] == NEWER
        assert decision["release_id"] == catalog[NEWER]["id"]
        assert decision["firmware_url"] == absolute_firmware_url(NEWER)
        assert decision["policy_id"] is None
        assert "newest published release" in decision["explanation"]

    def test_preview_reports_the_governing_policy(
        self, client: TestClient, known_device: dict, catalog: dict[str, dict]
    ) -> None:
        policy = pin(client, OLDER, name="Hold at 2.5.8")

        decision = client.get(f"/api/v1/admin/devices/{known_device['id']}/update-decision").json()

        assert decision["update_available"] is True
        assert decision["reason"] == "PINNED_VERSION"
        assert decision["target_version"] == OLDER
        assert decision["policy_id"] == policy["id"]
        assert decision["policy_scope"] == "global"
        assert decision["policy_name"] == "Hold at 2.5.8"
        assert "Hold at 2.5.8" in decision["explanation"]

    def test_preview_carries_the_known_version_with_its_provenance(
        self, client: TestClient, known_device: dict, catalog: dict[str, dict]
    ) -> None:
        decision = client.get(f"/api/v1/admin/devices/{known_device['id']}/update-decision").json()

        assert decision["known_version"] == KNOWN
        assert decision["known_version_source"] == "admin_asserted"

    def test_preview_reports_a_disable_policy(
        self, client: TestClient, known_device: dict, catalog: dict[str, dict]
    ) -> None:
        policy = create_policy(client, policy_type="disable", name="Freeze")

        decision = client.get(f"/api/v1/admin/devices/{known_device['id']}/update-decision").json()

        assert decision["update_available"] is False
        assert decision["reason"] == "OTA_DISABLED"
        assert decision["policy_id"] == policy["id"]
        assert "Freeze" in decision["explanation"]

    def test_preview_reports_an_ambiguous_configuration(
        self, client: TestClient, known_device: dict, catalog: dict[str, dict], engine: Engine
    ) -> None:
        with session_scope(engine) as session:
            for target in (OLDER, NEWER):
                session.add(
                    UpdatePolicy(
                        name=f"pin {target}",
                        scope=PolicyScope.GLOBAL,
                        policy_type=PolicyType.PIN,
                        target_version=target,
                        priority=0,
                        created_by="tester",
                    )
                )
            session.commit()

        decision = client.get(f"/api/v1/admin/devices/{known_device['id']}/update-decision").json()

        assert decision["reason"] == "POLICY_BLOCKED"
        assert decision["policy_id"] is None
        assert "ambiguous" in decision["explanation"]

    def test_preview_with_no_published_release(self, client: TestClient, device: dict) -> None:
        decision = client.get(f"/api/v1/admin/devices/{device['id']}/update-decision").json()

        assert decision["reason"] == "NO_ELIGIBLE_RELEASE"
        assert "no published release" in decision["explanation"]

    def test_preview_of_an_unknown_device(self, client: TestClient) -> None:
        assert client.get("/api/v1/admin/devices/999/update-decision").status_code == 404


class TestPreviewMatchesTheDevice:
    """The acceptance criterion: one decision, two surfaces."""

    def test_a_pin_offers_what_the_preview_promised(
        self, client: TestClient, known_device: dict, catalog: dict[str, dict]
    ) -> None:
        pin(client, OLDER)

        preview = client.get(f"/api/v1/admin/devices/{known_device['id']}/update-decision").json()
        stored = client.get(f"/api/v1/admin/firmware/releases/{catalog[OLDER]['id']}/manifest")
        served = client.get(manifest_path(CODE, PLATFORM), headers=headers(known_device))

        assert preview["target_version"] == OLDER
        assert served.status_code == 200
        assert served.content == stored.content
        assert served.json()["version"] == preview["target_version"]

    def test_a_range_offers_what_the_preview_promised(
        self, client: TestClient, known_device: dict, catalog: dict[str, dict]
    ) -> None:
        create_policy(client, policy_type="range", min_version=KNOWN, max_version=OLDER)

        preview = client.get(f"/api/v1/admin/devices/{known_device['id']}/update-decision").json()
        served = client.get(manifest_path(CODE, PLATFORM), headers=headers(known_device))

        assert preview["reason"] == "VERSION_RANGE"
        assert served.json()["version"] == preview["target_version"] == OLDER

    def test_a_disable_policy_is_visible_on_both_surfaces(
        self, client: TestClient, known_device: dict, catalog: dict[str, dict]
    ) -> None:
        create_policy(client, policy_type="disable")

        preview = client.get(f"/api/v1/admin/devices/{known_device['id']}/update-decision").json()
        manifest = client.get(manifest_path(CODE, PLATFORM), headers=headers(known_device))
        binary = client.get(
            firmware_path(CODE, PLATFORM, NEWER, FILENAME), headers=headers(known_device)
        )

        assert preview["reason"] == "OTA_DISABLED"
        assert manifest.status_code == 404
        assert manifest.json()["reason"] == "OTA_DISABLED"
        # The manifest the device already holds must not be redeemable either.
        assert binary.status_code == 404
        assert binary.json()["reason"] == "POLICY_BLOCKED"

    def test_a_pin_gates_the_bytes_it_does_not_name(
        self, client: TestClient, known_device: dict, catalog: dict[str, dict]
    ) -> None:
        """Pinning the fleet forward withholds the other version's binary too."""
        pin(client, NEWER)

        served = client.get(
            firmware_path(CODE, PLATFORM, OLDER, FILENAME), headers=headers(known_device)
        )

        assert served.status_code == 404
        assert served.json()["reason"] == "POLICY_BLOCKED"

    def test_the_preview_never_changes_the_device(
        self, client: TestClient, known_device: dict, catalog: dict[str, dict]
    ) -> None:
        before = client.get(f"/api/v1/admin/devices/{known_device['id']}").json()

        client.get(f"/api/v1/admin/devices/{known_device['id']}/update-decision")

        after = client.get(f"/api/v1/admin/devices/{known_device['id']}").json()
        assert after["last_manifest_check_at"] is None
        assert after["current_firmware_version"] == before["current_firmware_version"]


class TestDatabaseInvariants:
    def test_the_schema_rejects_a_contradictory_row(self, engine: Engine) -> None:
        """The service validates, and the database refuses anyway."""
        with session_scope(engine) as session, pytest.raises(IntegrityError) as exc:
            session.add(
                UpdatePolicy(
                    name="device scope without a device",
                    scope=PolicyScope.DEVICE,
                    policy_type=PolicyType.PIN,
                    target_version=OLDER,
                    created_by="tester",
                )
            )
            session.flush()

        assert "ck_update_policies_scope_target" in str(exc.value)

    def test_the_schema_rejects_a_pin_that_is_also_a_range(self, engine: Engine) -> None:
        with session_scope(engine) as session, pytest.raises(IntegrityError) as exc:
            session.add(
                UpdatePolicy(
                    name="pin with bounds",
                    scope=PolicyScope.GLOBAL,
                    policy_type=PolicyType.PIN,
                    target_version=OLDER,
                    min_version=NEWER,
                    created_by="tester",
                )
            )
            session.flush()

        assert "ck_update_policies_type_fields" in str(exc.value)


class TestPolicyAudit:
    def test_policy_changes_are_audited(
        self, client: TestClient, device: dict, engine: Engine
    ) -> None:
        policy = pin(client, OLDER, scope="device", device_id=device["id"])
        client.patch(f"/api/v1/admin/policies/{policy['id']}", json={"priority": 3})
        client.post(f"/api/v1/admin/policies/{policy['id']}/active", json={"is_active": False})
        client.delete(f"/api/v1/admin/policies/{policy['id']}")

        with session_scope(engine) as session:
            actions = [
                row.action
                for row in session.execute(
                    select(AuditEvent).where(AuditEvent.resource_type == "update_policy")
                ).scalars()
            ]

        assert set(actions) == {
            "policy_created",
            "policy_updated",
            "policy_deactivated",
            "policy_deleted",
        }
