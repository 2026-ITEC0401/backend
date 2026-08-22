from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import phonenumbers

from .config import Settings
from .domain import Device, EmergencyAddress, FIXED_DEVICES, Household, User, iso_utc, parse_timestamp
from .history import group_history, history_utc_range
from .security import (
    TokenManager,
    derive_invite_code,
    hash_password,
    hash_secret,
    normalize_login_id,
    random_secret,
    verify_password,
)
from .store import ConflictError, InvalidInviteError, NotFoundError


def expiration_iso(delta: timedelta) -> str:
    return iso_utc(datetime.now(UTC) + delta)


def issue_token_pair(repository, tokens: TokenManager, user: User) -> dict[str, Any]:
    access = tokens.access(user.user_id, user.token_version)
    refresh_token = tokens.refresh(user.user_id, user.token_version)
    refresh_payload = tokens.decode(refresh_token, "refresh")
    expires_at = datetime.fromtimestamp(refresh_payload["exp"], tz=UTC)
    repository.save_refresh_token(
        hash_secret(refresh_token), user.user_id, iso_utc(expires_at)
    )
    return {
        "access_token": access,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": tokens.settings.access_token_minutes * 60,
    }


def _new_invite(settings: Settings, household_id: str) -> tuple[str, str, str, str]:
    nonce = random_secret(12)
    raw_code = derive_invite_code(settings.jwt_secret, household_id, nonce)
    code_hash = hash_secret(raw_code)
    expires_at = expiration_iso(timedelta(hours=settings.invite_hours))
    return raw_code, code_hash, nonce, expires_at


def signup(repository, tokens: TokenManager, settings: Settings, request) -> dict[str, Any]:
    login_id = normalize_login_id(request.login_id)
    phone_number = normalize_phone(request.phone_number)
    password_hash = hash_password(request.password)
    user_id = uuid.uuid4().hex
    now = iso_utc()
    raw_credentials: dict[str, str] = {}

    if request.signup_type == "new_household":
        household_id = f"home-{uuid.uuid4().hex[:12]}"
        _, invite_hash, invite_nonce, invite_expires_at = _new_invite(
            settings, household_id
        )
        user = User(
            user_id=user_id,
            login_id=login_id,
            name=request.name,
            phone_number=phone_number,
            password_hash=password_hash,
            account_type="household_owner",
            household_id=household_id,
            role="owner",
            household_link_status="linked",
            linked_at=now,
            terms_service_agreed=True,
            privacy_agreed=True,
            consented_at=now,
        )
        address = request.emergency_address
        if request.household_name is None:
            raise ValueError("신규 가구 등록 정보가 완전하지 않습니다.")
        household = Household(
            household_id=household_id,
            name=request.household_name,
            owner_user_id=user_id,
            emergency_address=(
                EmergencyAddress(
                    postal_code=address.postal_code,
                    road_address=address.road_address,
                    detail_address=address.detail_address,
                    address_provider=address.address_provider,
                    # 가입 폼의 기존 주소 입력은 외부 공급자 응답을 서버가
                    # 확인한 것이 아니므로 검증 완료로 간주하지 않습니다.
                    verified=False,
                )
                if address is not None
                else None
            ),
            invite_hash=invite_hash,
            invite_nonce=invite_nonce,
            invite_expires_at=invite_expires_at,
        )
        devices = []
        for device_id, location, device_type in FIXED_DEVICES:
            raw = random_secret()
            raw_credentials[device_id] = raw
            devices.append(
                Device(
                    household_id=household_id,
                    device_id=device_id,
                    location=location,
                    device_type=device_type,
                    credential_hash=hash_secret(raw),
                )
            )
        repository.create_owner(user, household, devices)
    else:
        user = User(
            user_id=user_id,
            login_id=login_id,
            name=request.name,
            phone_number=phone_number,
            password_hash=password_hash,
            account_type="family_member",
            terms_service_agreed=True,
            privacy_agreed=True,
            consented_at=now,
        )
        repository.create_unlinked_user(user)

    response = {
        "user": user.public(),
        "tokens": issue_token_pair(repository, tokens, user),
    }
    if raw_credentials:
        response["device_credentials"] = raw_credentials
    return response


def login(repository, tokens: TokenManager, login_id: str, password: str) -> dict[str, Any]:
    try:
        normalized = normalize_login_id(login_id)
    except ValueError:
        normalized = "invalid_login_id"
    user = repository.get_user_by_login_id(normalized)
    if not user or not verify_password(password, user.password_hash):
        raise NotFoundError(
            "아이디 또는 비밀번호가 올바르지 않습니다.", code="INVALID_CREDENTIALS"
        )
    return {"user": user.public(), "tokens": issue_token_pair(repository, tokens, user)}


def refresh(repository, tokens: TokenManager, refresh_token: str) -> dict[str, Any]:
    payload = tokens.decode(refresh_token, "refresh")
    user_id = repository.consume_refresh_token(
        hash_secret(refresh_token), datetime.now(UTC)
    )
    if user_id != payload["sub"]:
        raise NotFoundError("refresh token의 사용자가 일치하지 않습니다.")
    user = repository.get_user(user_id)
    if not user or user.token_version != payload.get("tv"):
        raise NotFoundError("refresh token이 폐기되었습니다.")
    return issue_token_pair(repository, tokens, user)


def change_password(repository, user: User, current_password: str, new_password: str) -> None:
    if not verify_password(current_password, user.password_hash):
        raise ConflictError(
            "현재 비밀번호가 올바르지 않습니다.",
            code="CURRENT_PASSWORD_MISMATCH",
            field_errors={"current_password": "현재 비밀번호를 다시 확인해 주세요."},
        )
    if verify_password(new_password, user.password_hash):
        raise ConflictError(
            "새 비밀번호는 현재 비밀번호와 달라야 합니다.",
            code="PASSWORD_REUSE_NOT_ALLOWED",
            field_errors={"new_password": "다른 비밀번호를 입력해 주세요."},
        )
    repository.update_user_password(user.user_id, hash_password(new_password))


def current_invite(repository, settings: Settings, household: Household) -> dict[str, str]:
    if not household.invite_nonce or not household.invite_expires_at or not household.invite_hash:
        raise NotFoundError("초대 코드가 등록되지 않았습니다.", code="INVITE_CODE_NOT_FOUND")
    raw_code = derive_invite_code(
        settings.jwt_secret, household.household_id, household.invite_nonce
    )
    if not secrets_match(hash_secret(raw_code), household.invite_hash):
        raise ConflictError("초대 코드 메타데이터가 일치하지 않습니다.", code="INVITE_CODE_STATE_INVALID")
    return {"invite_code": raw_code, "expires_at": household.invite_expires_at}


def secrets_match(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left, right)


def rotate_invite(repository, settings: Settings, household_id: str) -> dict[str, str]:
    household = repository.get_household(household_id)
    if not household or household.status != "active":
        raise NotFoundError("가구를 찾을 수 없습니다.")
    for _ in range(5):
        raw_code, invite_hash, nonce, expires_at = _new_invite(settings, household_id)
        if invite_hash != household.invite_hash:
            break
    else:
        raise ConflictError("새 초대 코드를 생성하지 못했습니다.", code="INVITE_CODE_GENERATION_FAILED")
    repository.rotate_invite(household_id, invite_hash, nonce, expires_at)
    return {"invite_code": raw_code, "expires_at": expires_at}


def preview_invite(repository, raw_code: str, now: datetime | None = None) -> dict[str, Any]:
    current = now or datetime.now(UTC)
    invite = repository.get_invite(hash_secret(raw_code.strip().upper()))
    if not invite:
        raise InvalidInviteError("초대 코드를 찾을 수 없습니다.", code="INVITE_CODE_NOT_FOUND")
    if parse_timestamp(invite["expires_at"]) <= current:
        raise InvalidInviteError("초대 코드가 만료되었습니다.", code="INVITE_CODE_EXPIRED")
    household = repository.get_household(invite["household_id"])
    if (
        not household
        or household.status != "active"
        or household.invite_hash != hash_secret(raw_code.strip().upper())
    ):
        raise InvalidInviteError("사용할 수 없는 초대 코드입니다.", code="INVITE_CODE_INACTIVE")
    return {
        "linkable": True,
        "household": {
            "name": household.name,
            "member_count": repository.member_count(household.household_id),
            "created_at": household.created_at,
        },
    }


def link_household(repository, user: User, raw_code: str) -> dict[str, Any]:
    linked = repository.link_member(
        user.user_id, hash_secret(raw_code.strip().upper()), datetime.now(UTC)
    )
    return {
        "household_link_status": linked.household_link_status,
        "household_id": linked.household_id,
        "role": linked.role,
        "linked_at": linked.linked_at,
    }


def device_status(device: Device, settings: Settings, now: datetime | None = None) -> dict[str, Any]:
    current = now or datetime.now(UTC)
    last_seen = parse_timestamp(device.last_seen_at) if device.last_seen_at else None
    online = bool(
        device.reported_network_online
        and last_seen
        and (current - last_seen).total_seconds() <= settings.device_offline_seconds
    )
    if not online:
        ui_status = "offline"
        network_status = "offline"
    else:
        network_status = "online"
        configuration_matches = device.reported_config_version >= device.config_version
        if (
            device.desired_mqtt_connected == device.reported_mqtt_connected
            and configuration_matches
        ):
            ui_status = "connected" if device.desired_mqtt_connected else "disabled_by_owner"
        else:
            desired_age = (current - parse_timestamp(device.desired_updated_at)).total_seconds()
            ui_status = "error" if desired_age > settings.command_timeout_seconds else "pending"
    return {
        **device.public_state(),
        "network_status": network_status,
        "ui_status": ui_status,
    }


def recent_history(repository, household_id: str, now: datetime | None = None) -> dict[str, Any]:
    _, _, start, end_exclusive = history_utc_range(now)
    alerts = repository.query_alerts(household_id, start, end_exclusive)
    return group_history(alerts, now)


def normalize_phone(phone: str) -> str:
    try:
        parsed = phonenumbers.parse(phone, "KR")
    except phonenumbers.NumberParseException as exc:
        raise ValueError("유효한 전화번호가 아닙니다.") from exc
    if not phonenumbers.is_valid_number(parsed):
        raise ValueError("유효한 전화번호가 아닙니다.")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
