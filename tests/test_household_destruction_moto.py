"""Optional local DynamoDB parser checks; never evidence of live AWS behavior.

Moto is optional, so the production dependency set is unchanged. All clients
are created inside mock_aws with fake credentials and metadata lookup disabled.
"""
from __future__ import annotations

import copy
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from botocore.exceptions import ClientError

moto = pytest.importorskip("moto")

from hearo_backend.config import Settings
from hearo_backend.domain import Alert, Device, Household, User, iso_utc
from hearo_backend.store import ConflictError, DynamoRepository, NotFoundError


HOME = "home-destruction-local"


@pytest.fixture
def repo(monkeypatch):
    for key, value in {
        "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing", "AWS_EC2_METADATA_DISABLED": "true",
    }.items():
        monkeypatch.setenv(key, value)
    with moto.mock_aws():
        resource = boto3.resource("dynamodb", region_name="ap-south-1")
        for name, partition, sort in (
            ("destruction-core", "pk", "sk"),
            ("destruction-alerts", "household_id", "event_key"),
        ):
            resource.create_table(
                TableName=name, BillingMode="PAY_PER_REQUEST",
                KeySchema=[{"AttributeName": partition, "KeyType": "HASH"},
                           {"AttributeName": sort, "KeyType": "RANGE"}],
                AttributeDefinitions=[{"AttributeName": partition, "AttributeType": "S"},
                                      {"AttributeName": sort, "AttributeType": "S"}],
            )
        value = DynamoRepository(Settings(
            environment="test", store_backend="dynamodb", mqtt_enabled=False,
            region="ap-south-1", core_table="destruction-core",
            alerts_table="destruction-alerts",
        ))
        consent_time = iso_utc()
        owner = User(
            "owner-local", "owner-local", "합성 보호자", "+821090000001", "owner-hash",
            "household_owner", household_id=HOME, role="owner",
            household_link_status="linked", linked_at=consent_time,
            terms_service_agreed=True, privacy_agreed=True,
            consented_at=consent_time,
            age_over_14_agreed=True, age_over_14_agreed_at=consent_time,
        )
        house = Household(
            HOME, "합성 가구", owner.user_id, invite_hash="invite-local",
            invite_nonce="nonce-local", invite_expires_at="2999-01-01T00:00:00Z",
        )
        devices = [
            Device(HOME, f"esp32_{index}", "합성 위치", "alert_node", credential_hash=f"credential-{index}")
            for index in range(1, 4)
        ] + [Device(HOME, "rpi-001", "거실", "hub", credential_hash="credential-rpi")]
        value.create_owner(owner, house, devices)
        member = User(
            "member-local", "member-local", "합성 가족", "+821090000002", "member-hash", "family_member",
            terms_service_agreed=True, privacy_agreed=True, consented_at=consent_time,
            age_over_14_agreed=True, age_over_14_agreed_at=consent_time,
        )
        value.create_unlinked_user(member)
        value.link_member(member.user_id, "invite-local", datetime.now(UTC))
        for account in (owner, member):
            value.save_refresh_token(f"token-{account.user_id}", account.user_id, "2999-01-01T00:00:00Z")
        value.set_display_name(HOME, member.user_id, member.user_id, "보존 여부 시험")
        value.create_contact(HOME, {"contact_id": "contact-local", "name": "합성 연락처", "phone_number": "+821090000003"})
        retained = Alert(HOME, "retained-local", iso_utc(datetime.now(UTC) - timedelta(days=1)),
                         "esp32_1", "안방", "아기울음소리", "Noise")
        assert value.put_alert(retained) is True
        value.core.put_item(Item={"pk": "HOUSE#other-local", "sk": "META", "sentinel": "unchanged"})
        yield value


def rows(table):
    items = []
    kwargs = {"ConsistentRead": True}
    while True:
        response = table.scan(**kwargs)
        items.extend(response["Items"])
        if not response.get("LastEvaluatedKey"):
            return items
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def withdraw(repo, role="owner"):
    return repo.withdraw_user_account(f"{role}-local", datetime.now(UTC), expected_password_hash=f"{role}-hash")


def assert_registration_gone(repo):
    assert not any(row["pk"] == f"HOUSE#{HOME}" for row in rows(repo.core))
    assert not any(row.get("household_id") == HOME and row["pk"].startswith(("DEVICECRED#", "INVITE#"))
                   for row in rows(repo.core))


def test_moto_owner_removes_registration_preserves_alerts_and_survivor(repo):
    before_alerts = copy.deepcopy(rows(repo.alerts))
    before_alias = repo.core.get_item(Key={"pk": f"ALERTID#{HOME}#retained-local", "sk": "ALERT"})["Item"]
    assert withdraw(repo) == {"previous_household_id": HOME, "household_status": "inactive"}
    assert_registration_gone(repo)
    assert repo.get_user_consistent("owner-local") is None
    assert repo.get_user_consistent("member-local").household_link_status == "unlinked"
    assert rows(repo.alerts) == before_alerts
    assert repo.core.get_item(Key={"pk": f"ALERTID#{HOME}#retained-local", "sk": "ALERT"})["Item"] == before_alias
    assert repo.core.get_item(Key={"pk": "HOUSE#other-local", "sk": "META"})["Item"]["sentinel"] == "unchanged"
    assert repo.get_device_by_credential("credential-1") is None


def test_moto_member_keeps_shared_registration_and_owner(repo):
    before = { (row["pk"], row["sk"]): row for row in rows(repo.core)
              if row["sk"].startswith(("DEVICE#", "CONTACT#")) or row["pk"].startswith("DEVICECRED#") }
    assert withdraw(repo, "member")["household_status"] == "active"
    after = {(row["pk"], row["sk"]): row for row in rows(repo.core)}
    assert all(after[key] == row for key, row in before.items())
    assert repo.get_user_consistent("owner-local").household_link_status == "linked"
    assert not any(row["pk"] == "USER#member-local" for row in after.values())
    assert ("LOGINID#member-local", "USER") not in after
    assert ("PHONE#+821090000002", "USER") not in after
    assert ("TOKEN#token-member-local", "REFRESH") not in after
    assert (f"HOUSE#{HOME}", "MEMBER#member-local") not in after


def test_moto_owner_unlink_preserves_account_then_withdraws(repo):
    assert repo.unlink_user("owner-local", datetime.now(UTC))["household_status"] == "inactive"
    assert_registration_gone(repo)
    assert repo.get_user_consistent("owner-local").household_link_status == "unlinked"
    assert repo.core.get_item(Key={"pk": "TOKEN#token-owner-local", "sk": "REFRESH"}).get("Item")
    assert withdraw(repo) == {"previous_household_id": None, "household_status": None}
    assert repo.get_user_consistent("owner-local") is None


def test_moto_legacy_inactive_owned_house_is_removed_on_later_withdrawal(repo):
    for role in ("owner", "member"):
        key = {"pk": f"USER#{role}-local", "sk": "PROFILE"}
        repo.core.update_item(Key=key, UpdateExpression="SET household_link_status=:unlinked REMOVE household_id, #role, linked_at",
                              ExpressionAttributeNames={"#role": "role"}, ExpressionAttributeValues={":unlinked": "unlinked"})
        repo.core.delete_item(Key={"pk": f"HOUSE#{HOME}", "sk": f"MEMBER#{role}-local"})
    repo.core.update_item(Key={"pk": f"HOUSE#{HOME}", "sk": "META"},
                          UpdateExpression="SET #status=:inactive REMOVE registration_version",
                          ExpressionAttributeNames={"#status": "status"}, ExpressionAttributeValues={":inactive": "inactive"})
    assert withdraw(repo)["previous_household_id"] == HOME
    assert_registration_gone(repo)


@pytest.mark.parametrize("race", ["contact", "alias", "credential", "refresh"])
def test_moto_concurrent_reference_write_cancels_entire_destruction(repo, race):
    original = repo.client.transact_write_items
    after_race = None

    def racing_transaction(**kwargs):
        nonlocal after_race
        repo.client.transact_write_items = original
        try:
            if race == "contact":
                repo.create_contact(HOME, {"contact_id": "new-contact", "name": "새 연락처", "phone_number": "+821090000004"})
            elif race == "alias":
                repo.set_display_name(HOME, "member-local", "member-local", "경합 별명")
            elif race == "credential":
                repo.rotate_device_credential(HOME, "esp32_1", "credential-new")
            else:
                repo.save_refresh_token("survivor-racing-token", "member-local", "2999-01-01T00:00:00Z")
            after_race = copy.deepcopy(rows(repo.core))
            return original(**kwargs)
        finally:
            repo.client.transact_write_items = racing_transaction

    repo.client.transact_write_items = racing_transaction
    with pytest.raises(ConflictError) as error:
        withdraw(repo)
    assert error.value.code == "ACCOUNT_DELETION_CONFLICT"
    assert rows(repo.core) == after_race
    assert repo.get_user_consistent("owner-local") is not None


@pytest.mark.parametrize("writer", ["contact", "alias", "credential", "alert"])
def test_moto_stale_writers_cannot_recreate_data_after_destruction(repo, writer):
    withdraw(repo)
    before = copy.deepcopy(rows(repo.core))
    if writer == "alert":
        value = Alert(HOME, "late-alert", iso_utc(), "esp32_1", "안방", "노크소리", "Visitor")
        assert repo.put_alert(value) is False
    else:
        with pytest.raises((NotFoundError, ClientError)):
            if writer == "contact":
                repo.create_contact(HOME, {"contact_id": "late-contact", "name": "늦은 연락처"})
            elif writer == "alias":
                repo.set_display_name(HOME, "member-local", "member-local", "늦은 별명")
            else:
                repo.rotate_device_credential(HOME, "esp32_1", "credential-late")
    assert rows(repo.core) == before
    assert_registration_gone(repo)


def test_moto_unrecognized_child_fails_before_any_transaction(repo):
    repo.core.put_item(Item={"pk": f"HOUSE#{HOME}", "sk": "UNKNOWN#future", "private": "synthetic"})
    before = copy.deepcopy(rows(repo.core))
    with pytest.raises(ConflictError) as error:
        withdraw(repo)
    assert error.value.code == "ACCOUNT_DELETION_REVIEW_REQUIRED"
    assert rows(repo.core) == before


def test_moto_malformed_legacy_owned_house_requires_review_without_writes(repo):
    repo.core.put_item(Item={
        "pk": "HOUSE#legacy-malformed", "sk": "META",
        "household_id": "legacy-malformed", "owner_user_id": "owner-local",
        "status": "inactive",  # Required name is intentionally absent.
    })
    before = copy.deepcopy(rows(repo.core))
    with pytest.raises(ConflictError) as error:
        withdraw(repo)
    assert error.value.code == "ACCOUNT_DELETION_REVIEW_REQUIRED"
    assert rows(repo.core) == before


@pytest.mark.parametrize("role", ["owner", "member"])
def test_moto_profile_identifier_mismatch_requires_review(repo, role):
    repo.core.update_item(
        Key={"pk": f"USER#{role}-local", "sk": "PROFILE"},
        UpdateExpression="SET user_id=:wrong",
        ExpressionAttributeValues={":wrong": "unrelated-user-local"},
    )
    before = copy.deepcopy(rows(repo.core))
    with pytest.raises(ConflictError) as error:
        withdraw(repo)
    assert error.value.code == "ACCOUNT_DELETION_REVIEW_REQUIRED"
    assert rows(repo.core) == before


def test_moto_rebound_current_invite_prevents_destruction_and_rotation(repo):
    repo.core.update_item(
        Key={"pk": "INVITE#invite-local", "sk": "INVITE"},
        UpdateExpression="SET household_id=:other",
        ExpressionAttributeValues={":other": "other-local"},
    )
    before = copy.deepcopy(rows(repo.core))
    with pytest.raises(ConflictError) as error:
        withdraw(repo)
    assert error.value.code == "ACCOUNT_DELETION_REVIEW_REQUIRED"
    with pytest.raises(ClientError):
        repo.rotate_invite(HOME, "invite-new", "nonce-new", "2999-01-01T00:00:00Z")
    assert rows(repo.core) == before


def test_moto_expired_invite_alias_already_removed_can_rotate(repo):
    repo.core.delete_item(Key={"pk": "INVITE#invite-local", "sk": "INVITE"})
    before_version = repo.get_household_consistent(HOME).registration_version
    repo.rotate_invite(HOME, "invite-new", "nonce-new", "2999-01-01T00:00:00Z")
    assert repo.get_household_consistent(HOME).registration_version == before_version + 1
    assert repo.get_invite("invite-new")["household_id"] == HOME


@pytest.mark.parametrize("unlink_first", [False, True])
def test_moto_multiple_inactive_owned_houses_removed_in_one_plan(repo, unlink_first):
    if unlink_first:
        repo.unlink_user("owner-local", datetime.now(UTC))
    historical = ["legacy-owned-one", "legacy-owned-two"]
    for household_id in historical:
        house = Household(household_id, "과거 합성 가구", "owner-local", status="inactive")
        device = Device(household_id, "esp32_1", "과거 위치", "alert_node", credential_hash=f"cred-{household_id}")
        repo.core.put_item(Item={"pk": f"HOUSE#{household_id}", "sk": "META", **asdict(house)})
        repo.core.put_item(Item={"pk": f"HOUSE#{household_id}", "sk": "DEVICE#esp32_1", **asdict(device)})
        repo.core.put_item(Item={"pk": f"DEVICECRED#cred-{household_id}", "sk": "DEVICE",
                                 "household_id": household_id, "device_id": "esp32_1"})
    before_alerts = copy.deepcopy(rows(repo.alerts))
    withdraw(repo)
    for household_id in [HOME, *historical]:
        assert not any(row["pk"] == f"HOUSE#{household_id}" for row in rows(repo.core))
        assert not any(row.get("household_id") == household_id and row["pk"].startswith("DEVICECRED#")
                       for row in rows(repo.core))
    assert repo.get_user_consistent("member-local").household_link_status == "unlinked"
    assert rows(repo.alerts) == before_alerts


def test_moto_backed_public_withdrawal_and_device_auth_revocation(repo):
    from fastapi.testclient import TestClient
    from hearo_backend.integrations import NullMqttPublisher
    from hearo_backend.main import create_app
    from hearo_backend.security import hash_password, hash_secret

    password = "SyntheticTestPassword123"
    repo.update_user_password("owner-local", hash_password(password))
    credential = "synthetic-device-credential-only-for-local-test"
    repo.rotate_device_credential(HOME, "esp32_1", hash_secret(credential))
    app = create_app(repo.settings, repo, NullMqttPublisher())
    owner = repo.get_user_consistent("owner-local")
    member = repo.get_user_consistent("member-local")
    headers = {"Authorization": f"Bearer {app.state.tokens.access(owner.user_id, owner.token_version)}"}
    survivor_headers = {"Authorization": f"Bearer {app.state.tokens.access(member.user_id, member.token_version)}"}
    with TestClient(app) as client:
        assert client.get("/device/v1/config", headers={"X-Device-Credential": credential}).status_code == 200
        response = client.request("DELETE", "/me", headers=headers, json={"current_password": password})
        assert response.status_code == 204, response.text
        assert client.get("/me", headers=headers).status_code == 401
        assert client.get("/device/v1/config", headers={"X-Device-Credential": credential}).status_code == 401
        survivor = client.get("/me", headers=survivor_headers)
        assert survivor.status_code == 200
        assert survivor.json()["household_link_status"] == "unlinked"
        assert client.get(f"/households/{HOME}/alarms", headers=survivor_headers).status_code == 409
    assert len(rows(repo.alerts)) == 1
    assert_registration_gone(repo)
