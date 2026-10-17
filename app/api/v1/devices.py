from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.api.v1.deps import (
    build_firmware_service,
    build_update_decision_service,
    get_manifest_verifier,
    get_session,
    get_settings,
    get_storage,
)
from app.api.v1.errors import raise_domain_error
from app.api.v1.schemas import (
    DeviceCreate,
    DeviceFirmwareVersionUpdate,
    DeviceIdentityUpdate,
    DeviceRead,
    DeviceStatusUpdate,
    DeviceTokenRead,
    DeviceTypeActiveUpdate,
    DeviceTypeCreate,
    DeviceTypeRead,
    DeviceTypeUpdate,
    OtaEnabledUpdate,
    Page,
    TokenIssued,
    UpdateAttemptRead,
    UpdateDecisionRead,
)
from app.api.v1.security import (
    DevicesChangeLifecycle,
    DevicesCreate,
    DevicesPurge,
    DevicesRead,
    DevicesUpdateIdentity,
    DeviceTokensManage,
    DeviceTypesCreate,
    DeviceTypesDelete,
    DeviceTypesUpdate,
)
from app.core.config import Settings
from app.db.models import Device, DeviceStatus, DeviceType
from app.domain.errors import DomainError
from app.domain.manifest import ManifestVerifier
from app.domain.update_path import firmware_url, firmware_url_template, manifest_url
from app.services.audit import AuditService
from app.services.device_tokens import DeviceTokenService
from app.services.devices import DeviceService
from app.services.policies import PolicyService
from app.services.update_decision import UpdateDecision, UpdateReason
from app.storage.base import ObjectStorage

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


def _type_read(device_type: DeviceType, settings: Settings, *, can_delete: bool) -> DeviceTypeRead:
    return DeviceTypeRead(
        id=device_type.id,
        code=device_type.code,
        name=device_type.name,
        platform=device_type.platform,
        hardware_revision=device_type.hardware_revision,
        description=device_type.description,
        is_active=device_type.is_active,
        created_at=device_type.created_at,
        manifest_url=manifest_url(
            str(settings.ota_manifest_base_url), device_type.code, device_type.platform
        ),
        firmware_url_template=firmware_url_template(str(settings.ota_firmware_base_url)),
        can_delete=can_delete,
    )


def device_read(
    device: Device,
    settings: Settings,
    *,
    can_update_identity: bool = False,
    can_purge: bool = False,
) -> DeviceRead:
    device_type = device.device_type
    return DeviceRead(
        id=device.id,
        device_type_id=device.device_type_id,
        device_type_name=device_type.name,
        device_type_code=device_type.code,
        device_type_platform=device_type.platform,
        serial_number=device.serial_number,
        raw_efuse_mac=device.raw_efuse_mac,
        network_mac=device.network_mac,
        status=device.status,
        ota_enabled=device.ota_enabled,
        current_firmware_version=device.current_firmware_version,
        known_firmware_source=device.known_firmware_source,
        known_firmware_observed_at=device.known_firmware_observed_at,
        last_download_served_version=device.last_download_served_version,
        last_download_served_at=device.last_download_served_at,
        last_update_started_at=device.last_update_started_at,
        last_update_completed_at=device.last_update_completed_at,
        registered_at=device.registered_at,
        last_seen_at=device.last_seen_at,
        last_manifest_check_at=device.last_manifest_check_at,
        expected_manifest_url=manifest_url(
            str(settings.ota_manifest_base_url), device_type.code, device_type.platform
        ),
        can_update_identity=can_update_identity,
        can_purge=can_purge,
    )


def _get_device_or_404(session: Session, device_id: int) -> Device:
    try:
        return DeviceService(session).get(device_id)
    except DomainError as exc:
        raise_domain_error(exc)


@router.post("/device-types", status_code=status.HTTP_201_CREATED)
def create_device_type(
    payload: DeviceTypeCreate,
    admin: DeviceTypesCreate,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> DeviceTypeRead:
    service = DeviceService(session)
    try:
        device_type = service.create_type(**payload.model_dump())
    except DomainError as exc:
        session.rollback()
        raise_domain_error(exc)
    AuditService(session).record(admin.email, "device_type_created", "device_type", device_type.id)
    session.commit()
    return _type_read(device_type, settings, can_delete=True)


@router.get("/device-types")
def list_device_types(
    admin: DevicesRead,
    search: str | None = None,
    is_active: bool | None = None,
    sort: Literal["name", "code", "platform", "created_at"] = "name",
    order: Literal["asc", "desc"] = "asc",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Page[DeviceTypeRead]:
    service = DeviceService(session)
    rows, total = service.list_types(
        search=search,
        is_active=is_active,
        sort=sort,
        order=order,
        page=page,
        page_size=page_size,
    )
    deletable = service.deletable_type_ids([row.id for row in rows])
    return Page(
        items=[_type_read(row, settings, can_delete=row.id in deletable) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/device-types/{type_id}")
def get_device_type(
    type_id: int,
    admin: DevicesRead,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> DeviceTypeRead:
    service = DeviceService(session)
    try:
        device_type = service.get_type(type_id)
    except DomainError as exc:
        raise_domain_error(exc)
    return _type_read(
        device_type,
        settings,
        can_delete=type_id in service.deletable_type_ids([type_id]),
    )


@router.patch("/device-types/{type_id}")
def update_device_type(
    type_id: int,
    payload: DeviceTypeUpdate,
    admin: DeviceTypesUpdate,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> DeviceTypeRead:
    changes = payload.model_dump(exclude_unset=True)
    try:
        device_type = DeviceService(session).update_type(type_id, changes)
    except DomainError as exc:
        session.rollback()
        raise_domain_error(exc)
    AuditService(session).record(
        admin.email, "device_type_updated", "device_type", type_id, {"changed": sorted(changes)}
    )
    session.commit()
    can_delete = type_id in DeviceService(session).deletable_type_ids([type_id])
    return _type_read(device_type, settings, can_delete=can_delete)


@router.post("/device-types/{type_id}/active")
def set_device_type_active(
    type_id: int,
    payload: DeviceTypeActiveUpdate,
    admin: DeviceTypesUpdate,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> DeviceTypeRead:
    try:
        device_type = DeviceService(session).set_type_active(type_id, payload.is_active)
    except DomainError as exc:
        session.rollback()
        raise_domain_error(exc)
    AuditService(session).record(
        admin.email,
        "device_type_activated" if payload.is_active else "device_type_deactivated",
        "device_type",
        type_id,
    )
    session.commit()
    can_delete = type_id in DeviceService(session).deletable_type_ids([type_id])
    return _type_read(device_type, settings, can_delete=can_delete)


@router.delete("/device-types/{type_id}")
def delete_device_type(
    type_id: int,
    admin: DeviceTypesDelete,
    session: Session = Depends(get_session),
) -> dict[str, str]:
    try:
        DeviceService(session).delete_type(type_id)
    except DomainError as exc:
        session.rollback()
        raise_domain_error(exc)
    AuditService(session).record(admin.email, "device_type_deleted", "device_type", type_id)
    session.commit()
    return {"status": "deleted"}


@router.post("/devices", status_code=status.HTTP_201_CREATED)
def register_device(
    payload: DeviceCreate,
    admin: DevicesCreate,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> DeviceRead:
    service = DeviceService(session)
    try:
        device = service.register(**payload.model_dump())
    except DomainError as exc:
        session.rollback()
        raise_domain_error(exc)
    AuditService(session).record(admin.email, "device_created", "device", device.id)
    session.commit()
    return device_read(device, settings, can_update_identity=True, can_purge=True)


@router.get("/devices")
def list_devices(
    admin: DevicesRead,
    status_filter: DeviceStatus | None = Query(default=None, alias="status"),
    device_type_id: int | None = None,
    ota_enabled: bool | None = None,
    search: str | None = None,
    sort: Literal["serial_number", "status", "registered_at", "last_seen_at"] = "serial_number",
    order: Literal["asc", "desc"] = "asc",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Page[DeviceRead]:
    service = DeviceService(session)
    items, total = service.list_devices(
        status=status_filter,
        device_type_id=device_type_id,
        ota_enabled=ota_enabled,
        search=search,
        page=page,
        page_size=page_size,
        sort=sort,
        order=order,
    )
    eligibility = service.device_action_eligibility(items)
    return Page(
        items=[
            device_read(
                row,
                settings,
                can_update_identity=eligibility[row.id][0],
                can_purge=eligibility[row.id][1],
            )
            for row in items
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/devices/{device_id}")
def get_device(
    device_id: int,
    admin: DevicesRead,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> DeviceRead:
    row = _get_device_or_404(session, device_id)
    can_update_identity, can_purge = DeviceService(session).device_action_eligibility([row])[row.id]
    return device_read(
        row,
        settings,
        can_update_identity=can_update_identity,
        can_purge=can_purge,
    )


@router.patch("/devices/{device_id}")
def update_device_identity(
    device_id: int,
    payload: DeviceIdentityUpdate,
    admin: DevicesUpdateIdentity,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> DeviceRead:
    changes = payload.model_dump(exclude_unset=True)
    try:
        device = DeviceService(session).update_identity(device_id, changes)
    except DomainError as exc:
        session.rollback()
        raise_domain_error(exc)
    AuditService(session).record(
        admin.email, "device_identity_updated", "device", device_id, {"changed": sorted(changes)}
    )
    session.commit()
    can_update_identity, can_purge = DeviceService(session).device_action_eligibility([device])[
        device.id
    ]
    return device_read(
        device,
        settings,
        can_update_identity=can_update_identity,
        can_purge=can_purge,
    )


@router.delete("/devices/{device_id}")
def purge_unused_device(
    device_id: int,
    admin: DevicesPurge,
    session: Session = Depends(get_session),
) -> dict[str, str]:
    """Delete a mistaken enrollment and its tokens when retained history is absent."""
    try:
        DeviceService(session).purge(device_id)
    except DomainError as exc:
        session.rollback()
        raise_domain_error(exc)
    AuditService(session).record(admin.email, "device_purged", "device", device_id)
    session.commit()
    return {"status": "deleted"}


@router.post("/devices/{device_id}/status")
def set_device_status(
    device_id: int,
    payload: DeviceStatusUpdate,
    admin: DevicesChangeLifecycle,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> DeviceRead:
    previous_status = _get_device_or_404(session, device_id).status
    try:
        device = DeviceService(session).set_status(device_id, payload.status)
    except DomainError as exc:
        session.rollback()
        raise_domain_error(exc)
    action = (
        "device_restored"
        if previous_status is DeviceStatus.RETIRED and payload.status is DeviceStatus.DISABLED
        else f"device_{payload.status.value}"
    )
    AuditService(session).record(
        admin.email,
        action,
        "device",
        device_id,
        {
            "before": {"status": previous_status.value},
            "after": {"status": device.status.value},
        },
    )
    session.commit()
    can_update_identity, can_purge = DeviceService(session).device_action_eligibility([device])[
        device.id
    ]
    return device_read(
        device,
        settings,
        can_update_identity=can_update_identity,
        can_purge=can_purge,
    )


@router.post("/devices/{device_id}/ota-enabled")
def set_ota_enabled(
    device_id: int,
    payload: OtaEnabledUpdate,
    admin: DevicesChangeLifecycle,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> DeviceRead:
    previous_ota_enabled = _get_device_or_404(session, device_id).ota_enabled
    try:
        device = DeviceService(session).set_ota_enabled(device_id, payload.ota_enabled)
    except DomainError as exc:
        session.rollback()
        raise_domain_error(exc)
    AuditService(session).record(
        admin.email,
        "device_ota_flag_changed",
        "device",
        device_id,
        {
            "before": {"ota_enabled": previous_ota_enabled},
            "after": {"ota_enabled": device.ota_enabled},
        },
    )
    session.commit()
    can_update_identity, can_purge = DeviceService(session).device_action_eligibility([device])[
        device.id
    ]
    return device_read(
        device,
        settings,
        can_update_identity=can_update_identity,
        can_purge=can_purge,
    )


@router.post("/devices/{device_id}/firmware-version")
def set_device_firmware_version(
    device_id: int,
    payload: DeviceFirmwareVersionUpdate,
    admin: DevicesChangeLifecycle,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> DeviceRead:
    """Assert the running firmware version, or clear it when unknown.

    This is what makes the "nothing newer than the known version" rule usable;
    because the device never reports its version, an operator's assertion is the
    only input, and it is labelled as such wherever it is displayed.
    """
    previous_version = _get_device_or_404(session, device_id).current_firmware_version
    try:
        device = DeviceService(session).set_known_firmware_version(device_id, payload.version)
    except DomainError as exc:
        session.rollback()
        raise_domain_error(exc)
    AuditService(session).record(
        admin.email,
        "device_firmware_version_asserted",
        "device",
        device_id,
        {
            "version": payload.version,
            "before": {"version": previous_version},
            "after": {"version": payload.version},
        },
    )
    session.commit()
    can_update_identity, can_purge = DeviceService(session).device_action_eligibility([device])[
        device.id
    ]
    return device_read(
        device,
        settings,
        can_update_identity=can_update_identity,
        can_purge=can_purge,
    )


def _explain(decision: UpdateDecision, policy_name: str | None) -> str:
    """A sentence a human can act on; the machine-readable reason sits beside it."""
    reason = decision.reason
    if reason is UpdateReason.LATEST_AVAILABLE:
        return "The newest published release of this device type is eligible."
    if reason is UpdateReason.PINNED_VERSION:
        return f"The policy “{policy_name}” pins this device to {decision.target_version}."
    if reason is UpdateReason.VERSION_RANGE:
        return (
            f"The policy “{policy_name}” allows {decision.target_version}, "
            "the newest release inside its range."
        )
    if reason is UpdateReason.NO_UPDATE:
        known = decision.known_version or "unknown"
        return (
            f"The known version ({known}) is not older than the target "
            f"({decision.target_version}), and a downgrade is impossible."
        )
    if reason is UpdateReason.DEVICE_TYPE_MISMATCH:
        return "The requested device type or platform is not this device's own."
    if reason is UpdateReason.DEVICE_INACTIVE:
        return "The device is not active, so no firmware is offered."
    if reason is UpdateReason.OTA_DISABLED:
        if decision.policy_id is not None:
            return f"OTA is disabled by the policy “{policy_name}”."
        return "OTA is disabled on the device itself."
    if reason is UpdateReason.POLICY_BLOCKED:
        if decision.policy_id is None:
            return (
                "Two active policies with the same scope and priority make the outcome "
                "ambiguous; nothing is offered until one is changed or disabled."
            )
        return f"The policy “{policy_name}” leaves no eligible release for this device."
    return "This device type has no published release."


@router.get("/devices/{device_id}/update-decision")
def get_update_decision(
    device_id: int,
    admin: DevicesRead,
    session: Session = Depends(get_session),
    storage: ObjectStorage = Depends(get_storage),
    settings: Settings = Depends(get_settings),
    verifier: ManifestVerifier | None = Depends(get_manifest_verifier),
) -> UpdateDecisionRead:
    """Explain the decision the device endpoint would make for this device now.

    Same service, same inputs, so the dashboard cannot disagree with what the
    device is actually told (`docs/07-update-policy.md` §16). The device's own
    device type is used for path identity on purpose: the preview answers
    questions about the device, not about a URL someone typed.
    """
    device = _get_device_or_404(session, device_id)
    device_type = device.device_type
    firmware_service = build_firmware_service(session, storage, settings, verifier)
    decision = build_update_decision_service(session, firmware_service).decide_for_device(
        device, requested_code=device_type.code, requested_platform=device_type.platform
    )
    policy_name = (
        None if decision.policy_id is None else PolicyService(session).get(decision.policy_id).name
    )
    release = decision.release
    return UpdateDecisionRead(
        device_id=decision.device_id,
        update_available=decision.update_available,
        reason=decision.reason,
        target_version=decision.target_version,
        release_id=decision.release_id,
        firmware_url=None
        if release is None or release.artifact is None
        else firmware_url(
            str(settings.ota_firmware_base_url),
            device_type.code,
            device_type.platform,
            release.version,
            release.artifact.filename,
        ),
        policy_id=decision.policy_id,
        policy_scope=decision.policy_scope,
        policy_name=policy_name,
        known_version=decision.known_version,
        known_version_source=decision.known_version_source,
        explanation=_explain(decision, policy_name),
    )


@router.get("/devices/{device_id}/update-attempts")
def list_device_update_attempts(
    device_id: int,
    admin: DevicesRead,
    limit: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_session),
) -> dict[str, object]:
    """Recent server-observed OTA events: what was offered and what was served."""
    try:
        rows = DeviceService(session).list_update_attempts(device_id, limit)
    except DomainError as exc:
        raise_domain_error(exc)
    return {
        "items": [UpdateAttemptRead.model_validate(row, from_attributes=True) for row in rows],
        "total": len(rows),
    }


@router.get("/devices/{device_id}/tokens")
def list_device_tokens(
    device_id: int,
    admin: DevicesRead,
    session: Session = Depends(get_session),
) -> list[DeviceTokenRead]:
    _get_device_or_404(session, device_id)
    rows = DeviceTokenService(session).list_for_device(device_id)
    return [DeviceTokenRead.model_validate(row, from_attributes=True) for row in rows]


@router.post("/devices/{device_id}/token/issue", status_code=status.HTTP_201_CREATED)
def issue_token(
    device_id: int, admin: DeviceTokensManage, session: Session = Depends(get_session)
) -> TokenIssued:
    _get_device_or_404(session, device_id)
    record, plaintext = DeviceTokenService(session).issue(device_id)
    AuditService(session).record(
        admin.email, "token_created", "device_token", record.id, {"device_id": device_id}
    )
    session.commit()
    return TokenIssued(token_id=record.id, device_id=device_id, token=plaintext)


@router.post("/devices/{device_id}/token/rotate", status_code=status.HTTP_201_CREATED)
def rotate_token(
    device_id: int,
    admin: DeviceTokensManage,
    current_token_id: int = Query(...),
    session: Session = Depends(get_session),
) -> TokenIssued:
    try:
        record, plaintext = DeviceTokenService(session).rotate(device_id, current_token_id)
    except DomainError as exc:
        session.rollback()
        raise_domain_error(exc)
    AuditService(session).record(
        admin.email, "token_rotated", "device_token", record.id, {"device_id": device_id}
    )
    session.commit()
    return TokenIssued(token_id=record.id, device_id=device_id, token=plaintext)


@router.post("/devices/{device_id}/token/revoke")
def revoke_token(
    device_id: int,
    admin: DeviceTokensManage,
    token_id: int = Query(...),
    session: Session = Depends(get_session),
) -> dict[str, str]:
    try:
        DeviceTokenService(session).revoke(token_id, device_id=device_id)
    except DomainError as exc:
        session.rollback()
        raise_domain_error(exc)
    AuditService(session).record(
        admin.email, "token_revoked", "device_token", token_id, {"device_id": device_id}
    )
    session.commit()
    return {"status": "revoked"}
