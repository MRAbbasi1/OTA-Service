from __future__ import annotations

import pytest

from app.db.models import DeviceStatus
from app.services.device_auth import AuthError, DeviceAuthService
from app.services.device_tokens import DeviceTokenService, TokenError, hash_token
from app.services.devices import DeviceError, DeviceService, normalize_mac

SERIAL = 10432
EFUSE_MAC = "7C:9E:BD:12:34:56"


@pytest.fixture
def device_type_id(db_session) -> int:
    return (
        DeviceService(db_session)
        .create_type(code="bcs-controller", name="Smart Controller", platform="esp32-s3")
        .id
    )


@pytest.fixture
def device_id(db_session, device_type_id) -> int:
    return DeviceService(db_session).register(device_type_id, SERIAL, EFUSE_MAC).id


@pytest.fixture
def token(db_session, device_id) -> str:
    _, plaintext = DeviceTokenService(db_session).issue(device_id)
    return plaintext


class TestMacNormalization:
    def test_uppercase_and_colons(self) -> None:
        assert normalize_mac("7c-9e-bd-12-34-56") == EFUSE_MAC

    def test_invalid_length_rejected(self) -> None:
        with pytest.raises(DeviceError):
            normalize_mac("7C:9E:BD")


class TestRegistration:
    def test_register_and_defaults(self, db_session, device_id) -> None:
        device = DeviceService(db_session).get(device_id)
        assert device.serial_number == SERIAL
        assert device.raw_efuse_mac == EFUSE_MAC
        assert device.status is DeviceStatus.PROVISIONED
        assert device.ota_enabled is True

    def test_duplicate_serial_rejected(self, db_session, device_id, device_type_id) -> None:
        with pytest.raises(DeviceError, match="serial_exists"):
            DeviceService(db_session).register(device_type_id, SERIAL, "AA:BB:CC:DD:EE:01")

    def test_duplicate_mac_rejected(self, db_session, device_id, device_type_id) -> None:
        with pytest.raises(DeviceError, match="mac_exists"):
            DeviceService(db_session).register(device_type_id, 99999, EFUSE_MAC)


class TestAuthentication:
    def test_valid_credentials_pass(self, db_session, device_id, token) -> None:
        device = DeviceAuthService(db_session).authenticate(str(SERIAL), EFUSE_MAC, token)
        assert device.id == device_id
        assert device.last_seen_at is not None

    def test_unknown_device_403(self, db_session) -> None:
        with pytest.raises(AuthError) as exc:
            DeviceAuthService(db_session).authenticate("1", EFUSE_MAC, "tok")
        assert exc.value.http_status == 403

    def test_mac_mismatch_403(self, db_session, device_id, token) -> None:
        with pytest.raises(AuthError) as exc:
            DeviceAuthService(db_session).authenticate(str(SERIAL), "AA:BB:CC:DD:EE:FF", token)
        assert exc.value.http_status == 403

    def test_invalid_token_401(self, db_session, device_id, token) -> None:
        with pytest.raises(AuthError) as exc:
            DeviceAuthService(db_session).authenticate(str(SERIAL), EFUSE_MAC, "wrong-token")
        assert exc.value.http_status == 401

    def test_missing_headers_401(self, db_session) -> None:
        with pytest.raises(AuthError) as exc:
            DeviceAuthService(db_session).authenticate(None, EFUSE_MAC, "tok")
        assert exc.value.http_status == 401

    def test_revoked_token_401(self, db_session, device_id, token) -> None:
        service = DeviceTokenService(db_session)
        record = service.find_active_by_hash(hash_token(token))
        assert record is not None
        service.revoke(record.id)
        with pytest.raises(AuthError) as exc:
            DeviceAuthService(db_session).authenticate(str(SERIAL), EFUSE_MAC, token)
        assert exc.value.http_status == 401

    def test_disabled_device_403(self, db_session, device_id, token) -> None:
        DeviceService(db_session).set_status(device_id, DeviceStatus.DISABLED)
        with pytest.raises(AuthError) as exc:
            DeviceAuthService(db_session).authenticate(str(SERIAL), EFUSE_MAC, token)
        assert exc.value.http_status == 403


class TestTokenLifecycle:
    def test_rotate_revokes_old(self, db_session, device_id, token) -> None:
        service = DeviceTokenService(db_session)
        old = service.find_active_by_hash(hash_token(token))
        assert old is not None
        _, new_plaintext = service.rotate(device_id, old.id)
        assert new_plaintext != token
        assert service.find_active_by_hash(hash_token(token)) is None
        assert service.find_active_by_hash(hash_token(new_plaintext)) is not None

    def test_rotate_wrong_device_rejected(self, db_session, device_id, token) -> None:
        service = DeviceTokenService(db_session)
        old = service.find_active_by_hash(hash_token(token))
        assert old is not None
        with pytest.raises(TokenError):
            service.rotate(device_id + 999, old.id)
