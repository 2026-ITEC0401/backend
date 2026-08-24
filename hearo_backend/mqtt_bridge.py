"""TLS MQTT -> FastAPI internal ingestion bridge."""

from __future__ import annotations

import json
import os
import ssl
import time
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import paho.mqtt.client as mqtt

from .config import Settings


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class MqttBridge:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self.api_base_url = os.getenv("HEARO_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
        self.legacy_household_id = os.getenv("HEARO_LEGACY_HOUSEHOLD_ID", "")
        try:
            self.client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2, client_id="hearo-mqtt-bridge-v2"
            )
        except (AttributeError, TypeError):
            self.client = mqtt.Client(client_id="hearo-mqtt-bridge-v2")
        self.client.username_pw_set(self.settings.mqtt_username, self.settings.mqtt_password)
        self.client.tls_set(
            ca_certs=self.settings.mqtt_ca_path, cert_reqs=ssl.CERT_REQUIRED
        )
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    @staticmethod
    def _success(reason_code: Any) -> bool:
        try:
            return int(reason_code) == 0
        except (TypeError, ValueError):
            return str(reason_code).casefold() in {"success", "0"}

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if not self._success(reason_code):
            print(f"MQTT bridge connection failed: {reason_code}")
            return
        client.subscribe("hearo/+/alerts", qos=1)
        client.subscribe("hearo/+/devices/+/state", qos=1)
        client.subscribe("hearo/+/devices/+/ack", qos=1)
        client.subscribe("hearo/+/devices/+/presence", qos=1)
        if self.legacy_household_id:
            client.subscribe("hearo/log", qos=1)
        print("MQTT bridge subscribed to household-scoped topics")

    def _post(self, path: str, payload: dict[str, Any]) -> None:
        request = Request(
            f"{self.api_base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Internal-Token": self.settings.internal_token,
            },
        )
        with urlopen(request, timeout=8) as response:
            response.read()

    def _on_message(self, client, userdata, message):
        try:
            payload = json.loads(message.payload.decode("utf-8"))
            topic = message.topic.split("/")
            if message.topic == "hearo/log":
                self._ingest_legacy(payload)
            elif len(topic) == 3 and topic[2] == "alerts":
                payload["household_id"] = topic[1]
                self._post("/internal/mqtt/alert", payload)
            elif len(topic) == 5 and topic[2] == "devices":
                payload["household_id"] = topic[1]
                payload["device_id"] = topic[3]
                payload["seen_at"] = _utc_now()
                payload.setdefault("mqtt_connected", topic[4] != "presence")
                payload.setdefault("config_version", 0)
                if topic[4] == "presence":
                    payload.setdefault("network_online", False)
                self._post("/internal/mqtt/device-state", payload)
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, HTTPError, URLError, OSError) as exc:
            print(f"MQTT bridge dropped invalid/unavailable message: {type(exc).__name__}")

    def _ingest_legacy(self, payload: dict[str, Any]) -> None:
        device_id = payload.get("source_device_id") or payload.get("device_id") or "rpi-001"
        alert = {
            "household_id": self.legacy_household_id,
            "event_id": payload.get("event_id") or uuid.uuid4().hex,
            "timestamp": payload.get("timestamp") or _utc_now(),
            "source_device_id": device_id,
            "publisher_device_id": payload.get("publisher_device_id", device_id),
            "capture_device_id": payload.get("capture_device_id", device_id),
            "location": payload.get("location", "거실"),
            "sound": payload["sound"],
            "raw_label": payload.get("raw_label"),
            "type": payload.get("type", "Urgent"),
            "confidence": payload.get("confidence"),
            "model_version": payload.get("model_version"),
            "decision_source": payload.get("decision_source"),
            "confidence_kind": payload.get("confidence_kind"),
            "yamnet_family": payload.get("yamnet_family"),
            "yamnet_score": payload.get("yamnet_score"),
            "hearo_confidence": payload.get("hearo_confidence"),
            "applied_threshold": payload.get("applied_threshold"),
            "policy_version": payload.get("policy_version"),
        }
        self._post("/internal/mqtt/alert", alert)

    def run_forever(self) -> None:
        while True:
            try:
                self.client.connect(self.settings.mqtt_host, self.settings.mqtt_port, 60)
                self.client.loop_forever(retry_first_connection=True)
            except (OSError, RuntimeError) as exc:
                print(f"MQTT bridge reconnecting after {type(exc).__name__}")
                time.sleep(5)


def main() -> None:
    MqttBridge().run_forever()


if __name__ == "__main__":
    main()
