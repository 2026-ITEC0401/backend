"""Device-kit HTTP and memory contracts; these are not live-device/AWS tests."""
from __future__ import annotations

import copy
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

import pytest

from hearo_backend.domain import Alert, FIXED_DEVICES, Household, iso_utc

from .conftest import auth_header, create_family, create_owner


KIT_ID = "HEARO-KIT-0001"
CLAIM_CODE = "K7QM-29XA"
INPUT = {"kit_id": KIT_ID, "claim_code": CLAIM_CODE}
ORDER = ["rpi-001", "esp32_1", "esp32_2", "esp32_3"]


def kit_devices(suffix="0001"):
    return [
        {"device_id": role, "device_type": kind,
         "hardware_id": f"HR-{'RPI' if kind == 'hub' else 'ESP'}-{suffix}-{index}"}
        for index, (role, _location, kind) in enumerate(FIXED_DEVICES)
    ]


def issue_kit(repository, kit_id=KIT_ID, *, suffix="0001", code=CLAIM_CODE):
    from hearo_backend.device_kits import hash_claim_code

    repository.issue_device_kit(
        kit_id=kit_id, claim_code_hash=hash_claim_code(code),
        devices=kit_devices(suffix), created_at=iso_utc(),
    )


def kit_url(owner, action=""):
    return f"/households/{owner['user']['household_id']}/device-kit{action}"


def snapshot(repository):
    return copy.deepcopy({key: value for key, value in vars(repository).items()
                          if isinstance(value, dict)})


def linked_member(api, owner, index=1):
    member = create_family(api, login_id=f"kitmember{index}", phone_number=f"010-9001-{index:04d}")
    invite = api.client.get(
        f"/households/{owner['user']['household_id']}/invite-code",
        headers=auth_header(owner),
    ).json()["invite_code"]
    response = api.client.post("/households/link", headers=auth_header(member),
                               json={"invite_code": invite})
    assert response.status_code == 200, response.text
    return member


def assert_no_store(response):
    assert response.headers.get("cache-control") == "no-store"
    assert response.headers.get("x-request-id", "").startswith("req-")


def assert_no_secrets(value):
    forbidden = {"claim_code", "claim_code_hash", "credential_hash", "device_credential",
                 "mqtt_password", "audio_psk", "owner_user_id", "claimed_by_user_id"}
    if isinstance(value, dict):
        assert not forbidden.intersection(value)
        for child in value.values():
            assert_no_secrets(child)
    elif isinstance(value, list):
        for child in value:
            assert_no_secrets(child)
    else:
        assert value != CLAIM_CODE


def test_signup_and_new_kit_get_keep_existing_four_credentials_and_order(api):
    owner = create_owner(api)
    assert set(owner["device_credentials"]) == set(ORDER)
    response = api.client.get(kit_url(owner), headers=auth_header(owner))
    assert response.status_code == 200, response.text
    assert_no_store(response)
    value = response.json()
    assert {key: value[key] for key in ("status", "kit_id", "claimed_at", "can_claim")} == {
        "status": "unregistered", "kit_id": None, "claimed_at": None, "can_claim": True,
    }
    assert [item["device_id"] for item in value["devices"]] == ORDER
    for item in value["devices"]:
        assert set(item) == {"device_id", "device_type", "location", "hardware_id", "ui_status", "last_seen_at"}
        assert item["hardware_id"] is None
        assert item["ui_status"] == "offline"
        assert item["last_seen_at"] is None
    assert_no_secrets(value)


def test_permissions_for_member_other_house_unlinked_and_missing_auth(api):
    owner = create_owner(api)
    member = linked_member(api, owner)
    other = create_owner(api, login_id="kitother", phone_number="010-9002-0001")
    unlinked = create_family(api, login_id="kitunlinked", phone_number="010-9002-0002")
    response = api.client.get(kit_url(owner), headers=auth_header(member))
    assert response.status_code == 200
    assert response.json()["can_claim"] is False
    assert_no_store(response)
    before = snapshot(api.repository)
    for method, suffix in (("GET", ""), ("POST", "/claim/preview"), ("POST", "/claim")):
        for who, expected, code in (
            (None, 401, "AUTHENTICATION_REQUIRED"),
            (other, 403, "HOUSEHOLD_ACCESS_DENIED"),
            (unlinked, 409, "HOUSEHOLD_LINK_REQUIRED"),
        ):
            response = api.client.request(method, kit_url(owner, suffix),
                                          headers=auth_header(who) if who else {},
                                          **({"json": INPUT} if method == "POST" else {}))
            assert response.status_code == expected, response.text
            assert response.json()["code"] == code
            assert_no_store(response)
        if method == "POST":
            response = api.client.post(kit_url(owner, suffix), headers=auth_header(member), json=INPUT)
            assert response.status_code == 403 and response.json()["code"] == "OWNER_REQUIRED"
            assert_no_store(response)
    assert snapshot(api.repository) == before


@pytest.mark.parametrize("payload", [
    {}, {"kit_id": KIT_ID}, {"claim_code": CLAIM_CODE},
    {**INPUT, "owner_user_id": "forbidden"},
    {**INPUT, "hardware_id": "forbidden"},
    {**INPUT, "claimed_at": "2026-01-01T00:00:00Z"},
    {**INPUT, "kit_id": "HEARO-KIT-０００１"},
    {**INPUT, "kit_id": "HEARO-KIT-ß001"},
    {**INPUT, "kit_id": "HEARO-KIT-000 1"},
    {**INPUT, "claim_code": "K7QM–29XA"},
    {**INPUT, "claim_code": "K7QM-２9XA"},
    {**INPUT, "claim_code": "K7Q -29XA"},
    {**INPUT, "kit_id": " " * 51 + KIT_ID},
    {**INPUT, "claim_code": " " * 24 + CLAIM_CODE},
    {**INPUT, "claim_code": 123456789},
])
def test_strict_registration_input_rejects_before_writes(api, payload):
    owner = create_owner(api)
    before = snapshot(api.repository)
    for suffix in ("/claim/preview", "/claim"):
        response = api.client.post(kit_url(owner, suffix), headers=auth_header(owner), json=payload)
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "VALIDATION_ERROR"
        assert_no_store(response)
    assert snapshot(api.repository) == before


def test_normalized_preview_is_read_only_ordered_and_hides_secret_material(api):
    owner = create_owner(api)
    issue_kit(api.repository)
    before = snapshot(api.repository)
    before_mqtt = copy.deepcopy(api.mqtt.published)
    response = api.client.post(kit_url(owner, "/claim/preview"), headers=auth_header(owner),
                               json={"kit_id": "  hearo-kit-0001  ", "claim_code": " k7qm-29xa "})
    assert response.status_code == 200, response.text
    assert_no_store(response)
    value = response.json()
    assert value["claimable"] is True and value["kit_id"] == KIT_ID
    assert [item["device_id"] for item in value["devices"]] == ORDER
    assert [item["hardware_id"] for item in value["devices"]] == [item["hardware_id"] for item in kit_devices()]
    assert_no_secrets(value)
    assert snapshot(api.repository) == before
    assert api.mqtt.published == before_mqtt


def test_missing_kit_and_wrong_code_are_indistinguishable_and_read_only(api):
    owner = create_owner(api)
    issue_kit(api.repository)
    before = snapshot(api.repository)
    for suffix in ("/claim/preview", "/claim"):
        bodies = []
        for payload in ({**INPUT, "kit_id": "HEARO-KIT-NONE"}, {**INPUT, "claim_code": "AAAA-BBBB"}):
            response = api.client.post(kit_url(owner, suffix), headers=auth_header(owner), json=payload)
            assert response.status_code == 400, response.text
            assert_no_store(response)
            value = response.json()
            assert value["code"] == "INVALID_KIT_CREDENTIALS"
            bodies.append({key: value[key] for key in ("code", "message", "field_errors")})
            assert_no_secrets(value)
        assert bodies[0] == bodies[1]
    assert snapshot(api.repository) == before


def test_claim_preserves_live_device_configuration_credentials_and_mqtt(api):
    owner = create_owner(api)
    home = owner["user"]["household_id"]
    headers = auth_header(owner)
    issue_kit(api.repository)
    led = api.client.patch(f"/households/{home}/devices/esp32_1/settings", headers=headers,
                           json={"led_alert_enabled": False})
    assert led.status_code == 200
    credential = owner["device_credentials"]["esp32_1"]
    assert api.client.post("/device/v1/heartbeat", headers={"X-Device-Credential": credential}, json={
        "mqtt_connected": True, "config_version": led.json()["config_version"],
        "firmware_version": "kit-regression", "audio_streaming": True,
        "microphone_ok": True, "audio_packets_sent": 43, "audio_packets_dropped": 2,
    }).status_code == 200
    old_devices = {item.device_id: asdict(item) for item in api.repository.list_devices(home)}
    old_credentials = copy.deepcopy(api.repository.device_credentials)
    old_mqtt = copy.deepcopy(api.mqtt.published)
    old_states = {item["device_id"]: item for item in api.client.get(
        f"/households/{home}/devices", headers=headers).json()["devices"]}
    response = api.client.post(kit_url(owner, "/claim"), headers=headers, json=INPUT)
    assert response.status_code == 200, response.text
    assert_no_store(response)
    value = response.json()
    assert value["status"] == "claimed" and value["can_claim"] is False
    assert value["kit_id"] == KIT_ID and value["claimed_at"].endswith("Z")
    assert [item["device_id"] for item in value["devices"]] == ORDER
    for item in api.repository.list_devices(home):
        current = asdict(item)
        current.pop("kit_id")
        current.pop("hardware_id")
        before = old_devices[item.device_id]
        before.pop("kit_id", None)
        before.pop("hardware_id", None)
        assert current == before
    for item in value["devices"]:
        assert item["ui_status"] == old_states[item["device_id"]]["ui_status"]
        assert item["last_seen_at"] == old_states[item["device_id"]]["last_seen_at"]
    assert api.repository.device_credentials == old_credentials
    assert api.mqtt.published == old_mqtt
    assert api.client.get("/device/v1/config", headers={"X-Device-Credential": credential}).status_code == 200
    assert_no_secrets(value)


def test_repeated_claim_keeps_first_mapping_wrong_code_fails_and_preview_conflicts(api):
    owner = create_owner(api)
    issue_kit(api.repository)
    headers = auth_header(owner)
    first = api.client.post(kit_url(owner, "/claim"), headers=headers, json=INPUT)
    assert first.status_code == 200, first.text
    before = snapshot(api.repository)
    again = api.client.post(kit_url(owner, "/claim"), headers=headers, json=INPUT)
    assert again.status_code == 200 and again.json() == first.json()
    wrong = api.client.post(kit_url(owner, "/claim"), headers=headers,
                            json={**INPUT, "claim_code": "AAAA-BBBB"})
    assert wrong.status_code == 400 and wrong.json()["code"] == "INVALID_KIT_CREDENTIALS"
    preview = api.client.post(kit_url(owner, "/claim/preview"), headers=headers, json=INPUT)
    assert preview.status_code == 409 and preview.json()["code"] == "HOUSEHOLD_ALREADY_HAS_KIT"
    assert snapshot(api.repository) == before


def test_other_household_claim_and_second_kit_have_distinct_conflicts(api):
    owner = create_owner(api)
    other = create_owner(api, login_id="kitother", phone_number="010-9002-0001")
    issue_kit(api.repository)
    issue_kit(api.repository, "HEARO-KIT-0002", suffix="0002")
    assert api.client.post(kit_url(owner, "/claim"), headers=auth_header(owner), json=INPUT).status_code == 200
    before = snapshot(api.repository)
    foreign = api.client.post(kit_url(other, "/claim"), headers=auth_header(other), json=INPUT)
    assert foreign.status_code == 409 and foreign.json()["code"] == "KIT_ALREADY_CLAIMED"
    assert owner["user"]["household_id"] not in foreign.text
    second = api.client.post(kit_url(owner, "/claim"), headers=auth_header(owner),
                             json={**INPUT, "kit_id": "HEARO-KIT-0002"})
    assert second.status_code == 409 and second.json()["code"] == "HOUSEHOLD_ALREADY_HAS_KIT"
    assert snapshot(api.repository) == before


def test_legacy_household_remains_operational_without_requiring_claim(api):
    owner = create_owner(api)
    home = owner["user"]["household_id"]
    assert Household("legacy", "기존 가구", "legacy-owner").device_kit_status == "legacy_registered"
    api.repository.households[home] = replace(api.repository.households[home], device_kit_status="legacy_registered")
    response = api.client.get(kit_url(owner), headers=auth_header(owner))
    assert response.status_code == 200
    assert {key: response.json()[key] for key in ("status", "kit_id", "claimed_at", "can_claim")} == {
        "status": "legacy_registered", "kit_id": None, "claimed_at": None, "can_claim": False,
    }
    issue_kit(api.repository)
    before = snapshot(api.repository)
    for suffix in ("/claim/preview", "/claim"):
        response = api.client.post(kit_url(owner, suffix), headers=auth_header(owner), json=INPUT)
        assert response.status_code == 409 and response.json()["code"] == "HOUSEHOLD_ALREADY_HAS_KIT"
    assert snapshot(api.repository) == before
    assert api.client.get("/device/v1/config", headers={
        "X-Device-Credential": owner["device_credentials"]["esp32_1"],
    }).status_code == 200


def test_preview_claim_share_user_limit_but_get_validation_and_permission_do_not(api):
    owner = create_owner(api)
    member = linked_member(api, owner)
    for _ in range(12):
        assert api.client.get(kit_url(owner), headers=auth_header(owner)).status_code == 200
        assert api.client.post(kit_url(owner, "/claim/preview"), headers=auth_header(owner), json={}).status_code == 422
        assert api.client.post(kit_url(owner, "/claim"), headers=auth_header(member), json=INPUT).status_code == 403
    for index in range(10):
        suffix = "/claim/preview" if index % 2 else "/claim"
        response = api.client.post(kit_url(owner, suffix), headers=auth_header(owner),
                                   json={**INPUT, "kit_id": f"HEARO-KIT-{index:04d}"})
        assert response.status_code == 400, response.text
    blocked = api.client.post(kit_url(owner, "/claim"), headers=auth_header(owner), json=INPUT)
    assert blocked.status_code == 429 and blocked.json()["code"] == "KIT_CLAIM_RATE_LIMITED"
    assert_no_store(blocked)
    assert api.client.get(kit_url(owner), headers=auth_header(owner)).status_code == 200


def test_ip_limit_is_shared_across_distinct_owners_and_both_post_paths(api):
    owners = [create_owner(api, login_id=f"kitrate{index}", phone_number=f"010-9010-{index:04d}")
              for index in range(4)]
    for index in range(30):
        owner = owners[index % 3]
        response = api.client.post(kit_url(owner, "/claim/preview" if index % 2 else "/claim"),
                                   headers=auth_header(owner), json=INPUT)
        assert response.status_code == 400, response.text
    blocked = api.client.post(kit_url(owners[3], "/claim"), headers=auth_header(owners[3]), json=INPUT)
    assert blocked.status_code == 429 and blocked.json()["code"] == "KIT_CLAIM_RATE_LIMITED"


def test_storage_error_is_sanitized_503_without_secret_echo(api, monkeypatch):
    owner = create_owner(api)

    def unavailable(*args, **kwargs):
        raise RuntimeError(f"PRIVATE-STORAGE {CLAIM_CODE} {owner['tokens']['access_token']}")

    monkeypatch.setattr(api.repository, "get_device_kit_state", unavailable)
    response = api.client.get(kit_url(owner), headers=auth_header(owner))
    assert response.status_code == 503
    assert response.json()["code"] == "KIT_SERVICE_UNAVAILABLE"
    assert_no_store(response)
    assert "PRIVATE-STORAGE" not in response.text and CLAIM_CODE not in response.text
    assert owner["tokens"]["access_token"] not in response.text


def test_member_withdrawal_keeps_binding_owner_withdrawal_does_not_release_kit(api):
    owner = create_owner(api)
    member = linked_member(api, owner)
    peer = linked_member(api, owner, 2)
    home = owner["user"]["household_id"]
    issue_kit(api.repository)
    assert api.client.post(kit_url(owner, "/claim"), headers=auth_header(owner), json=INPUT).status_code == 200
    inventory = copy.deepcopy((api.repository.device_kits, api.repository.hardware_kits))
    retained = Alert(home, "kit-retained", iso_utc(datetime.now(UTC) - timedelta(days=1)),
                     "esp32_1", "안방", "아기울음소리", "Noise")
    assert api.repository.put_alert(retained) is True
    alerts_before = copy.deepcopy(api.repository.alerts)
    assert api.client.request("DELETE", "/me", headers=auth_header(member),
                              json={"current_password": "MemberPassword123"}).status_code == 204
    assert (api.repository.device_kits, api.repository.hardware_kits) == inventory
    assert api.client.get(kit_url(owner), headers=auth_header(owner)).json()["status"] == "claimed"
    assert api.client.request("DELETE", "/me", headers=auth_header(owner),
                              json={"current_password": "StrongPassword123"}).status_code == 204
    assert (api.repository.device_kits, api.repository.hardware_kits) == inventory
    assert api.repository.alerts == alerts_before
    assert api.repository.get_user(peer["user"]["user_id"]).household_link_status == "unlinked"
    assert "claimed_by_user_id" not in repr(inventory) and "owner_user_id" not in repr(inventory)
    another = create_owner(api, login_id="kitafter", phone_number="010-9019-0001")
    response = api.client.post(kit_url(another, "/claim"), headers=auth_header(another), json=INPUT)
    assert response.status_code == 409 and response.json()["code"] == "KIT_ALREADY_CLAIMED"
