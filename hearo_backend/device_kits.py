from __future__ import annotations

import copy
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Callable

from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from .domain import FIXED_DEVICES, Device, Household, User, iso_utc, parse_timestamp
from .security import PASSWORD_HASHER


KIT_ID_PATTERN = re.compile(r"^HEARO-KIT-[A-Z0-9]{4,32}$")
CLAIM_CODE_PATTERN = re.compile(r"^[A-Z0-9]{4}-[A-Z0-9]{4}$")
HARDWARE_ID_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._-]{3,63}$")
CLAIM_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
# This is deliberately not a secret and hashes a value that cannot pass the
# public claim-code grammar. It equalizes the expensive verification step for
# unknown kit IDs without ever allowing the dummy credential to authenticate.
DUMMY_CLAIM_CODE_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$wRdwDSqkVOHgbZdtI/wB3g$"
    "bP9i7xiyAhC5rLg/pP3G+7gWPmpJn2HbDWdENVrDvQE"
)
FIXED_DEVICE_TYPES = {
    device_id: device_type for device_id, _, device_type in FIXED_DEVICES
}
FIXED_DEVICE_IDS = tuple(device_id for device_id, _, _ in FIXED_DEVICES)


class KitError(ValueError):
    """Stable, non-sensitive error raised by the public device-kit API."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class _KitInconsistent(RuntimeError):
    pass


class _KitCommitFailed(RuntimeError):
    def __init__(self, *, conditional: bool):
        super().__init__("device-kit transaction failed")
        self.conditional = conditional


@dataclass(frozen=True)
class _ClaimPlan:
    user: User
    household: Household
    logical_devices: dict[str, Device]
    inventory: dict[str, Any]


def _invalid_credentials() -> KitError:
    return KitError(
        400,
        "INVALID_KIT_CREDENTIALS",
        "키트 ID 또는 등록 코드를 확인해 주세요.",
    )


def _configuration_invalid() -> KitError:
    return KitError(
        409,
        "KIT_CONFIGURATION_INVALID",
        "키트 구성을 확인할 수 없습니다. 설치 담당자에게 문의해 주세요.",
    )


def _service_unavailable() -> KitError:
    return KitError(
        503,
        "KIT_SERVICE_UNAVAILABLE",
        "키트 등록 상태를 확인할 수 없습니다. 잠시 후 다시 시도해 주세요.",
    )


def _normalize_ascii_identifier(
    value: str,
    *,
    maximum_length: int,
    pattern: re.Pattern[str],
    message: str,
) -> str:
    if not isinstance(value, str) or len(value) > maximum_length:
        raise ValueError(message)
    stripped = value.strip()
    if not stripped or not stripped.isascii():
        raise ValueError(message)
    normalized = stripped.upper()
    if not pattern.fullmatch(normalized):
        raise ValueError(message)
    return normalized


def normalize_kit_id(value: str) -> str:
    return _normalize_ascii_identifier(
        value,
        maximum_length=64,
        pattern=KIT_ID_PATTERN,
        message="키트 ID 형식이 올바르지 않습니다.",
    )


def normalize_claim_code(value: str) -> str:
    return _normalize_ascii_identifier(
        value,
        maximum_length=32,
        pattern=CLAIM_CODE_PATTERN,
        message="등록 코드 형식이 올바르지 않습니다.",
    )


def normalize_hardware_id(value: str) -> str:
    return _normalize_ascii_identifier(
        value,
        maximum_length=64,
        pattern=HARDWARE_ID_PATTERN,
        message="물리 기기 ID 형식이 올바르지 않습니다.",
    )


def hash_claim_code(value: str) -> str:
    return PASSWORD_HASHER.hash(normalize_claim_code(value))


def verify_claim_code(value: str, encoded_hash: str) -> bool:
    try:
        normalized = normalize_claim_code(value)
        return bool(PASSWORD_HASHER.verify(encoded_hash, normalized))
    except (ValueError, TypeError, VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def new_claim_code() -> str:
    raw = "".join(secrets.choice(CLAIM_CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return value


def _aws_error_response(exc: Exception) -> dict[str, Any]:
    response = getattr(exc, "response", None)
    return response if isinstance(response, dict) else {}


def _transaction_conflict(exc: Exception, *, include_write_conflict: bool) -> bool:
    """Return true only for state races, never for capacity/service failures."""

    response = _aws_error_response(exc)
    error = response.get("Error")
    code = error.get("Code") if isinstance(error, dict) else None
    accepted = {"ConditionalCheckFailed"}
    direct = {"ConditionalCheckFailedException"}
    if include_write_conflict:
        accepted.add("TransactionConflict")
        direct.add("TransactionConflictException")
    if code in direct:
        return True
    if code != "TransactionCanceledException":
        return False
    reasons = response.get("CancellationReasons")
    if not isinstance(reasons, list):
        return False
    meaningful: list[str] = []
    for reason in reasons:
        if not isinstance(reason, dict):
            return False
        reason_code = reason.get("Code")
        if reason_code not in {None, "None"}:
            if not isinstance(reason_code, str):
                return False
            meaningful.append(reason_code)
    return bool(meaningful) and all(reason in accepted for reason in meaningful)


def _transaction_succeeded(response: Any) -> bool:
    if not isinstance(response, dict):
        return False
    metadata = response.get("ResponseMetadata")
    return isinstance(metadata, dict) and metadata.get("HTTPStatusCode") == 200


def _normalize_created_at(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("created_at must be an ISO 8601 timestamp")
    try:
        return iso_utc(parse_timestamp(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("created_at must be an ISO 8601 timestamp") from exc


def _validate_claim_code_hash(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("$argon2id$"):
        raise ValueError("claim_code_hash must be an Argon2id encoded hash")
    try:
        PASSWORD_HASHER.check_needs_rehash(value)
    except (InvalidHashError, VerificationError, TypeError) as exc:
        raise ValueError("claim_code_hash must be an Argon2id encoded hash") from exc
    return value


def _normalize_issue_devices(devices: list[dict[str, Any]]) -> list[dict[str, str]]:
    if not isinstance(devices, list) or len(devices) != len(FIXED_DEVICE_IDS):
        raise KitError(
            409,
            "KIT_CONFIGURATION_INVALID",
            "키트에는 정해진 기기 4대가 필요합니다.",
        )
    normalized: dict[str, dict[str, str]] = {}
    hardware_ids: set[str] = set()
    for raw in devices:
        if not isinstance(raw, dict) or set(raw) != {
            "device_id",
            "device_type",
            "hardware_id",
        }:
            raise _configuration_invalid()
        device_id = raw.get("device_id")
        device_type = raw.get("device_type")
        if (
            not isinstance(device_id, str)
            or device_id not in FIXED_DEVICE_TYPES
            or device_type != FIXED_DEVICE_TYPES[device_id]
            or device_id in normalized
        ):
            raise _configuration_invalid()
        try:
            hardware_id = normalize_hardware_id(raw.get("hardware_id"))
        except ValueError as exc:
            raise _configuration_invalid() from exc
        if hardware_id in hardware_ids:
            raise _configuration_invalid()
        hardware_ids.add(hardware_id)
        normalized[device_id] = {
            "device_id": device_id,
            "device_type": device_type,
            "hardware_id": hardware_id,
        }
    if set(normalized) != set(FIXED_DEVICE_IDS):
        raise _configuration_invalid()
    return [normalized[device_id] for device_id in FIXED_DEVICE_IDS]


def _ui_status(device: Device, settings: Any, now: datetime) -> str:
    try:
        last_seen = parse_timestamp(device.last_seen_at) if device.last_seen_at else None
        online = bool(
            device.reported_network_online
            and last_seen
            and (now - last_seen).total_seconds() <= settings.device_offline_seconds
        )
        if not online:
            return "offline"
        matches = (
            device.desired_mqtt_connected == device.reported_mqtt_connected
            and device.reported_config_version >= device.config_version
        )
        if matches:
            return "connected" if device.desired_mqtt_connected else "disabled_by_owner"
        age = (now - parse_timestamp(device.desired_updated_at)).total_seconds()
        return "error" if age > settings.command_timeout_seconds else "pending"
    except (AttributeError, TypeError, ValueError):
        raise _KitInconsistent() from None


class _KitRepositoryMixinBase:
    def _kit_read_user(self, user_id: str) -> User | None:
        raise NotImplementedError

    def _kit_read_household(self, household_id: str) -> Household | None:
        raise NotImplementedError

    def _kit_read_logical_devices(self, household_id: str) -> dict[str, Device | None]:
        raise NotImplementedError

    def _kit_read_inventory(self, kit_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def _kit_commit_claim(self, plan: _ClaimPlan, claimed_at: str) -> None:
        raise NotImplementedError

    @staticmethod
    def _house_kit_status(household: Household) -> str:
        status = getattr(household, "device_kit_status", "legacy_registered")
        if status not in {"unregistered", "claimed", "legacy_registered"}:
            raise _KitInconsistent()
        return status

    @staticmethod
    def _registration_version(household: Household) -> int:
        value = getattr(household, "registration_version", 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise _KitInconsistent()
        return value

    def _authorized_context(
        self, supplied_user: User, *, owner_required: bool
    ) -> tuple[User, Household]:
        if not isinstance(supplied_user, User) or not supplied_user.user_id:
            raise KitError(401, "AUTHENTICATION_REQUIRED", "로그인이 필요합니다.")
        user = self._kit_read_user(supplied_user.user_id)
        if user is None or user.user_id != supplied_user.user_id:
            raise KitError(401, "REVOKED_ACCESS_TOKEN", "로그인 상태가 만료되었습니다.")
        if user.token_version != supplied_user.token_version:
            raise KitError(401, "REVOKED_ACCESS_TOKEN", "로그인 상태가 만료되었습니다.")
        # The route has already authorized the URL household against this
        # snapshot. Never silently follow a membership that changed after that
        # check, or a request for household A could read or claim household B.
        if user.household_id != supplied_user.household_id:
            raise KitError(
                409,
                "KIT_CLAIM_CONFLICT",
                "요청 중 가구 연결 상태가 변경되었습니다. 현재 상태를 다시 확인해 주세요.",
            )
        if user.household_link_status != "linked" or not user.household_id:
            raise KitError(409, "HOUSEHOLD_LINK_REQUIRED", "가구 연결이 필요합니다.")
        household = self._kit_read_household(user.household_id)
        if household is None or household.status != "active":
            raise KitError(409, "HOUSEHOLD_INACTIVE", "활성 가구를 찾을 수 없습니다.")
        if household.household_id != user.household_id:
            raise _KitInconsistent()
        self._registration_version(household)
        if owner_required and (
            user.role != "owner"
            or user.account_type != "household_owner"
            or household.owner_user_id != user.user_id
        ):
            raise KitError(403, "OWNER_REQUIRED", "가구 소유자만 요청할 수 있습니다.")
        if not owner_required:
            valid_identity = (
                user.role == "owner" and user.account_type == "household_owner"
            ) or (
                user.role == "member" and user.account_type == "family_member"
            )
            if not valid_identity:
                raise _KitInconsistent()
        return user, household

    def _logical_devices(self, household_id: str) -> dict[str, Device]:
        raw = self._kit_read_logical_devices(household_id)
        devices: dict[str, Device] = {}
        for device_id in FIXED_DEVICE_IDS:
            device = raw.get(device_id)
            if (
                device is None
                or device.household_id != household_id
                or device.device_id != device_id
                or device.device_type != FIXED_DEVICE_TYPES[device_id]
            ):
                raise _KitInconsistent()
            devices[device_id] = device
        return devices

    def _assert_context_unchanged(self, user: User, household: Household) -> None:
        current_user = self._kit_read_user(user.user_id)
        current_house = self._kit_read_household(household.household_id)
        if current_house is not None:
            self._registration_version(current_house)
        if (
            current_user is None
            or current_house is None
            or current_user.user_id != user.user_id
            or current_user.household_id != household.household_id
            or current_user.household_link_status != "linked"
            or current_user.role != user.role
            or current_user.account_type != user.account_type
            or current_user.token_version != user.token_version
            or current_house.household_id != household.household_id
            or current_house.status != household.status
            or current_house.owner_user_id != household.owner_user_id
            or getattr(current_house, "device_kit_status", "legacy_registered")
            != getattr(household, "device_kit_status", "legacy_registered")
            or getattr(current_house, "kit_id", None)
            != getattr(household, "kit_id", None)
            or getattr(current_house, "kit_claimed_at", None)
            != getattr(household, "kit_claimed_at", None)
            or self._registration_version(current_house)
            != self._registration_version(household)
        ):
            raise _KitInconsistent()

    @staticmethod
    def _credentials_match(bundle: dict[str, Any] | None, claim_code: str) -> bool:
        meta = bundle.get("meta") if isinstance(bundle, dict) else None
        candidate = meta.get("claim_code_hash") if isinstance(meta, dict) else None
        has_stored_hash = True
        try:
            encoded_hash = _validate_claim_code_hash(candidate)
        except (ValueError, TypeError):
            encoded_hash = DUMMY_CLAIM_CODE_HASH
            has_stored_hash = False
        matched = verify_claim_code(claim_code, encoded_hash)
        return has_stored_hash and matched

    @staticmethod
    def _validated_inventory(kit_id: str, bundle: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(bundle, dict):
            raise _configuration_invalid()
        meta = bundle.get("meta")
        devices = bundle.get("devices")
        hardware = bundle.get("hardware")
        if (
            not isinstance(meta, dict)
            or not isinstance(devices, dict)
            or not isinstance(hardware, dict)
            or bundle.get("unknown_items")
            or meta.get("kit_id") != kit_id
            or meta.get("status") not in {"unclaimed", "claimed"}
            or not isinstance(meta.get("configuration_version"), int)
            or isinstance(meta.get("configuration_version"), bool)
            or meta["configuration_version"] < 1
            or set(devices) != set(FIXED_DEVICE_IDS)
        ):
            raise _configuration_invalid()
        configuration_version = meta["configuration_version"]
        normalized_hardware: set[str] = set()
        for device_id in FIXED_DEVICE_IDS:
            row = devices.get(device_id)
            if not isinstance(row, dict):
                raise _configuration_invalid()
            hardware_id = row.get("hardware_id")
            try:
                normalized = normalize_hardware_id(hardware_id)
            except ValueError as exc:
                raise _configuration_invalid() from exc
            if (
                hardware_id != normalized
                or normalized in normalized_hardware
                or row.get("kit_id") != kit_id
                or row.get("device_id") != device_id
                or row.get("device_type") != FIXED_DEVICE_TYPES[device_id]
                or row.get("configuration_version") != configuration_version
            ):
                raise _configuration_invalid()
            normalized_hardware.add(normalized)
            alias = hardware.get(normalized)
            if (
                not isinstance(alias, dict)
                or alias.get("hardware_id") != normalized
                or alias.get("kit_id") != kit_id
                or alias.get("device_id") != device_id
                or alias.get("device_type") != FIXED_DEVICE_TYPES[device_id]
            ):
                raise _configuration_invalid()
        if set(hardware) != normalized_hardware:
            raise _configuration_invalid()
        claimed_household_id = meta.get("claimed_household_id")
        claimed_at = meta.get("claimed_at")
        if meta["status"] == "unclaimed":
            if claimed_household_id is not None or claimed_at is not None:
                raise _configuration_invalid()
            if any(
                alias.get("claimed_household_id") is not None
                or alias.get("claimed_at") is not None
                for alias in hardware.values()
            ):
                raise _configuration_invalid()
        else:
            if (
                not isinstance(claimed_household_id, str)
                or not claimed_household_id
                or not isinstance(claimed_at, str)
            ):
                raise _configuration_invalid()
            try:
                parse_timestamp(claimed_at)
            except (TypeError, ValueError):
                raise _configuration_invalid() from None
            if any(
                alias.get("claimed_household_id") != claimed_household_id
                or alias.get("claimed_at") != claimed_at
                for alias in hardware.values()
            ):
                raise _configuration_invalid()
        return bundle

    @staticmethod
    def _unregistered_graph(household: Household, devices: dict[str, Device]) -> None:
        if (
            getattr(household, "kit_id", None) is not None
            or getattr(household, "kit_claimed_at", None) is not None
            or any(
                getattr(device, "kit_id", None) is not None
                or getattr(device, "hardware_id", None) is not None
                for device in devices.values()
            )
        ):
            raise _KitInconsistent()

    @staticmethod
    def _claimed_graph(
        household: Household,
        devices: dict[str, Device],
        bundle: dict[str, Any],
    ) -> None:
        kit_id = getattr(household, "kit_id", None)
        claimed_at = getattr(household, "kit_claimed_at", None)
        meta = bundle["meta"]
        if (
            not isinstance(kit_id, str)
            or not isinstance(claimed_at, str)
            or meta.get("status") != "claimed"
            or meta.get("claimed_household_id") != household.household_id
            or meta.get("claimed_at") != claimed_at
        ):
            raise _KitInconsistent()
        for device_id, device in devices.items():
            inventory_device = bundle["devices"][device_id]
            if (
                getattr(device, "kit_id", None) != kit_id
                or getattr(device, "hardware_id", None)
                != inventory_device.get("hardware_id")
            ):
                raise _KitInconsistent()

    @staticmethod
    def _state_response(
        user: User,
        household: Household,
        devices: dict[str, Device],
        settings: Any,
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        status = getattr(household, "device_kit_status", "legacy_registered")
        return {
            "household_id": household.household_id,
            "status": status,
            "kit_id": getattr(household, "kit_id", None),
            "claimed_at": getattr(household, "kit_claimed_at", None),
            "can_claim": bool(
                status == "unregistered"
                and user.role == "owner"
                and household.owner_user_id == user.user_id
            ),
            "devices": [
                {
                    "device_id": device_id,
                    "device_type": devices[device_id].device_type,
                    "location": devices[device_id].location,
                    "hardware_id": getattr(devices[device_id], "hardware_id", None),
                    "ui_status": _ui_status(devices[device_id], settings, now),
                    "last_seen_at": devices[device_id].last_seen_at,
                }
                for device_id in FIXED_DEVICE_IDS
            ],
        }

    def _read_state_once(self, supplied_user: User, settings: Any) -> dict[str, Any]:
        user, household = self._authorized_context(supplied_user, owner_required=False)
        devices = self._logical_devices(household.household_id)
        status = self._house_kit_status(household)
        if status == "claimed":
            kit_id = getattr(household, "kit_id", None)
            if not isinstance(kit_id, str):
                raise _KitInconsistent()
            bundle = self._kit_read_inventory(kit_id)
            try:
                validated = self._validated_inventory(kit_id, bundle)
            except KitError as exc:
                raise _KitInconsistent() from exc
            self._claimed_graph(household, devices, validated)
        elif status == "unregistered":
            self._unregistered_graph(household, devices)
        else:
            if (
                getattr(household, "kit_id", None) is not None
                or getattr(household, "kit_claimed_at", None) is not None
                or any(
                    getattr(device, "kit_id", None) is not None
                    or getattr(device, "hardware_id", None) is not None
                    for device in devices.values()
                )
            ):
                raise _KitInconsistent()
        # A second profile/house read catches unlink, withdrawal or a claim that
        # occurred while the multi-item graph was being assembled.
        self._assert_context_unchanged(user, household)
        return self._state_response(user, household, devices, settings)

    @staticmethod
    def _retry_consistency(operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        for _ in range(2):
            try:
                return operation()
            except _KitInconsistent:
                continue
        raise _service_unavailable()

    def get_device_kit_state(self, user: User, settings: Any) -> dict[str, Any]:
        return self._retry_consistency(lambda: self._read_state_once(user, settings))

    def _preview_once(
        self,
        supplied_user: User,
        kit_id: str,
        claim_code: str,
    ) -> dict[str, Any]:
        user, household = self._authorized_context(supplied_user, owner_required=True)
        status = self._house_kit_status(household)
        if status != "unregistered":
            raise KitError(
                409,
                "HOUSEHOLD_ALREADY_HAS_KIT",
                "이미 키트가 등록된 가구입니다.",
            )
        devices = self._logical_devices(household.household_id)
        self._unregistered_graph(household, devices)
        bundle = self._kit_read_inventory(kit_id)
        if not self._credentials_match(bundle, claim_code):
            raise _invalid_credentials()
        inventory = self._validated_inventory(kit_id, bundle)
        if inventory["meta"]["status"] == "claimed":
            if (
                inventory["meta"].get("claimed_household_id")
                == household.household_id
            ):
                raise _KitInconsistent()
            raise KitError(409, "KIT_ALREADY_CLAIMED", "이미 다른 가구에서 사용 중인 키트입니다.")
        self._assert_context_unchanged(user, household)
        return {
            "claimable": True,
            "kit_id": kit_id,
            "devices": [
                {
                    "device_id": device_id,
                    "device_type": devices[device_id].device_type,
                    "location": devices[device_id].location,
                    "hardware_id": inventory["devices"][device_id]["hardware_id"],
                }
                for device_id in FIXED_DEVICE_IDS
            ],
        }

    def preview_device_kit(
        self,
        user: User,
        settings: Any,
        kit_id: str,
        claim_code: str,
    ) -> dict[str, Any]:
        del settings  # Kept in the interface so all public kit functions align.
        normalized_kit = normalize_kit_id(kit_id)
        normalized_code = normalize_claim_code(claim_code)
        return self._retry_consistency(
            lambda: self._preview_once(user, normalized_kit, normalized_code)
        )

    def _claim_preflight_once(
        self,
        supplied_user: User,
        kit_id: str,
        claim_code: str,
        settings: Any,
    ) -> _ClaimPlan | dict[str, Any]:
        user, household = self._authorized_context(supplied_user, owner_required=True)
        devices = self._logical_devices(household.household_id)
        status = self._house_kit_status(household)
        if status == "legacy_registered":
            raise KitError(
                409,
                "HOUSEHOLD_ALREADY_HAS_KIT",
                "이미 키트가 등록된 가구입니다.",
            )
        if status == "claimed":
            if getattr(household, "kit_id", None) != kit_id:
                raise KitError(
                    409,
                    "HOUSEHOLD_ALREADY_HAS_KIT",
                    "이미 다른 키트가 등록된 가구입니다.",
                )
            bundle = self._kit_read_inventory(kit_id)
            if not self._credentials_match(bundle, claim_code):
                raise _invalid_credentials()
            try:
                inventory = self._validated_inventory(kit_id, bundle)
            except KitError as exc:
                raise _KitInconsistent() from exc
            self._claimed_graph(household, devices, inventory)
            state = self._read_state_once(supplied_user, settings)
            if state["status"] != "claimed" or state["kit_id"] != kit_id:
                raise _KitInconsistent()
            return state
        self._unregistered_graph(household, devices)
        bundle = self._kit_read_inventory(kit_id)
        if not self._credentials_match(bundle, claim_code):
            raise _invalid_credentials()
        inventory = self._validated_inventory(kit_id, bundle)
        if inventory["meta"]["status"] == "claimed":
            if inventory["meta"].get("claimed_household_id") == household.household_id:
                raise _KitInconsistent()
            raise KitError(409, "KIT_ALREADY_CLAIMED", "이미 다른 가구에서 사용 중인 키트입니다.")
        return _ClaimPlan(user, household, devices, inventory)

    def _claim_preflight(
        self,
        user: User,
        kit_id: str,
        claim_code: str,
        settings: Any,
    ) -> _ClaimPlan | dict[str, Any]:
        last_inconsistent = False
        for _ in range(2):
            try:
                return self._claim_preflight_once(user, kit_id, claim_code, settings)
            except _KitInconsistent:
                last_inconsistent = True
        if last_inconsistent:
            raise _service_unavailable()
        raise _service_unavailable()

    def _resolve_failed_claim(
        self,
        supplied_user: User,
        settings: Any,
        kit_id: str,
        claim_code: str,
        failure: _KitCommitFailed,
    ) -> dict[str, Any]:
        for _ in range(2):
            try:
                result = self._claim_preflight_once(
                    supplied_user, kit_id, claim_code, settings
                )
                if isinstance(result, dict):
                    return result
                if failure.conditional:
                    raise KitError(
                        409,
                        "KIT_CLAIM_CONFLICT",
                        "등록 중 상태가 변경되었습니다. 현재 상태를 다시 확인해 주세요.",
                    )
                raise _service_unavailable()
            except _KitInconsistent:
                continue
        raise _service_unavailable()

    def claim_device_kit(
        self,
        user: User,
        settings: Any,
        kit_id: str,
        claim_code: str,
    ) -> dict[str, Any]:
        normalized_kit = normalize_kit_id(kit_id)
        normalized_code = normalize_claim_code(claim_code)
        preflight = self._claim_preflight(
            user, normalized_kit, normalized_code, settings
        )
        if isinstance(preflight, dict):
            return preflight
        claimed_at = iso_utc()
        try:
            self._kit_commit_claim(preflight, claimed_at)
        except _KitCommitFailed as failure:
            return self._resolve_failed_claim(
                user,
                settings,
                normalized_kit,
                normalized_code,
                failure,
            )
        return self.get_device_kit_state(user, settings)


class MemoryKitRepositoryMixin(_KitRepositoryMixinBase):
    def _ensure_memory_kit_state(self) -> None:
        has_kits = hasattr(self, "device_kits")
        has_hardware = hasattr(self, "hardware_kits")
        if has_kits != has_hardware:
            raise _service_unavailable()
        if not has_kits:
            self.device_kits: dict[str, dict[str, Any]] = {}
            self.hardware_kits: dict[str, dict[str, Any]] = {}
        if not isinstance(self.device_kits, dict) or not isinstance(self.hardware_kits, dict):
            raise _service_unavailable()

    def issue_device_kit(
        self,
        kit_id: str,
        claim_code_hash: str,
        devices: list[dict[str, Any]],
        created_at: str,
    ) -> None:
        normalized_kit = normalize_kit_id(kit_id)
        encoded_hash = _validate_claim_code_hash(claim_code_hash)
        normalized_devices = _normalize_issue_devices(devices)
        normalized_created_at = _normalize_created_at(created_at)
        with self._lock:
            self._ensure_memory_kit_state()
            hardware_ids = {item["hardware_id"] for item in normalized_devices}
            if normalized_kit in self.device_kits or any(
                hardware_id in self.hardware_kits for hardware_id in hardware_ids
            ):
                raise KitError(
                    409,
                    "KIT_INVENTORY_CONFLICT",
                    "키트 또는 물리 기기 ID가 이미 등록되어 있습니다.",
                )
            inventory_devices = {
                item["device_id"]: {
                    **item,
                    "kit_id": normalized_kit,
                    "configuration_version": 1,
                    "created_at": normalized_created_at,
                }
                for item in normalized_devices
            }
            next_kit = {
                "kit_id": normalized_kit,
                "claim_code_hash": encoded_hash,
                "status": "unclaimed",
                "configuration_version": 1,
                "claimed_household_id": None,
                "claimed_at": None,
                "created_at": normalized_created_at,
                "devices": inventory_devices,
            }
            next_hardware = {
                item["hardware_id"]: {
                    **item,
                    "kit_id": normalized_kit,
                    "claimed_household_id": None,
                    "claimed_at": None,
                    "created_at": normalized_created_at,
                }
                for item in normalized_devices
            }
            kits_snapshot = copy.deepcopy(self.device_kits)
            hardware_snapshot = copy.deepcopy(self.hardware_kits)
            try:
                self.device_kits[normalized_kit] = next_kit
                self.hardware_kits.update(next_hardware)
            except Exception:
                self.device_kits.clear()
                self.device_kits.update(kits_snapshot)
                self.hardware_kits.clear()
                self.hardware_kits.update(hardware_snapshot)
                raise _service_unavailable() from None

    def _kit_read_user(self, user_id: str) -> User | None:
        return self.users.get(user_id)

    def _kit_read_household(self, household_id: str) -> Household | None:
        return self.households.get(household_id)

    def _kit_read_logical_devices(self, household_id: str) -> dict[str, Device | None]:
        return {
            device_id: self.devices.get((household_id, device_id))
            for device_id in FIXED_DEVICE_IDS
        }

    def _kit_read_inventory(self, kit_id: str) -> dict[str, Any] | None:
        device_kits = getattr(self, "device_kits", None)
        hardware_kits = getattr(self, "hardware_kits", None)
        if device_kits is None and hardware_kits is None:
            return None
        if not isinstance(device_kits, dict) or not isinstance(hardware_kits, dict):
            raise _service_unavailable()
        stored = device_kits.get(kit_id)
        if stored is None:
            return None
        value = copy.deepcopy(stored)
        inventory_devices = value.pop("devices", None)
        hardware: dict[str, Any] = {}
        if isinstance(inventory_devices, dict):
            for row in inventory_devices.values():
                if isinstance(row, dict) and isinstance(row.get("hardware_id"), str):
                    alias = hardware_kits.get(row["hardware_id"])
                    if alias is not None:
                        hardware[row["hardware_id"]] = copy.deepcopy(alias)
        return {
            "meta": value,
            "devices": inventory_devices,
            "hardware": hardware,
            "unknown_items": [],
        }

    def _kit_commit_claim(self, plan: _ClaimPlan, claimed_at: str) -> None:
        household_id = plan.household.household_id
        kit_id = plan.inventory["meta"]["kit_id"]
        house = self.households.get(household_id)
        user = self.users.get(plan.user.user_id)
        if (
            house is not plan.household
            or user is not plan.user
            or user.token_version != plan.user.token_version
            or user.household_id != household_id
            or user.household_link_status != "linked"
            or user.role != "owner"
            or user.account_type != "household_owner"
            or house.status != "active"
            or house.owner_user_id != user.user_id
            or getattr(house, "device_kit_status", "legacy_registered") != "unregistered"
            or self._registration_version(house)
            != self._registration_version(plan.household)
        ):
            raise _KitCommitFailed(conditional=True)
        current_bundle = self._kit_read_inventory(kit_id)
        if current_bundle != plan.inventory:
            raise _KitCommitFailed(conditional=True)
        current_devices = {
            device_id: self.devices.get((household_id, device_id))
            for device_id in FIXED_DEVICE_IDS
        }
        if any(current_devices[device_id] is not plan.logical_devices[device_id]
               for device_id in FIXED_DEVICE_IDS):
            raise _KitCommitFailed(conditional=True)

        house_snapshot = copy.deepcopy(house)
        device_snapshots = {
            device_id: copy.deepcopy(device) for device_id, device in current_devices.items()
        }
        kits_snapshot = copy.deepcopy(self.device_kits)
        hardware_snapshot = copy.deepcopy(self.hardware_kits)
        try:
            house.device_kit_status = "claimed"
            house.kit_id = kit_id
            house.kit_claimed_at = claimed_at
            house.registration_version += 1
            bundle = self.device_kits[kit_id]
            bundle["status"] = "claimed"
            bundle["claimed_household_id"] = household_id
            bundle["claimed_at"] = claimed_at
            for device_id in FIXED_DEVICE_IDS:
                inventory_device = plan.inventory["devices"][device_id]
                device = current_devices[device_id]
                device.kit_id = kit_id
                device.hardware_id = inventory_device["hardware_id"]
                alias = self.hardware_kits[inventory_device["hardware_id"]]
                alias["claimed_household_id"] = household_id
                alias["claimed_at"] = claimed_at
        except Exception:
            for item_field, value in vars(house_snapshot).items():
                setattr(house, item_field, value)
            for device_id, snapshot in device_snapshots.items():
                device = current_devices[device_id]
                for item_field, value in vars(snapshot).items():
                    setattr(device, item_field, value)
            self.device_kits.clear()
            self.device_kits.update(kits_snapshot)
            self.hardware_kits.clear()
            self.hardware_kits.update(hardware_snapshot)
            raise _KitCommitFailed(conditional=False) from None

    def get_device_kit_state(self, user: User, settings: Any) -> dict[str, Any]:
        with self._lock:
            return super().get_device_kit_state(user, settings)

    def preview_device_kit(
        self, user: User, settings: Any, kit_id: str, claim_code: str
    ) -> dict[str, Any]:
        with self._lock:
            return super().preview_device_kit(user, settings, kit_id, claim_code)

    def claim_device_kit(
        self, user: User, settings: Any, kit_id: str, claim_code: str
    ) -> dict[str, Any]:
        with self._lock:
            return super().claim_device_kit(user, settings, kit_id, claim_code)


class DynamoKitRepositoryMixin(_KitRepositoryMixinBase):
    def issue_device_kit(
        self,
        kit_id: str,
        claim_code_hash: str,
        devices: list[dict[str, Any]],
        created_at: str,
    ) -> None:
        normalized_kit = normalize_kit_id(kit_id)
        encoded_hash = _validate_claim_code_hash(claim_code_hash)
        normalized_devices = _normalize_issue_devices(devices)
        normalized_created_at = _normalize_created_at(created_at)
        operations: list[dict[str, Any]] = [{"Put": {
            "TableName": self.settings.core_table,
            "Item": self._ddb({
                "pk": f"KIT#{normalized_kit}",
                "sk": "META",
                "kit_id": normalized_kit,
                "claim_code_hash": encoded_hash,
                "status": "unclaimed",
                "configuration_version": 1,
                "claimed_household_id": None,
                "claimed_at": None,
                "created_at": normalized_created_at,
            }),
            "ConditionExpression": "attribute_not_exists(pk)",
        }}]
        for item in normalized_devices:
            operations.append({"Put": {
                "TableName": self.settings.core_table,
                "Item": self._ddb({
                    "pk": f"KIT#{normalized_kit}",
                    "sk": f"DEVICE#{item['device_id']}",
                    **item,
                    "kit_id": normalized_kit,
                    "configuration_version": 1,
                    "created_at": normalized_created_at,
                }),
                "ConditionExpression": "attribute_not_exists(pk)",
            }})
        for item in normalized_devices:
            operations.append({"Put": {
                "TableName": self.settings.core_table,
                "Item": self._ddb({
                    "pk": f"HARDWARE#{item['hardware_id']}",
                    "sk": "KIT",
                    **item,
                    "kit_id": normalized_kit,
                    "claimed_household_id": None,
                    "claimed_at": None,
                    "created_at": normalized_created_at,
                }),
                "ConditionExpression": "attribute_not_exists(pk)",
            }})
        try:
            response = self.client.transact_write_items(
                TransactItems=operations,
                ClientRequestToken=secrets.token_hex(16),
            )
            if not _transaction_succeeded(response):
                raise _service_unavailable()
        except Exception as exc:
            if _transaction_conflict(exc, include_write_conflict=False):
                raise KitError(
                    409,
                    "KIT_INVENTORY_CONFLICT",
                    "키트 또는 물리 기기 ID가 이미 등록되어 있습니다.",
                ) from None
            raise _service_unavailable() from None

    def _kit_read_user(self, user_id: str) -> User | None:
        item = self.core.get_item(
            Key={"pk": f"USER#{user_id}", "sk": "PROFILE"},
            ConsistentRead=True,
        ).get("Item")
        return self._user(item)

    def _kit_read_household(self, household_id: str) -> Household | None:
        item = self.core.get_item(
            Key={"pk": f"HOUSE#{household_id}", "sk": "META"},
            ConsistentRead=True,
        ).get("Item")
        return self._household(item)

    def _kit_read_logical_devices(self, household_id: str) -> dict[str, Device | None]:
        values: dict[str, Device | None] = {}
        for device_id in FIXED_DEVICE_IDS:
            item = self.core.get_item(
                Key={"pk": f"HOUSE#{household_id}", "sk": f"DEVICE#{device_id}"},
                ConsistentRead=True,
            ).get("Item")
            values[device_id] = self._device(item)
        return values

    def _kit_read_inventory(self, kit_id: str) -> dict[str, Any] | None:
        from boto3.dynamodb.conditions import Key

        arguments: dict[str, Any] = {
            "KeyConditionExpression": Key("pk").eq(f"KIT#{kit_id}"),
            "ConsistentRead": True,
        }
        meta: dict[str, Any] | None = None
        devices: dict[str, dict[str, Any]] = {}
        unknown_items: list[tuple[str, str]] = []
        while True:
            response = self.core.query(**arguments)
            for raw in response.get("Items", []):
                item = _plain(raw)
                pk, sk = item.get("pk"), item.get("sk")
                if pk != f"KIT#{kit_id}" or not isinstance(sk, str):
                    unknown_items.append((str(pk), str(sk)))
                elif sk == "META" and meta is None:
                    meta = {key: value for key, value in item.items() if key not in {"pk", "sk"}}
                elif sk.startswith("DEVICE#"):
                    device_id = sk.removeprefix("DEVICE#")
                    if device_id in devices:
                        unknown_items.append((pk, sk))
                    else:
                        devices[device_id] = {
                            key: value for key, value in item.items() if key not in {"pk", "sk"}
                        }
                else:
                    unknown_items.append((pk, sk))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            arguments["ExclusiveStartKey"] = last_key
        if meta is None:
            return None
        hardware: dict[str, dict[str, Any]] = {}
        for row in devices.values():
            hardware_id = row.get("hardware_id")
            if not isinstance(hardware_id, str):
                continue
            raw = self.core.get_item(
                Key={"pk": f"HARDWARE#{hardware_id}", "sk": "KIT"},
                ConsistentRead=True,
            ).get("Item")
            if raw is not None:
                item = _plain(raw)
                hardware[hardware_id] = {
                    key: value for key, value in item.items() if key not in {"pk", "sk"}
                }
        return {
            "meta": meta,
            "devices": devices,
            "hardware": hardware,
            "unknown_items": unknown_items,
        }

    def _kit_commit_claim(self, plan: _ClaimPlan, claimed_at: str) -> None:
        household_id = plan.household.household_id
        kit_id = plan.inventory["meta"]["kit_id"]
        configuration_version = plan.inventory["meta"]["configuration_version"]
        registration_version = self._registration_version(plan.household)
        operations: list[dict[str, Any]] = [
            {"Update": {
                "TableName": self.settings.core_table,
                "Key": self._ddb({"pk": f"KIT#{kit_id}", "sk": "META"}),
                "UpdateExpression": (
                    "SET #kit_status=:claimed, claimed_household_id=:house, claimed_at=:at"
                ),
                "ConditionExpression": (
                    "attribute_exists(pk) AND #kit_status=:unclaimed AND "
                    "claim_code_hash=:claim_hash AND configuration_version=:configuration AND "
                    "(attribute_not_exists(claimed_household_id) OR claimed_household_id=:none) AND "
                    "(attribute_not_exists(claimed_at) OR claimed_at=:none)"
                ),
                "ExpressionAttributeNames": {"#kit_status": "status"},
                "ExpressionAttributeValues": self._ddb({
                    ":claimed": "claimed",
                    ":unclaimed": "unclaimed",
                    ":house": household_id,
                    ":at": claimed_at,
                    ":claim_hash": plan.inventory["meta"]["claim_code_hash"],
                    ":configuration": configuration_version,
                    ":none": None,
                }),
            }},
            {"Update": {
                "TableName": self.settings.core_table,
                "Key": self._ddb({"pk": f"HOUSE#{household_id}", "sk": "META"}),
                "UpdateExpression": (
                    "SET device_kit_status=:claimed, kit_id=:kit, kit_claimed_at=:at, "
                    "registration_version=:next_registration"
                ),
                "ConditionExpression": (
                    "attribute_exists(pk) AND #house_status=:active AND owner_user_id=:owner AND "
                    "device_kit_status=:unregistered AND "
                    "(attribute_not_exists(kit_id) OR kit_id=:none) AND "
                    "(attribute_not_exists(kit_claimed_at) OR kit_claimed_at=:none) AND "
                    + (
                        "(attribute_not_exists(registration_version) OR "
                        "registration_version=:registration)"
                        if registration_version == 0
                        else "registration_version=:registration"
                    )
                ),
                "ExpressionAttributeNames": {"#house_status": "status"},
                "ExpressionAttributeValues": self._ddb({
                    ":claimed": "claimed",
                    ":unregistered": "unregistered",
                    ":kit": kit_id,
                    ":at": claimed_at,
                    ":active": "active",
                    ":owner": plan.user.user_id,
                    ":registration": registration_version,
                    ":next_registration": registration_version + 1,
                    ":none": None,
                }),
            }},
        ]
        for device_id in FIXED_DEVICE_IDS:
            device = plan.logical_devices[device_id]
            hardware_id = plan.inventory["devices"][device_id]["hardware_id"]
            operations.append({"Update": {
                "TableName": self.settings.core_table,
                "Key": self._ddb({
                    "pk": f"HOUSE#{household_id}", "sk": f"DEVICE#{device_id}"
                }),
                "UpdateExpression": "SET kit_id=:kit, hardware_id=:hardware",
                "ConditionExpression": (
                    "attribute_exists(pk) AND household_id=:house AND device_id=:device AND "
                    "device_type=:device_type AND "
                    "(attribute_not_exists(kit_id) OR kit_id=:none) AND "
                    "(attribute_not_exists(hardware_id) OR hardware_id=:none)"
                ),
                "ExpressionAttributeValues": self._ddb({
                    ":kit": kit_id,
                    ":hardware": hardware_id,
                    ":house": household_id,
                    ":device": device_id,
                    ":device_type": device.device_type,
                    ":none": None,
                }),
            }})
        for device_id in FIXED_DEVICE_IDS:
            hardware_id = plan.inventory["devices"][device_id]["hardware_id"]
            operations.append({"Update": {
                "TableName": self.settings.core_table,
                "Key": self._ddb({"pk": f"HARDWARE#{hardware_id}", "sk": "KIT"}),
                "UpdateExpression": "SET claimed_household_id=:house, claimed_at=:at",
                "ConditionExpression": (
                    "attribute_exists(pk) AND hardware_id=:hardware AND kit_id=:kit AND "
                    "device_id=:device AND device_type=:device_type AND "
                    "(attribute_not_exists(claimed_household_id) OR claimed_household_id=:none) AND "
                    "(attribute_not_exists(claimed_at) OR claimed_at=:none)"
                ),
                "ExpressionAttributeValues": self._ddb({
                    ":house": household_id,
                    ":at": claimed_at,
                    ":hardware": hardware_id,
                    ":kit": kit_id,
                    ":device": device_id,
                    ":device_type": FIXED_DEVICE_TYPES[device_id],
                    ":none": None,
                }),
            }})
        token_version = plan.user.token_version
        token_condition = "token_version=:token"
        if token_version == 0:
            token_condition = "(attribute_not_exists(token_version) OR token_version=:token)"
        operations.append({"ConditionCheck": {
            "TableName": self.settings.core_table,
            "Key": self._ddb({"pk": f"USER#{plan.user.user_id}", "sk": "PROFILE"}),
            "ConditionExpression": (
                "attribute_exists(pk) AND user_id=:user AND household_id=:house AND "
                "household_link_status=:linked AND #role=:owner_role AND "
                "account_type=:owner_type AND " + token_condition
            ),
            "ExpressionAttributeNames": {"#role": "role"},
            "ExpressionAttributeValues": self._ddb({
                ":user": plan.user.user_id,
                ":house": household_id,
                ":linked": "linked",
                ":owner_role": "owner",
                ":owner_type": "household_owner",
                ":token": token_version,
            }),
        }})
        try:
            response = self.client.transact_write_items(
                TransactItems=operations,
                ClientRequestToken=secrets.token_hex(16),
            )
            if not _transaction_succeeded(response):
                raise _KitCommitFailed(conditional=False)
        except Exception as exc:
            raise _KitCommitFailed(
                conditional=_transaction_conflict(
                    exc, include_write_conflict=True
                )
            ) from None


def _validation_error() -> KitError:
    return KitError(422, "VALIDATION_ERROR", "요청 값을 확인해 주세요.")


def get_kit_state(repository: Any, user: User, settings: Any) -> dict[str, Any]:
    try:
        return repository.get_device_kit_state(user, settings)
    except KitError:
        raise
    except Exception:
        raise _service_unavailable() from None


def preview_kit(
    repository: Any,
    user: User,
    settings: Any,
    kit_id: str,
    claim_code: str,
) -> dict[str, Any]:
    try:
        normalized_kit = normalize_kit_id(kit_id)
        normalized_code = normalize_claim_code(claim_code)
    except ValueError:
        raise _validation_error() from None
    try:
        return repository.preview_device_kit(
            user, settings, normalized_kit, normalized_code
        )
    except KitError:
        raise
    except Exception:
        raise _service_unavailable() from None


def claim_kit(
    repository: Any,
    user: User,
    settings: Any,
    kit_id: str,
    claim_code: str,
) -> dict[str, Any]:
    try:
        normalized_kit = normalize_kit_id(kit_id)
        normalized_code = normalize_claim_code(claim_code)
    except ValueError:
        raise _validation_error() from None
    try:
        return repository.claim_device_kit(
            user, settings, normalized_kit, normalized_code
        )
    except KitError:
        raise
    except Exception:
        raise _service_unavailable() from None
