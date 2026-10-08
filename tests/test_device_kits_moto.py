"""Optional Moto kit transaction checks, never evidence of live AWS behavior."""
from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError, ReadTimeoutError
from fastapi.testclient import TestClient
import pytest

moto = pytest.importorskip("moto")

from hearo_backend.config import Settings
from hearo_backend.domain import Alert, iso_utc
from hearo_backend.integrations import NullMqttPublisher
from hearo_backend.main import create_app
from hearo_backend.store import DynamoRepository

from .conftest import auth_header, create_owner
from .test_device_kits import (
    CLAIM_CODE, INPUT, KIT_ID, ORDER, issue_kit, kit_devices, kit_url, linked_member,
)


@pytest.fixture
def ddb_api(monkeypatch):
    for key, value in {
        "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing", "AWS_EC2_METADATA_DISABLED": "true",
    }.items():
        monkeypatch.setenv(key, value)
    with moto.mock_aws():
        resource = boto3.resource("dynamodb", region_name="ap-south-1")
        for name, partition, sort in (
            ("kit-offline-core", "pk", "sk"),
            ("kit-offline-alerts", "household_id", "event_key"),
        ):
            resource.create_table(
                TableName=name, BillingMode="PAY_PER_REQUEST",
                KeySchema=[{"AttributeName": partition, "KeyType": "HASH"},
                           {"AttributeName": sort, "KeyType": "RANGE"}],
                AttributeDefinitions=[{"AttributeName": partition, "AttributeType": "S"},
                                      {"AttributeName": sort, "AttributeType": "S"}],
            )
        settings = Settings(environment="test", store_backend="dynamodb", mqtt_enabled=False,
                            region="ap-south-1", core_table="kit-offline-core", alerts_table="kit-offline-alerts")
        repository = DynamoRepository(settings)
        publisher = NullMqttPublisher()
        app = create_app(settings, repository, publisher)
        with TestClient(app) as client:
            yield SimpleNamespace(client=client, repository=repository, settings=settings, mqtt=publisher)


def all_rows(table):
    rows = []
    kwargs = {"ConsistentRead": True}
    while True:
        response = table.scan(**kwargs)
        rows.extend(response.get("Items", []))
        if not response.get("LastEvaluatedKey"):
            return rows
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def core_snapshot(repository):
    return {(row["pk"], row["sk"]): copy.deepcopy(row) for row in all_rows(repository.core)}


def claim(repository, owner, kit_id=KIT_ID):
    user = repository.get_user_consistent(owner["user"]["user_id"])
    return repository.claim_device_kit(user, repository.settings, kit_id, CLAIM_CODE)


def update_keys(operations):
    deserialize = TypeDeserializer().deserialize
    result = []
    for operation in operations:
        value = next(iter(operation.values()))
        result.append(tuple(deserialize(value["Key"][field]) for field in ("pk", "sk")))
    return result


def test_moto_claim_uses_unique_conditional_partial_updates_and_preserves_other_rows(ddb_api, monkeypatch):
    owner = create_owner(ddb_api)
    home = owner["user"]["household_id"]
    repo = ddb_api.repository
    issue_kit(repo)
    before = core_snapshot(repo)
    seen = []
    original = repo.client.transact_write_items

    def observed(**kwargs):
        seen.append(copy.deepcopy(kwargs["TransactItems"]))
        return original(**kwargs)

    monkeypatch.setattr(repo.client, "transact_write_items", observed)
    response = ddb_api.client.post(kit_url(owner, "/claim"), headers=auth_header(owner), json=INPUT)
    assert response.status_code == 200, response.text
    assert len(seen) == 1
    operations = seen[0]
    assert len(operations) == 11
    assert sum("Update" in operation for operation in operations) == 10
    assert sum("ConditionCheck" in operation for operation in operations) == 1
    keys = update_keys(operations)
    assert len(set(keys)) == len(keys)
    expected_updates = {(f"KIT#{KIT_ID}", "META"), (f"HOUSE#{home}", "META")}
    expected_updates.update((f"HOUSE#{home}", f"DEVICE#{role}") for role in ORDER)
    expected_updates.update((f"HARDWARE#{item['hardware_id']}", "KIT") for item in kit_devices())
    assert set(keys) == expected_updates | {(f"USER#{owner['user']['user_id']}", "PROFILE")}
    for operation in operations:
        value = next(iter(operation.values()))
        assert value["TableName"] == repo.settings.core_table
        assert value.get("ConditionExpression")
        if "Update" in operation and update_keys([operation])[0][1].startswith("DEVICE#"):
            expression = value["UpdateExpression"]
            assert "kit_id" in expression and "hardware_id" in expression
            assert not any(field in expression for field in (
                "credential_hash", "config_version", "led_alert_enabled",
                "desired_mqtt_connected", "audio_packets_sent",
            ))
    after = core_snapshot(repo)
    assert set(after) == set(before)
    assert all(after[key] == value for key, value in before.items() if key not in expected_updates)
    for role in ORDER:
        key = (f"HOUSE#{home}", f"DEVICE#{role}")
        old, current = copy.deepcopy(before[key]), copy.deepcopy(after[key])
        for field in ("kit_id", "hardware_id"):
            old.pop(field, None)
            current.pop(field, None)
        assert old == current
    inventory = [row for key, row in after.items() if key[0].startswith(("KIT#", "HARDWARE#"))]
    assert len(inventory) == 9
    assert not any("expires_at_epoch" in row or "owner_user_id" in row or "claimed_by_user_id" in row
                   for row in inventory)
    assert ddb_api.mqtt.published == []


def test_moto_preview_uses_consistent_reads_and_makes_no_transaction(ddb_api, monkeypatch):
    owner = create_owner(ddb_api)
    repo = ddb_api.repository
    issue_kit(repo)
    before = core_snapshot(repo)
    reads = []
    originals = {name: getattr(repo.core, name) for name in ("get_item", "query")}
    for name, original in originals.items():
        def observed(_original=original, **kwargs):
            reads.append(kwargs)
            return _original(**kwargs)
        monkeypatch.setattr(repo.core, name, observed)
    monkeypatch.setattr(repo.client, "transact_write_items", lambda **kwargs: pytest.fail("Preview wrote transaction"))
    response = ddb_api.client.post(kit_url(owner, "/claim/preview"), headers=auth_header(owner), json=INPUT)
    assert response.status_code == 200, response.text
    assert reads and all(read.get("ConsistentRead") is True for read in reads)
    assert core_snapshot(repo) == before


def test_moto_same_house_same_kit_repeat_does_not_write_again(ddb_api, monkeypatch):
    owner = create_owner(ddb_api)
    repo = ddb_api.repository
    issue_kit(repo)
    first = claim(repo, owner)
    before = core_snapshot(repo)
    monkeypatch.setattr(repo.client, "transact_write_items", lambda **kwargs: pytest.fail("Idempotent claim wrote"))
    assert claim(repo, owner) == first
    assert core_snapshot(repo) == before


def test_moto_two_houses_one_kit_only_one_claim_commits(ddb_api, monkeypatch):
    from hearo_backend.device_kits import KitError

    first = create_owner(ddb_api)
    winner = create_owner(ddb_api, login_id="kitwinner", phone_number="010-9040-0001")
    repo = ddb_api.repository
    issue_kit(repo)
    original = repo.client.transact_write_items
    after_winner = None

    def racing(**kwargs):
        nonlocal after_winner
        monkeypatch.setattr(repo.client, "transact_write_items", original)
        claim(repo, winner)
        after_winner = core_snapshot(repo)
        return original(**kwargs)

    monkeypatch.setattr(repo.client, "transact_write_items", racing)
    with pytest.raises(KitError) as error:
        claim(repo, first)
    assert error.value.code == "KIT_ALREADY_CLAIMED"
    assert core_snapshot(repo) == after_winner
    assert repo.get_household_consistent(first["user"]["household_id"]).device_kit_status == "unregistered"
    assert repo.get_household_consistent(winner["user"]["household_id"]).kit_id == KIT_ID


def test_moto_two_kits_one_house_only_one_claim_commits(ddb_api, monkeypatch):
    from hearo_backend.device_kits import KitError

    owner = create_owner(ddb_api)
    repo = ddb_api.repository
    issue_kit(repo)
    issue_kit(repo, "HEARO-KIT-0002", suffix="0002")
    original = repo.client.transact_write_items
    after_winner = None

    def racing(**kwargs):
        nonlocal after_winner
        monkeypatch.setattr(repo.client, "transact_write_items", original)
        claim(repo, owner, "HEARO-KIT-0002")
        after_winner = core_snapshot(repo)
        return original(**kwargs)

    monkeypatch.setattr(repo.client, "transact_write_items", racing)
    with pytest.raises(KitError) as error:
        claim(repo, owner)
    assert error.value.code == "HOUSEHOLD_ALREADY_HAS_KIT"
    assert core_snapshot(repo) == after_winner
    assert repo.get_household_consistent(owner["user"]["household_id"]).kit_id == "HEARO-KIT-0002"
    assert repo.core.get_item(Key={"pk": f"KIT#{KIT_ID}", "sk": "META"})["Item"]["status"] == "unclaimed"


def test_moto_rebound_hardware_rejects_claim_without_partial_updates(ddb_api):
    from hearo_backend.device_kits import KitError

    owner = create_owner(ddb_api)
    repo = ddb_api.repository
    issue_kit(repo)
    repo.core.update_item(Key={"pk": f"HARDWARE#{kit_devices()[0]['hardware_id']}", "sk": "KIT"},
                          UpdateExpression="SET kit_id=:other", ExpressionAttributeValues={":other": "HEARO-KIT-OTHER"})
    before = core_snapshot(repo)
    with pytest.raises(KitError) as error:
        claim(repo, owner)
    assert error.value.code == "KIT_CONFIGURATION_INVALID"
    assert core_snapshot(repo) == before


@pytest.mark.parametrize("kind", ["profile", "device", "hardware"])
def test_moto_changed_row_before_transaction_cancels_all_binding_updates(ddb_api, monkeypatch, kind):
    from hearo_backend.device_kits import KitError

    owner = create_owner(ddb_api)
    repo = ddb_api.repository
    issue_kit(repo)
    home = owner["user"]["household_id"]
    original = repo.client.transact_write_items
    after_race = None

    def racing(**kwargs):
        nonlocal after_race
        if kind == "profile":
            key = {"pk": f"USER#{owner['user']['user_id']}", "sk": "PROFILE"}
            expression, values = "ADD token_version :one", {":one": 1}
        elif kind == "device":
            key = {"pk": f"HOUSE#{home}", "sk": "DEVICE#esp32_1"}
            expression, values = "SET kit_id=:other", {":other": "HEARO-KIT-OTHER"}
        else:
            key = {"pk": f"HARDWARE#{kit_devices()[0]['hardware_id']}", "sk": "KIT"}
            expression, values = "SET claimed_household_id=:other", {":other": "home-unrelated"}
        repo.core.update_item(Key=key, UpdateExpression=expression, ExpressionAttributeValues=values)
        after_race = core_snapshot(repo)
        return original(**kwargs)

    monkeypatch.setattr(repo.client, "transact_write_items", racing)
    with pytest.raises(KitError) as error:
        claim(repo, owner)
    expected = {
        "profile": (401, "REVOKED_ACCESS_TOKEN"),
        "device": (503, "KIT_SERVICE_UNAVAILABLE"),
        "hardware": (409, "KIT_CONFIGURATION_INVALID"),
    }
    assert (error.value.status_code, error.value.code) == expected[kind]
    assert core_snapshot(repo) == after_race


def test_moto_transport_failure_is_503_one_attempt_and_no_partial_binding(ddb_api, monkeypatch):
    from hearo_backend.device_kits import KitError

    owner = create_owner(ddb_api)
    repo = ddb_api.repository
    issue_kit(repo)
    before = core_snapshot(repo)
    calls = []

    def uncertain(**kwargs):
        calls.append(kwargs)
        raise ReadTimeoutError(endpoint_url="https://offline.invalid/PRIVATE-TRANSPORT")

    monkeypatch.setattr(repo.client, "transact_write_items", uncertain)
    with pytest.raises(KitError) as error:
        claim(repo, owner)
    assert error.value.status_code == 503 and error.value.code == "KIT_SERVICE_UNAVAILABLE"
    assert "PRIVATE-TRANSPORT" not in str(error.value)
    assert len(calls) == 1
    assert core_snapshot(repo) == before


@pytest.mark.parametrize("aws_code,cancellation_code", [
    ("ThrottlingException", None),
    ("TransactionCanceledException", "ThrottlingError"),
    ("TransactionCanceledException", "ProvisionedThroughputExceeded"),
])
def test_moto_capacity_failures_are_503_not_conditional_409(
    ddb_api, monkeypatch, aws_code, cancellation_code,
):
    from hearo_backend.device_kits import KitError

    owner = create_owner(ddb_api)
    repo = ddb_api.repository
    issue_kit(repo)
    before = core_snapshot(repo)
    calls = []
    response = {"Error": {"Code": aws_code, "Message": "PRIVATE-SDK-ERROR"}}
    if cancellation_code:
        response["CancellationReasons"] = [{"Code": cancellation_code}, {"Code": "None"}]

    def unavailable(**kwargs):
        calls.append(kwargs)
        raise ClientError(response, "TransactWriteItems")

    monkeypatch.setattr(repo.client, "transact_write_items", unavailable)
    with pytest.raises(KitError) as error:
        claim(repo, owner)
    assert (error.value.status_code, error.value.code) == (503, "KIT_SERVICE_UNAVAILABLE")
    assert "PRIVATE-SDK-ERROR" not in str(error.value)
    assert len(calls) == 1 and core_snapshot(repo) == before


def test_moto_committed_but_lost_response_reconciles_without_second_write(ddb_api, monkeypatch):
    owner = create_owner(ddb_api)
    repo = ddb_api.repository
    issue_kit(repo)
    calls = []
    original = repo.client.transact_write_items

    def committed_then_disconnected(**kwargs):
        calls.append(kwargs)
        original(**kwargs)
        raise ReadTimeoutError(endpoint_url="https://offline.invalid/PRIVATE-LOST-RESPONSE")

    monkeypatch.setattr(repo.client, "transact_write_items", committed_then_disconnected)
    value = claim(repo, owner)
    assert value["status"] == "claimed" and value["kit_id"] == KIT_ID
    assert len(calls) == 1
    state = repo.get_device_kit_state(repo.get_user_consistent(owner["user"]["user_id"]), repo.settings)
    assert state == value


def test_moto_duplicate_global_hardware_cannot_issue_partial_second_kit(ddb_api):
    from hearo_backend.device_kits import KitError

    owner = create_owner(ddb_api)
    repo = ddb_api.repository
    issue_kit(repo)
    before = core_snapshot(repo)
    with pytest.raises(KitError) as error:
        issue_kit(repo, "HEARO-KIT-0002", suffix="0001")
    assert error.value.status_code == 409 and error.value.code == "KIT_INVENTORY_CONFLICT"
    assert core_snapshot(repo) == before
    assert ddb_api.client.get(kit_url(owner), headers=auth_header(owner)).json()["status"] == "unregistered"


def test_moto_legacy_absent_marker_does_not_become_unregistered(ddb_api):
    owner = create_owner(ddb_api)
    repo = ddb_api.repository
    home = owner["user"]["household_id"]
    repo.core.update_item(Key={"pk": f"HOUSE#{home}", "sk": "META"},
                          UpdateExpression="REMOVE device_kit_status, kit_id, kit_claimed_at")
    before = core_snapshot(repo)
    response = ddb_api.client.get(kit_url(owner), headers=auth_header(owner))
    assert response.status_code == 200
    assert response.json()["status"] == "legacy_registered"
    assert response.json()["can_claim"] is False
    assert core_snapshot(repo) == before


def test_moto_claimed_binding_survives_member_and_owner_deletion_with_alert_ttl(ddb_api):
    owner = create_owner(ddb_api)
    member = linked_member(ddb_api, owner)
    peer = linked_member(ddb_api, owner, 2)
    repo = ddb_api.repository
    home = owner["user"]["household_id"]
    issue_kit(repo)
    claim(repo, owner)
    inventory = {key: value for key, value in core_snapshot(repo).items()
                 if key[0].startswith(("KIT#", "HARDWARE#"))}
    alert = Alert(home, "kit-retained", iso_utc(datetime.now(UTC) - timedelta(days=1)),
                  "esp32_1", "안방", "아기울음소리", "Noise")
    assert repo.put_alert(alert) is True
    alerts_before = copy.deepcopy(all_rows(repo.alerts))
    alias_before = repo.core.get_item(Key={"pk": f"ALERTID#{home}#kit-retained", "sk": "ALERT"})["Item"]
    assert ddb_api.client.request("DELETE", "/me", headers=auth_header(member),
                                  json={"current_password": "MemberPassword123"}).status_code == 204
    assert all(core_snapshot(repo)[key] == value for key, value in inventory.items())
    response = ddb_api.client.request("DELETE", "/me", headers=auth_header(owner),
                                      json={"current_password": "StrongPassword123"})
    assert response.status_code == 204, response.text
    after = core_snapshot(repo)
    assert all(after[key] == value for key, value in inventory.items())
    assert not any(key[0] == f"HOUSE#{home}" for key in after)
    assert repo.get_user_consistent(peer["user"]["user_id"]).household_link_status == "unlinked"
    assert all_rows(repo.alerts) == alerts_before
    assert after[(f"ALERTID#{home}#kit-retained", "ALERT")] == alias_before
    assert alias_before["expires_at_epoch"] == alerts_before[0]["expires_at_epoch"]
    assert all("owner_user_id" not in row and "claimed_by_user_id" not in row for row in inventory.values())


def test_moto_owner_unlink_wins_without_late_claim_resurrecting_registration(ddb_api, monkeypatch):
    from hearo_backend.device_kits import KitError

    owner = create_owner(ddb_api)
    repo = ddb_api.repository
    issue_kit(repo)
    original = repo.client.transact_write_items
    after_unlink = None

    def racing(**kwargs):
        nonlocal after_unlink
        monkeypatch.setattr(repo.client, "transact_write_items", original)
        repo.unlink_user(owner["user"]["user_id"], datetime.now(UTC))
        after_unlink = core_snapshot(repo)
        return original(**kwargs)

    monkeypatch.setattr(repo.client, "transact_write_items", racing)
    with pytest.raises(KitError):
        claim(repo, owner)
    assert core_snapshot(repo) == after_unlink
    assert repo.core.get_item(Key={"pk": f"KIT#{KIT_ID}", "sk": "META"})["Item"]["status"] == "unclaimed"


def test_moto_stale_route_snapshot_cannot_read_or_claim_relinked_house(ddb_api, monkeypatch):
    from hearo_backend.device_kits import KitError

    owner = create_owner(ddb_api)
    other = create_owner(ddb_api, login_id="kitrelocated", phone_number="010-9049-0001")
    repo = ddb_api.repository
    issue_kit(repo)
    supplied_user = repo.get_user_consistent(owner["user"]["user_id"])
    other_home = other["user"]["household_id"]
    repo.core.update_item(
        Key={"pk": f"USER#{supplied_user.user_id}", "sk": "PROFILE"},
        UpdateExpression="SET household_id=:other",
        ExpressionAttributeValues={":other": other_home},
    )
    before = core_snapshot(repo)
    reads = []
    original = repo.core.get_item

    def observed(**kwargs):
        reads.append(kwargs["Key"])
        return original(**kwargs)

    monkeypatch.setattr(repo.core, "get_item", observed)
    for method in (repo.get_device_kit_state, repo.preview_device_kit, repo.claim_device_kit):
        arguments = () if method.__name__ == "get_device_kit_state" else (KIT_ID, CLAIM_CODE)
        with pytest.raises(KitError) as error:
            method(supplied_user, repo.settings, *arguments)
        assert (error.value.status_code, error.value.code) == (409, "KIT_CLAIM_CONFLICT")
    assert all(key["pk"] != f"HOUSE#{other_home}" for key in reads)
    assert core_snapshot(repo) == before
