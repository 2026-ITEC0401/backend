from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal


Role = Literal["owner", "member"]
AccountType = Literal["household_owner", "family_member"]
HouseholdLinkStatus = Literal["linked", "unlinked"]
HouseholdStatus = Literal["active", "inactive"]
DeviceUiStatus = Literal["connected", "disabled_by_owner", "pending", "offline", "error"]


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_utc(value: datetime | None = None) -> str:
    current = value or utc_now()
    return current.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@dataclass
class User:
    user_id: str
    login_id: str
    name: str
    phone_number: str
    password_hash: str
    account_type: AccountType
    household_id: str | None = None
    role: Role | None = None
    household_link_status: HouseholdLinkStatus = "unlinked"
    linked_at: str | None = None
    terms_service_agreed: bool = False
    privacy_agreed: bool = False
    consented_at: str | None = None
    # Unknown legacy versions stay null; a deployment must not invent consent.
    terms_version: str | None = None
    privacy_version: str | None = None
    token_version: int = 0
    # Internal fence for records that reference this user. It is deliberately
    # independent from token_version and is not part of the public profile.
    reference_version: int = 0
    created_at: str = field(default_factory=iso_utc)

    def public(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "login_id": self.login_id,
            "name": self.name,
            "phone_number": self.phone_number,
            "account_type": self.account_type,
            "household_id": self.household_id,
            "role": self.role,
            "household_link_status": self.household_link_status,
            "terms_version": self.terms_version,
            "privacy_version": self.privacy_version,
            "consented_at": self.consented_at,
            "created_at": self.created_at,
        }


@dataclass
class EmergencyAddress:
    postal_code: str
    road_address: str
    detail_address: str
    address_provider: Literal["kakao_postcode", "juso_go_kr", "manual"]
    verified: bool = False
    updated_at: str = field(default_factory=iso_utc)

    def public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Household:
    household_id: str
    name: str
    owner_user_id: str
    timezone: str = "Asia/Seoul"
    status: HouseholdStatus = "active"
    created_at: str = field(default_factory=iso_utc)
    inactive_at: str | None = None
    emergency_address: EmergencyAddress | None = None
    invite_hash: str | None = None
    invite_nonce: str | None = None
    invite_expires_at: str | None = None

    def public(self) -> dict[str, Any]:
        # 주소와 초대 코드 메타데이터는 각각의 전용 권한 API에서만 반환합니다.
        return {
            "household_id": self.household_id,
            "name": self.name,
            "created_at": self.created_at,
        }


@dataclass
class Device:
    household_id: str
    device_id: str
    location: str
    device_type: Literal["hub", "alert_node"]
    desired_mqtt_connected: bool = True
    reported_mqtt_connected: bool = False
    reported_network_online: bool = False
    last_seen_at: str | None = None
    desired_updated_at: str = field(default_factory=iso_utc)
    config_version: int = 1
    reported_config_version: int = 0
    firmware_version: str | None = None
    credential_hash: str | None = None
    led_alert_enabled: bool = True
    # 이전 저장 레코드를 읽기 위한 내부 기본값이며 사용자 API에는 노출하지 않습니다.
    sensitivity: Literal["low", "default", "high"] = "default"
    audio_streaming: bool = False
    microphone_ok: bool = False
    audio_packets_sent: int = 0
    audio_packets_dropped: int = 0
    audio_clipped_samples: int = 0

    def raw(self) -> dict[str, Any]:
        return asdict(self)

    def public_state(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "location": self.location,
            "device_type": self.device_type,
            "led_alert_control_supported": self.device_type == "alert_node",
            "desired_mqtt_connected": self.desired_mqtt_connected,
            "reported_mqtt_connected": self.reported_mqtt_connected,
            "last_seen_at": self.last_seen_at,
            "led_alert_enabled": self.led_alert_enabled,
            "audio_streaming": self.audio_streaming,
            "microphone_ok": self.microphone_ok,
            "config_version": self.config_version,
        }


@dataclass
class Alert:
    household_id: str
    event_id: str
    timestamp: str
    source_device_id: str
    location: str
    sound: str
    type: str
    confidence: float | None = None
    raw_label: str | None = None
    model_version: str | None = None
    publisher_device_id: str | None = None
    decision_source: str | None = None
    confidence_kind: str | None = None
    yamnet_family: str | None = None
    yamnet_score: float | None = None
    hearo_confidence: float | None = None
    applied_threshold: float | None = None
    policy_version: str | None = None
    # Internal TTL field; the public alarm/WebSocket contract is unchanged.
    expires_at_epoch: int | None = None

    @property
    def event_key(self) -> str:
        return f"{self.timestamp}#{self.event_id}"

    def public(self) -> dict[str, Any]:
        return {
            "id": self.event_id,
            "time": self.timestamp,
            "location": self.location,
            "source_device_id": self.source_device_id,
            "sound": self.sound,
            "raw_label": self.raw_label,
            "type": self.type,
            "confidence": self.confidence,
        }


FIXED_DEVICES = (
    ("rpi-001", "거실", "hub"),
    ("esp32_1", "안방", "alert_node"),
    ("esp32_2", "현관", "alert_node"),
    ("esp32_3", "화장실", "alert_node"),
)
