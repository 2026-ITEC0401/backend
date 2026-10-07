from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit


def _csv_env(name: str, default: str) -> list[str]:
    return [value.strip() for value in os.getenv(name, default).split(",") if value.strip()]


def _strict_bool_env(name: str, default: str = "false") -> bool:
    value = os.getenv(name, default).strip().lower()
    if value not in {"true", "false"}:
        raise RuntimeError(f"{name} must be true or false")
    return value == "true"


@dataclass(frozen=True)
class Settings:
    environment: str = field(default_factory=lambda: os.getenv("HEARO_ENV", "development"))
    region: str = field(default_factory=lambda: os.getenv("AWS_REGION", "ap-south-1"))
    core_table: str = field(
        default_factory=lambda: os.getenv("HEARO_CORE_TABLE", "hearo-core-v2-final")
    )
    alerts_table: str = field(
        default_factory=lambda: os.getenv("HEARO_ALERTS_TABLE", "hearo-alerts-v2-final")
    )
    store_backend: str = field(default_factory=lambda: os.getenv("HEARO_STORE", "memory"))
    jwt_secret: str = field(default_factory=lambda: os.getenv("HEARO_JWT_SECRET", "development-only-change-me"))
    jwt_issuer: str = "hearo-api"
    access_token_minutes: int = 15
    refresh_token_days: int = 30
    invite_hours: int = 24
    device_offline_seconds: int = 45
    command_timeout_seconds: int = 30
    cors_origins: list[str] = field(
        default_factory=lambda: _csv_env(
            "HEARO_CORS_ORIGINS", "http://localhost:5173,http://localhost:3000"
        )
    )
    mqtt_host: str = field(default_factory=lambda: os.getenv("HEARO_MQTT_HOST", "localhost"))
    mqtt_port: int = field(default_factory=lambda: int(os.getenv("HEARO_MQTT_PORT", "8883")))
    mqtt_username: str = field(default_factory=lambda: os.getenv("HEARO_MQTT_USERNAME", "hearo-api"))
    mqtt_password: str = field(default_factory=lambda: os.getenv("HEARO_MQTT_PASSWORD", ""))
    mqtt_ca_path: str = field(default_factory=lambda: os.getenv("HEARO_MQTT_CA_PATH", "/etc/ssl/certs/ca-certificates.crt"))
    mqtt_enabled: bool = field(default_factory=lambda: os.getenv("HEARO_MQTT_ENABLED", "false").lower() == "true")
    internal_token: str = field(default_factory=lambda: os.getenv("HEARO_INTERNAL_TOKEN", "development-internal-token"))
    juso_confirm_key: str = field(
        default_factory=lambda: os.getenv("HEARO_JUSO_CONFIRM_KEY", "")
    )
    juso_detail_confirm_key: str = field(
        default_factory=lambda: os.getenv("HEARO_JUSO_DETAIL_CONFIRM_KEY", "")
    )
    juso_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("HEARO_JUSO_TIMEOUT_SECONDS", "5"))
    )
    alarm_unread_baseline_at: str = field(
        default_factory=lambda: os.getenv("HEARO_ALARM_UNREAD_BASELINE_AT", "")
    )
    terms_version: str = field(default_factory=lambda: os.getenv("HEARO_TERMS_VERSION", ""))
    privacy_version: str = field(default_factory=lambda: os.getenv("HEARO_PRIVACY_VERSION", ""))
    legal_effective_at: str = field(default_factory=lambda: os.getenv("HEARO_LEGAL_EFFECTIVE_AT", ""))
    terms_url: str = field(default_factory=lambda: os.getenv("HEARO_TERMS_URL", ""))
    privacy_url: str = field(default_factory=lambda: os.getenv("HEARO_PRIVACY_URL", ""))
    legal_consent_required: bool = field(
        default_factory=lambda: _strict_bool_env("HEARO_LEGAL_CONSENT_REQUIRED")
    )

    def validate_legal_policy(self) -> None:
        values = (
            self.terms_version, self.privacy_version, self.legal_effective_at,
            self.terms_url, self.privacy_url,
        )
        if not any(values) and not self.legal_consent_required:
            return  # Safe legacy mode until approved documents are published.
        if not all(values):
            raise RuntimeError("Legal policy requires both versions, effective time and public document URLs")
        for version in (self.terms_version, self.privacy_version):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", version):
                raise RuntimeError("Legal policy versions must be ASCII identifiers of at most 64 characters")
        try:
            effective_at = datetime.fromisoformat(self.legal_effective_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RuntimeError("HEARO_LEGAL_EFFECTIVE_AT must be an ISO 8601 timestamp") from exc
        if effective_at.tzinfo is None:
            raise RuntimeError("HEARO_LEGAL_EFFECTIVE_AT must include a UTC offset")
        for url in (self.terms_url, self.privacy_url):
            parsed = urlsplit(url)
            local_development = (
                self.environment != "production"
                and parsed.scheme == "http"
                and parsed.hostname in {"localhost", "127.0.0.1"}
            )
            if (
                not parsed.hostname
                or (parsed.scheme != "https" and not local_development)
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or any(character.isspace() or ord(character) < 32 for character in url)
            ):
                raise RuntimeError("Legal document URLs must be public HTTPS URLs without credentials or query strings")

    def validate_for_production(self) -> None:
        self.validate_legal_policy()
        if self.environment != "production":
            return
        weak = {
            "development-only-change-me",
            "development-internal-token",
            "",
        }
        if (
            self.jwt_secret in weak
            or len(self.jwt_secret) < 32
            or "REPLACE_WITH" in self.jwt_secret
        ):
            raise RuntimeError("HEARO_JWT_SECRET must be a random value of at least 32 characters")
        if (
            self.internal_token in weak
            or len(self.internal_token) < 32
            or "REPLACE_WITH" in self.internal_token
        ):
            raise RuntimeError("HEARO_INTERNAL_TOKEN must be a random value of at least 32 characters")
        if "*" in self.cors_origins:
            raise RuntimeError("Wildcard CORS is forbidden in production")
        if self.store_backend != "dynamodb":
            raise RuntimeError("Production must use HEARO_STORE=dynamodb")
        if not self.mqtt_enabled:
            raise RuntimeError("Production requires HEARO_MQTT_ENABLED=true")
        if not self.mqtt_password or "REPLACE_WITH" in self.mqtt_password:
            raise RuntimeError("Production requires a non-placeholder MQTT password")
        if self.mqtt_port != 8883:
            raise RuntimeError("Production MQTT must use the TLS listener on port 8883")
        allowed_http_origins = {
            "http://localhost:5173",
        }
        if any(
            not origin.startswith("https://")
            and origin not in allowed_http_origins
            for origin in self.cors_origins
        ):
            raise RuntimeError(
                "Production CORS origins must use HTTPS "
                "except approved localhost development origins"
            )
        if not self.juso_confirm_key or "REPLACE_WITH" in self.juso_confirm_key:
            raise RuntimeError("Production requires a non-placeholder Juso search API key")
        if (
            not self.juso_detail_confirm_key
            or "REPLACE_WITH" in self.juso_detail_confirm_key
        ):
            raise RuntimeError("Production requires a non-placeholder Juso detail API key")
        if not 0 < self.juso_timeout_seconds <= 15:
            raise RuntimeError("HEARO_JUSO_TIMEOUT_SECONDS must be between 0 and 15")
        if not self.alarm_unread_baseline_at:
            raise RuntimeError("Production requires HEARO_ALARM_UNREAD_BASELINE_AT")
        try:
            baseline = datetime.fromisoformat(
                self.alarm_unread_baseline_at.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise RuntimeError(
                "HEARO_ALARM_UNREAD_BASELINE_AT must be an ISO 8601 timestamp"
            ) from exc
        if baseline.tzinfo is None:
            raise RuntimeError(
                "HEARO_ALARM_UNREAD_BASELINE_AT must include a UTC offset"
            )
