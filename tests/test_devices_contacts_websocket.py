from datetime import UTC, datetime, timedelta

from hearo_backend.domain import Device, iso_utc
from hearo_backend.config import Settings
from hearo_backend.services import device_status

from .conftest import auth_header, create_owner


def test_device_state_machine_old_ack_and_credential_rotation(api):
    owner = create_owner(api)
    household_id = owner["user"]["household_id"]
    owner_headers = auth_header(owner)
    credential = owner["device_credentials"]["esp32_2"]
    device_headers = {"X-Device-Credential": credential}

    initial = api.client.get(
        f"/households/{household_id}/devices", headers=owner_headers
    ).json()["devices"]
    entrance = next(item for item in initial if item["device_id"] == "esp32_2")
    assert entrance["location"] == "현관"
    assert entrance["ui_status"] == "offline"
    assert "credential_hash" not in entrance

    connected = api.client.post(
        "/device/v1/heartbeat",
        headers=device_headers,
        json={
            "mqtt_connected": True,
            "config_version": 1,
            "firmware_version": "test",
            "audio_streaming": True,
            "microphone_ok": True,
            "audio_packets_sent": 750,
            "audio_packets_dropped": 3,
            "audio_clipped_samples": 2,
        },
    )
    assert connected.status_code == 200
    reported = api.client.get(
        f"/households/{household_id}/devices", headers=owner_headers
    ).json()["devices"]
    entrance = next(item for item in reported if item["device_id"] == "esp32_2")
    assert entrance["audio_streaming"] is True
    assert entrance["microphone_ok"] is True
    stored_device = api.repository.get_device(household_id, "esp32_2")
    assert stored_device.audio_packets_sent == 750
    assert stored_device.audio_packets_dropped == 3
    assert stored_device.audio_clipped_samples == 2
    assert "capabilities" not in entrance

    off = api.client.patch(
        f"/households/{household_id}/devices/esp32_2/connection",
        headers=owner_headers,
        json={"enabled": False},
    )
    assert off.status_code == 200
    assert off.json()["ui_status"] == "pending"
    assert api.mqtt.published[-1][0] == (
        f"hearo/{household_id}/devices/esp32_2/command"
    )
    off_version = off.json()["config_version"]

    acknowledged_off = api.client.post(
        "/device/v1/heartbeat",
        headers=device_headers,
        json={"mqtt_connected": False, "config_version": off_version},
    )
    assert acknowledged_off.status_code == 200
    values = api.client.get(
        f"/households/{household_id}/devices", headers=owner_headers
    ).json()["devices"]
    assert next(item for item in values if item["device_id"] == "esp32_2")["ui_status"] == "disabled_by_owner"

    on = api.client.patch(
        f"/households/{household_id}/devices/esp32_2/connection",
        headers=owner_headers,
        json={"enabled": True},
    )
    on_version = on.json()["config_version"]
    assert on.json()["ui_status"] == "pending"

    api.client.post(
        "/device/v1/heartbeat",
        headers=device_headers,
        json={"mqtt_connected": False, "config_version": off_version},
    )
    stale_values = api.client.get(
        f"/households/{household_id}/devices", headers=owner_headers
    ).json()["devices"]
    stale_bathroom = next(item for item in stale_values if item["device_id"] == "esp32_2")
    assert api.repository.get_device(household_id, "esp32_2").reported_config_version == off_version
    assert stale_bathroom["ui_status"] == "pending"

    api.client.post(
        "/device/v1/heartbeat",
        headers=device_headers,
        json={"mqtt_connected": True, "config_version": on_version},
    )
    values = api.client.get(
        f"/households/{household_id}/devices", headers=owner_headers
    ).json()["devices"]
    assert next(item for item in values if item["device_id"] == "esp32_2")["ui_status"] == "connected"

    rotated = api.client.post(
        f"/households/{household_id}/devices/esp32_2/credential/rotate",
        headers=owner_headers,
    )
    assert rotated.status_code == 200
    assert api.client.get("/device/v1/config", headers=device_headers).status_code == 401
    assert api.client.get(
        "/device/v1/config",
        headers={"X-Device-Credential": rotated.json()["device_credential"]},
    ).status_code == 200


def test_contact_e164_and_websocket_authentication(api):
    owner = create_owner(api)
    household_id = owner["user"]["household_id"]
    headers = auth_header(owner)
    added = api.client.post(
        f"/households/{household_id}/contacts",
        headers=headers,
        json={"name": "어머니", "relationship": "보호자", "phone_number": "010-1234-5678"},
    )
    assert added.status_code == 201
    assert added.json()["phone_number"] == "+821012345678"

    contacts = api.client.get(
        f"/households/{household_id}/contacts", headers=headers
    ).json()["contacts"]
    assert contacts[0]["phone_number"] == "+821012345678"
    edited = api.client.patch(
        f"/households/{household_id}/contacts/{contacts[0]['contact_id']}",
        headers=headers,
        json={"name": "아버지", "relationship": "보호자", "phone_number": "010-9999-8888"},
    )
    assert edited.status_code == 200
    assert edited.json()["phone_number"] == "+821099998888"
    assert api.client.delete(
        f"/households/{household_id}/contacts/{contacts[0]['contact_id']}", headers=headers
    ).status_code == 204

    with api.client.websocket_connect(f"/ws/households/{household_id}") as websocket:
        websocket.send_json(
            {"type": "auth", "access_token": owner["tokens"]["access_token"]}
        )
        ready = websocket.receive_json()
        assert ready["type"] == "connection.ready"
        assert len(ready["devices"]) == 4
        changed = api.client.patch(
            f"/households/{household_id}/devices/esp32_3/connection",
            headers=headers,
            json={"enabled": False},
        )
        assert changed.status_code == 200
        event = websocket.receive_json()
        assert event["type"] == "device.status_changed"
        assert event["device_id"] == "esp32_3"

        alert_response = api.client.post(
            "/internal/mqtt/alert",
            headers={"X-Internal-Token": api.settings.internal_token},
            json={
                "household_id": household_id,
                "event_id": "websocket-alert-001",
                "timestamp": iso_utc(),
                "source_device_id": "rpi-001",
                "location": "거실",
                "sound": "도어락소리",
                "raw_label": "도어락_개방음",
                "type": "Visitor",
                "confidence": 0.91,
            },
        )
        assert alert_response.status_code == 200
        alarm_event = websocket.receive_json()
        assert alarm_event["type"] == "alarm.created"
        assert alarm_event["alarm"]["raw_label"] == "도어락_개방음"
        assert alarm_event["alarm"]["type"] == "Visitor"

        websocket.send_json({"type": "ping"})
        assert websocket.receive_json()["type"] == "pong"


def test_led_only_settings_and_timeout_status(api):
    owner = create_owner(api)
    household_id = owner["user"]["household_id"]
    headers = auth_header(owner)
    rpi_credential = owner["device_credentials"]["rpi-001"]
    device_headers = {"X-Device-Credential": rpi_credential}

    api.client.post(
        "/device/v1/heartbeat",
        headers=device_headers,
        json={"mqtt_connected": True, "config_version": 1},
    )
    changed = api.client.patch(
        f"/households/{household_id}/devices/rpi-001/settings",
        headers=headers,
        json={"led_alert_enabled": False},
    )
    assert changed.status_code == 200
    assert changed.json()["ui_status"] == "pending"
    assert "capabilities" not in changed.json()
    assert "sensitivity" not in changed.json()
    command = api.mqtt.published[-1][1]
    assert command["type"] == "device.config.set"
    assert "sensitivity" not in command

    config = api.client.get("/device/v1/config", headers=device_headers).json()
    assert config["led_alert_enabled"] is False
    assert "sensitivity" not in config
    assert "capabilities" not in config
    api.client.post(
        "/device/v1/heartbeat",
        headers=device_headers,
        json={"mqtt_connected": True, "config_version": config["config_version"]},
    )
    values = api.client.get(
        f"/households/{household_id}/devices", headers=headers
    ).json()["devices"]
    assert next(item for item in values if item["device_id"] == "rpi-001")["ui_status"] == "connected"

    unsupported = api.client.patch(
        f"/households/{household_id}/devices/esp32_1/settings",
        headers=headers,
        json={"led_alert_enabled": True, "sensitivity": "low"},
    )
    assert unsupported.status_code == 422

    now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    stale_command = Device(
        household_id="home-test",
        device_id="esp32_3",
        location="화장실",
        device_type="alert_node",
        desired_mqtt_connected=True,
        reported_mqtt_connected=False,
        reported_network_online=True,
        last_seen_at=iso_utc(now),
        desired_updated_at=iso_utc(now - timedelta(seconds=31)),
        config_version=2,
        reported_config_version=1,
    )
    assert device_status(stale_command, api.settings, now)["ui_status"] == "error"
    stale_command.last_seen_at = iso_utc(now - timedelta(seconds=46))
    assert device_status(stale_command, api.settings, now)["ui_status"] == "offline"


def test_production_rejects_placeholder_secrets():
    settings = Settings(
        environment="production",
        store_backend="dynamodb",
        jwt_secret="REPLACE_WITH_RANDOM_32_PLUS_CHARACTERS",
        internal_token="REPLACE_WITH_A_DIFFERENT_RANDOM_32_PLUS_CHARACTERS",
        mqtt_enabled=True,
        mqtt_password="REPLACE_WITH_BROKER_PASSWORD",
        cors_origins=["https://frontend.example.com"],
    )
    try:
        settings.validate_for_production()
    except RuntimeError as exc:
        assert "JWT" in str(exc)
    else:
        raise AssertionError("placeholder secret must be rejected")


def test_production_requires_juso_search_api_key():
    settings = Settings(
        environment="production",
        store_backend="dynamodb",
        jwt_secret="valid-jwt-secret-that-is-at-least-thirty-two-characters",
        internal_token="valid-internal-token-that-is-at-least-thirty-two-characters",
        mqtt_enabled=True,
        mqtt_password="valid-mqtt-password",
        cors_origins=["https://frontend.example.com"],
        juso_confirm_key="",
        juso_detail_confirm_key="valid-detail-api-key",
    )
    try:
        settings.validate_for_production()
    except RuntimeError as exc:
        assert "Juso" in str(exc)
    else:
        raise AssertionError("production Juso search API key must be required")


def test_production_requires_juso_detail_api_key():
    settings = Settings(
        environment="production",
        store_backend="dynamodb",
        jwt_secret="valid-jwt-secret-that-is-at-least-thirty-two-characters",
        internal_token="valid-internal-token-that-is-at-least-thirty-two-characters",
        mqtt_enabled=True,
        mqtt_password="valid-mqtt-password",
        cors_origins=["https://frontend.example.com"],
        juso_confirm_key="valid-search-api-key",
        juso_detail_confirm_key="",
    )
    try:
        settings.validate_for_production()
    except RuntimeError as exc:
        assert "detail" in str(exc)
    else:
        raise AssertionError("production Juso detail API key must be required")
