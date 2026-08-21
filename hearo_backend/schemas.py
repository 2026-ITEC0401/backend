from __future__ import annotations

import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


LOGIN_ID_PATTERN = r"^[A-Za-z0-9._-]{4,30}$"
INVITE_CODE_PATTERN = r"^[A-Za-z0-9]{6}$"


def _validate_password_complexity(value: str) -> str:
    if not any(character.isascii() and character.isalpha() for character in value):
        raise ValueError("비밀번호에는 영문자가 필요합니다.")
    if not any(character.isdigit() for character in value):
        raise ValueError("비밀번호에는 숫자가 필요합니다.")
    return value


def _normalize_text(value: str, *, allow_empty: bool = False) -> str:
    normalized = unicodedata.normalize("NFC", value.strip())
    if not allow_empty and not normalized:
        raise ValueError("공백만 입력할 수 없습니다.")
    if any(unicodedata.category(character) == "Cc" for character in normalized):
        raise ValueError("제어 문자를 사용할 수 없습니다.")
    return normalized


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmergencyAddressRequest(StrictModel):
    postal_code: str = Field(pattern=r"^[0-9]{5}$")
    road_address: str = Field(min_length=5, max_length=200)
    detail_address: str = Field(default="", max_length=200)
    address_provider: Literal["kakao_postcode", "juso_go_kr"]

    @field_validator("road_address", "detail_address")
    @classmethod
    def normalize_address_text(cls, value: str, info):
        return _normalize_text(value, allow_empty=info.field_name == "detail_address")


class SignupRequest(StrictModel):
    login_id: str = Field(pattern=LOGIN_ID_PATTERN)
    name: str = Field(min_length=1, max_length=60)
    phone_number: str = Field(min_length=5, max_length=30)
    password: str = Field(min_length=10, max_length=256)
    signup_type: Literal["new_household", "family_member"]
    household_name: str | None = Field(default=None, max_length=60)
    emergency_address: EmergencyAddressRequest | None = None
    terms_service_agreed: Literal[True]
    privacy_agreed: Literal[True]

    @field_validator("login_id")
    @classmethod
    def normalize_login_id(cls, value: str) -> str:
        return value.strip().casefold()

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return _normalize_text(value)

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        return _validate_password_complexity(value)

    @field_validator("household_name")
    @classmethod
    def normalize_household_name(cls, value: str | None) -> str | None:
        return _normalize_text(value) if value is not None else None

    @model_validator(mode="after")
    def validate_signup_type_fields(self):
        if self.signup_type == "new_household":
            if self.household_name is None:
                raise ValueError("신규 가구 등록에는 household_name이 필요합니다.")
            if self.emergency_address is None:
                raise ValueError("신규 가구 등록에는 emergency_address가 필요합니다.")
        elif self.household_name is not None or self.emergency_address is not None:
            raise ValueError("가족 참여 가입에는 가구 이름이나 주소를 입력하지 않습니다.")
        return self


class LoginRequest(StrictModel):
    login_id: str = Field(pattern=LOGIN_ID_PATTERN)
    password: str = Field(min_length=1, max_length=256)

    @field_validator("login_id")
    @classmethod
    def normalize_login_id(cls, value: str) -> str:
        return value.strip().casefold()


class RefreshRequest(StrictModel):
    refresh_token: str = Field(min_length=20, max_length=4096)


class PasswordChangeRequest(StrictModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=10, max_length=256)

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, value: str) -> str:
        return _validate_password_complexity(value)


class InviteCodeRequest(StrictModel):
    invite_code: str = Field(pattern=INVITE_CODE_PATTERN)

    @field_validator("invite_code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        return value.strip().upper()


class DisplayNameRequest(StrictModel):
    display_name: str = Field(min_length=1, max_length=60)

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str) -> str:
        return _normalize_text(value)


class ConnectionRequest(StrictModel):
    enabled: bool


class DeviceSettingsRequest(StrictModel):
    led_alert_enabled: bool


class HeartbeatRequest(StrictModel):
    mqtt_connected: bool
    config_version: int = Field(ge=0)
    firmware_version: str | None = Field(default=None, max_length=80)
    audio_streaming: bool | None = None
    microphone_ok: bool | None = None
    audio_packets_sent: int | None = Field(default=None, ge=0)
    audio_packets_dropped: int | None = Field(default=None, ge=0)
    audio_clipped_samples: int | None = Field(default=None, ge=0)


class ContactRequest(StrictModel):
    name: str = Field(min_length=1, max_length=60)
    relationship: str = Field(min_length=1, max_length=30)
    phone_number: str = Field(min_length=5, max_length=30)

    @field_validator("name", "relationship")
    @classmethod
    def normalize_contact_text(cls, value: str) -> str:
        return _normalize_text(value)


class InternalDeviceStateRequest(StrictModel):
    household_id: str = Field(min_length=1, max_length=80)
    device_id: str = Field(min_length=1, max_length=50)
    mqtt_connected: bool
    config_version: int = Field(ge=0)
    firmware_version: str | None = Field(default=None, max_length=80)
    seen_at: str | None = Field(default=None, max_length=64)
    network_online: bool = True


class InternalAlertRequest(StrictModel):
    household_id: str = Field(min_length=1, max_length=80)
    event_id: str = Field(min_length=1, max_length=100)
    timestamp: str = Field(min_length=1, max_length=64)
    source_device_id: str = Field(min_length=1, max_length=50)
    publisher_device_id: str | None = Field(default=None, min_length=1, max_length=50)
    capture_device_id: str | None = Field(default=None, min_length=1, max_length=50)
    location: str = Field(min_length=1, max_length=60)
    sound: Literal["비상벨소리", "도어락소리", "노크소리", "아기울음소리"]
    raw_label: str | None = Field(default=None, max_length=100)
    type: Literal["Urgent", "Visitor", "Noise"]
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    decision_source: str | None = Field(default=None, max_length=80)
    confidence_kind: str | None = Field(default=None, max_length=80)
    yamnet_family: str | None = Field(default=None, max_length=80)
    yamnet_score: float | None = Field(default=None, ge=0.0, le=1.0)
    hearo_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    applied_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    policy_version: str | None = Field(default=None, max_length=100)


class WsAuthMessage(StrictModel):
    type: Literal["auth"]
    access_token: str = Field(min_length=20, max_length=4096)
