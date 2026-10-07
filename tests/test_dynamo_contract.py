"""DynamoDB operation-shape checks that do not require an AWS account.

The API behavior tests use the in-memory repository. These tests cover the
most important production-only transaction shapes so a refactor cannot omit
an identity alias, invite record, device credential, or address cleanup.
"""

from dataclasses import asdict
from datetime import UTC, datetime
from types import SimpleNamespace

from boto3.dynamodb.types import TypeDeserializer

from hearo_backend.domain import Device, EmergencyAddress, Household, User
from hearo_backend.store import DynamoRepository


class RecordingClient:
    def __init__(self):
        self.transactions: list[list[dict]] = []

    def transact_write_items(self, *, TransactItems):
        self.transactions.append(TransactItems)


class ConditionalCheckFailedException(Exception):
    pass


class RecordingCoreTable:
    def __init__(self):
        self.updates: list[dict] = []

    def update_item(self, **kwargs):
        self.updates.append(kwargs)
        values = kwargs["ExpressionAttributeValues"]
        value = values.get(":baseline", values.get(":seen"))
        return {"Attributes": {"alarms_last_seen_at": value}}

    def get_item(self, *, Key):
        return {"Item": {**Key, "alarms_last_seen_at": "2026-08-30T01:00:00Z"}}


class AccountDeletionTable:
    def __init__(self, user: User):
        self.user = user
        self.deleted_keys: list[dict] = []
        self.scan_calls = 0

    def get_item(self, *, Key, **kwargs):
        if Key == {"pk": f"USER#{self.user.user_id}", "sk": "PROFILE"}:
            return {"Item": asdict(self.user)}
        return {}

    def scan(self, **_):
        self.scan_calls += 1
        return {
            "Items": [
                {"pk": "TOKEN#refresh-hash", "sk": "REFRESH"},
                {"pk": "HOUSE#home-old", "sk": "ALIAS#member-1#owner-1"},
            ]
        }

    def delete_item(self, *, Key):
        self.deleted_keys.append(Key)

    def query(self, **kwargs):
        assert kwargs["ConsistentRead"] is True
        return {"Items": []}


def repository() -> DynamoRepository:
    value = DynamoRepository.__new__(DynamoRepository)
    value.settings = SimpleNamespace(
        core_table="hearo-core-final", alerts_table="hearo-alerts-final"
    )
    value.client = RecordingClient()
    return value


def decoded_item(operation: dict) -> dict:
    raw = operation["Put"]["Item"]
    deserializer = TypeDeserializer()
    return {key: deserializer.deserialize(value) for key, value in raw.items()}


def decoded_key(operation: dict) -> dict:
    raw = operation["Delete"]["Key"]
    deserializer = TypeDeserializer()
    return {key: deserializer.deserialize(value) for key, value in raw.items()}


def test_create_owner_transaction_contains_all_final_identity_and_device_records():
    repo = repository()
    user = User(
        user_id="owner-1",
        login_id="owner01",
        name="보호자",
        phone_number="+821012345678",
        password_hash="argon2-hash",
        account_type="household_owner",
        household_id="home-1",
        role="owner",
        household_link_status="linked",
    )
    household = Household(
        household_id="home-1",
        name="테스트 가구",
        owner_user_id=user.user_id,
        emergency_address=EmergencyAddress(
            postal_code="41566",
            road_address="대구광역시 북구 대학로 80",
            detail_address="101동",
            address_provider="kakao_postcode",
        ),
        invite_hash="invite-hash",
        invite_nonce="invite-nonce",
        invite_expires_at="2026-08-22T00:00:00Z",
    )
    devices = [
        Device(
            household_id="home-1",
            device_id=device_id,
            location=location,
            device_type=device_type,
            credential_hash=f"credential-{device_id}",
        )
        for device_id, location, device_type in (
            ("rpi-001", "거실", "hub"),
            ("esp32_1", "안방", "alert_node"),
            ("esp32_2", "현관", "alert_node"),
            ("esp32_3", "화장실", "alert_node"),
        )
    ]

    repo.create_owner(user, household, devices)

    operations = repo.client.transactions[0]
    items = [decoded_item(operation) for operation in operations]
    keys = {(item["pk"], item["sk"]) for item in items}
    assert ("LOGINID#owner01", "USER") in keys
    assert ("PHONE#+821012345678", "USER") in keys
    assert ("USER#owner-1", "PROFILE") in keys
    assert ("HOUSE#home-1", "META") in keys
    assert ("INVITE#invite-hash", "INVITE") in keys
    assert len([key for key in keys if key[1].startswith("DEVICE#")]) == 4
    assert len([key for key in keys if key[0].startswith("DEVICECRED#")]) == 4
    household_item = next(item for item in items if item["sk"] == "META")
    assert household_item["emergency_address"]["postal_code"] == "41566"
    owner_membership = next(item for item in items if item["sk"] == "MEMBER#owner-1")
    assert owner_membership["alarms_last_seen_at"] == user.created_at
    assert "email" not in str(items).casefold()


def test_dynamo_alarm_seen_updates_are_membership_scoped_and_monotonic():
    repo = DynamoRepository.__new__(DynamoRepository)
    repo.core = RecordingCoreTable()
    repo.client = SimpleNamespace(
        exceptions=SimpleNamespace(
            ConditionalCheckFailedException=ConditionalCheckFailedException
        )
    )

    baseline = repo.get_or_initialize_alarm_last_seen(
        "home-1",
        "owner-1",
        "2026-08-30T00:00:00Z",
    )
    assert baseline == "2026-08-30T00:00:00Z"
    initialize = repo.core.updates[0]
    assert initialize["Key"] == {
        "pk": "HOUSE#home-1",
        "sk": "MEMBER#owner-1",
    }
    assert "if_not_exists" in initialize["UpdateExpression"]
    assert initialize["ConditionExpression"] == "attribute_exists(pk)"

    seen = repo.mark_alarms_seen(
        "home-1",
        "owner-1",
        "2026-08-30T02:00:00Z",
    )
    assert seen == "2026-08-30T02:00:00Z"
    update = repo.core.updates[1]
    assert update["Key"] == initialize["Key"]
    assert "alarms_last_seen_at <= :seen" in update["ConditionExpression"]


def test_link_member_transaction_initializes_alarm_seen_at_to_link_time():
    repo = repository()
    user = User(
        user_id="member-1",
        login_id="member01",
        name="가족",
        phone_number="+821087654321",
        password_hash="hash",
        account_type="family_member",
    )
    linked_at = datetime(2026, 8, 30, 3, 0, tzinfo=UTC)
    repo.get_user = lambda user_id: user
    repo.get_invite = lambda invite_hash: {
        "household_id": "home-1",
        "expires_at": "2026-08-31T00:00:00Z",
    }

    repo.link_member(user.user_id, "invite-hash", linked_at)

    operations = repo.client.transactions[0]
    membership = next(
        decoded_item(operation)
        for operation in operations
        if operation.get("Put")
        and operation["Put"]["Item"].get("sk") == {"S": "MEMBER#member-1"}
    )
    assert membership["linked_at"] == "2026-08-30T03:00:00Z"
    assert membership["alarms_last_seen_at"] == membership["linked_at"]


def test_owner_unlink_transaction_removes_address_invite_and_all_members():
    repo = repository()
    owner = User(
        user_id="owner-1",
        login_id="owner01",
        name="보호자",
        phone_number="+821012345678",
        password_hash="hash",
        account_type="household_owner",
        household_id="home-1",
        role="owner",
        household_link_status="linked",
    )
    member = User(
        user_id="member-1",
        login_id="member01",
        name="가족",
        phone_number="+821087654321",
        password_hash="hash",
        account_type="family_member",
        household_id="home-1",
        role="member",
        household_link_status="linked",
    )
    household = Household(
        household_id="home-1",
        name="가구",
        owner_user_id=owner.user_id,
        emergency_address=EmergencyAddress(
            postal_code="41566",
            road_address="대구광역시 북구 대학로 80",
            detail_address="",
            address_provider="kakao_postcode",
        ),
        invite_hash="invite-hash",
    )
    repo.get_user_consistent = lambda user_id: owner if user_id == owner.user_id else member
    repo.get_household_consistent = lambda household_id: household
    repo._consistent_household_members = lambda household_id: [
        ({"linked_at": value.linked_at}, value) for value in (owner, member)
    ]

    result = repo.unlink_user(owner.user_id, datetime.now(UTC))

    assert result == {
        "household_link_status": "unlinked",
        "household_status": "inactive",
    }
    operations = repo.client.transactions[0]
    household_update = operations[0]["Update"]
    assert "REMOVE emergency_address" in household_update["UpdateExpression"]
    assert any(
        operation.get("Delete", {}).get("Key", {}).get("pk") == {"S": "INVITE#invite-hash"}
        for operation in operations
    )
    member_deletes = [
        operation
        for operation in operations
        if operation.get("Delete", {}).get("Key", {}).get("sk", {}).get("S", "").startswith("MEMBER#")
    ]
    assert len(member_deletes) == 2


def test_delete_user_account_removes_identity_aliases_profile_and_user_references():
    repo = repository()
    user = User(
        user_id="member-1",
        login_id="member01",
        name="가족",
        phone_number="+821087654321",
        password_hash="hash",
        account_type="family_member",
    )
    table = AccountDeletionTable(user)
    repo.core = table

    repo.delete_user_account(user.user_id)

    assert table.scan_calls == 1
    assert table.deleted_keys == []  # Final cleanup is atomic with identity deletion.
    deleted = [decoded_key(operation) for operation in repo.client.transactions[0]]
    assert deleted == [
        {"pk": "LOGINID#member01", "sk": "USER"},
        {"pk": "PHONE#+821087654321", "sk": "USER"},
        {"pk": "USER#member-1", "sk": "PROFILE"},
        {"pk": "HOUSE#home-old", "sk": "ALIAS#member-1#owner-1"},
        {"pk": "TOKEN#refresh-hash", "sk": "REFRESH"},
    ]


def test_final_user_and_household_deserialization_accepts_stored_maps():
    user = User(
        user_id="member-1",
        login_id="member01",
        name="가족",
        phone_number="+821087654321",
        password_hash="hash",
        account_type="family_member",
    )
    decoded_user = DynamoRepository._user({"pk": "ignored", **asdict(user)})
    assert decoded_user.login_id == "member01"

    household = Household(
        household_id="home-1",
        name="가구",
        owner_user_id="owner-1",
        emergency_address=EmergencyAddress(
            postal_code="41566",
            road_address="대구광역시 북구 대학로 80",
            detail_address="101동",
            address_provider="kakao_postcode",
        ),
    )
    decoded_household = DynamoRepository._household(asdict(household))
    assert decoded_household.emergency_address.postal_code == "41566"
    assert decoded_household.emergency_address.verified is False


def test_legacy_stored_address_without_verified_field_defaults_to_false():
    decoded = DynamoRepository._household(
        {
            "household_id": "home-legacy",
            "name": "기존 가구",
            "owner_user_id": "owner-legacy",
            "emergency_address": {
                "postal_code": "41566",
                "road_address": "대구광역시 북구 대학로 80",
                "detail_address": "101동",
                "address_provider": "kakao_postcode",
                "updated_at": "2026-08-20T00:00:00Z",
            },
        }
    )
    assert decoded.emergency_address is not None
    assert decoded.emergency_address.verified is False


def test_put_alert_transaction_reserves_event_id_across_timestamps(monkeypatch):
    from hearo_backend.domain import Alert

    repo = repository()
    monkeypatch.setattr("hearo_backend.retention.utc_now", lambda: datetime(2026, 8, 22, tzinfo=UTC))
    alert = Alert(
        household_id="home-1",
        event_id="alarm-1",
        timestamp="2026-08-21T00:00:00Z",
        source_device_id="rpi-001",
        location="거실",
        sound="비상벨소리",
        type="Urgent",
    )

    assert repo.put_alert(alert) is True
    operations = repo.client.transactions[0]
    assert operations[0]["Put"]["TableName"] == "hearo-alerts-final"
    alias = decoded_item(operations[1])
    assert alias["pk"] == "ALERTID#home-1#alarm-1"
    assert alias["event_key"] == "2026-08-21T00:00:00Z#alarm-1"
