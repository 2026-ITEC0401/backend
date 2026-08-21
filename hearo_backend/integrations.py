from __future__ import annotations

import json
import logging
import ssl
from typing import Any

from .config import Settings


LOGGER = logging.getLogger(__name__)


class NullMqttPublisher:
    def __init__(self):
        self.published: list[tuple[str, dict[str, Any], bool]] = []

    def publish(self, topic: str, payload: dict[str, Any], retain: bool = False) -> bool:
        self.published.append((topic, payload, retain))
        return True


class MqttPublisher:
    def __init__(self, settings: Settings):
        import paho.mqtt.client as mqtt

        self.settings = settings
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="hearo-api-v2")
        self.client.username_pw_set(settings.mqtt_username, settings.mqtt_password)
        self.client.tls_set(ca_certs=settings.mqtt_ca_path, cert_reqs=ssl.CERT_REQUIRED)
        self.client.connect_async(settings.mqtt_host, settings.mqtt_port, 60)
        self.client.loop_start()

    def publish(self, topic: str, payload: dict[str, Any], retain: bool = False) -> bool:
        info = self.client.publish(
            topic,
            json.dumps(payload, ensure_ascii=False),
            qos=1,
            retain=retain,
        )
        if info.rc != 0:
            # The desired state is already persisted and devices poll HTTPS,
            # so a transient broker outage must not turn a valid API change
            # into an ambiguous 500 response.
            LOGGER.warning("MQTT publish deferred for %s (code=%s)", topic, info.rc)
            return False
        return True


def create_mqtt_publisher(settings: Settings):
    if settings.mqtt_enabled:
        return MqttPublisher(settings)
    return NullMqttPublisher()
