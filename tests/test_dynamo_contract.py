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
    assert "email" not in str(items).casefold()


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
    repo.get_user = lambda user_id: owner if user_id == owner.user_id else member
    repo.get_household = lambda household_id: household
    repo.list_members = lambda household_id: [owner, member]

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


def test_put_alert_transaction_reserves_event_id_across_timestamps():
    from hearo_backend.domain import Alert

    repo = repository()
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
