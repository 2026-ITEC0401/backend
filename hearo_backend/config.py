from __future__ import annotations

import os
from dataclasses import dataclass, field


def _csv_env(name: str, default: str) -> list[str]:
    return [value.strip() for value in os.getenv(name, default).split(",") if value.strip()]


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

    def validate_for_production(self) -> None:
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
        if any(not origin.startswith("https://") for origin in self.cors_origins):
            raise RuntimeError("Production CORS origins must use HTTPS")
        if not self.juso_confirm_key or "REPLACE_WITH" in self.juso_confirm_key:
            raise RuntimeError("Production requires a non-placeholder Juso search API key")
        if (
            not self.juso_detail_confirm_key
            or "REPLACE_WITH" in self.juso_detail_confirm_key
        ):
            raise RuntimeError("Production requires a non-placeholder Juso detail API key")
        if not 0 < self.juso_timeout_seconds <= 15:
            raise RuntimeError("HEARO_JUSO_TIMEOUT_SECONDS must be between 0 and 15")
