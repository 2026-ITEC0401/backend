"""Withdrawal invariants with memory and a local DynamoDB transaction model.

The model evaluates the emitted conditions against changed rows; it is not an
AWS integration test and cannot establish live IAM or DynamoDB compatibility.
"""

from __future__ import annotations

import copy
import re
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError
from starlette.websockets import WebSocketDisconnect

from hearo_backend.domain import Alert, Device, EmergencyAddress, Household, User, iso_utc
from hearo_backend.store import ConflictError, DynamoRepository

from .conftest import auth_header, create_family, create_owner


NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)
HOME = "home-withdrawal"
OTHER_HOME = "home-unrelated"


def retained_alert(household_id: str, event_id: str = "withdrawal-retained") -> Alert:
    return Alert(
        household_id=household_id,
        event_id=event_id,
        timestamp=iso_utc(datetime.now(UTC) - timedelta(days=1)),
        source_device_id="rpi-001",
        location="거실",
        sound="아기울음소리",
        type="Urgent",
        confidence=0.91,
    )


def linked_family(api, owner, index=1):
    family = create_family(
        api, login_id=f"atomicmember{index}", phone_number=f"010-9000-{index:04d}"
    )
    invite = api.client.get(
        f"/households/{owner['user']['household_id']}/invite-code",
        headers=auth_header(owner),
    ).json()
    response = api.client.post(
        "/households/link", headers=auth_header(family),
        json={"invite_code": invite["invite_code"]},
    )
    assert response.status_code == 200
    return family


def memory_snapshot(repository):
    return copy.deepcopy({
        name: value for name, value in vars(repository).items()
        if isinstance(value, dict)
    })


def memory_household_state(repository, household_id):
    device_keys = {
        key for key in repository.devices if key[0] == household_id
    }
    credential_hashes = {
        device.credential_hash
        for key, device in repository.devices.items()
        if key in device_keys and device.credential_hash
    }
    return copy.deepcopy({
        "household": repository.households.get(household_id),
        "members": repository.members.get(household_id),
        "devices": {
            key: value for key, value in repository.devices.items() if key in device_keys
        },
        "credentials": {
            key: value
            for key, value in repository.device_credentials.items()
            if key in credential_hashes
        },
        "contacts": repository.contacts.get(household_id),
        "display_names": {
            key: value
            for key, value in repository.display_names.items()
            if key[0] == household_id
        },
        "last_seen": {
            key: value
            for key, value in repository.alarm_last_seen.items()
            if key[0] == household_id
        },
        "alerts": repository.alerts.get(household_id),
    })


@pytest.mark.parametrize("role", ["owner", "member"])
def test_memory_failed_final_delete_restores_every_household_and_identity_row(
    api, monkeypatch, role,
):
    owner = create_owner(api)
    member = linked_family(api, owner)
    peer = linked_family(api, owner, 2)
    selected = owner if role == "owner" else member
    home = owner["user"]["household_id"]
    user_id = selected["user"]["user_id"]
    api.repository.set_display_name(home, peer["user"]["user_id"], user_id, "별명")
    api.repository.legal_consents[user_id] = [{"sk": "CONSENT#test"}]
    before = memory_snapshot(api.repository)
    visited = []

    def fail_final_delete(deleting_user_id):
        visited.append(deleting_user_id)
        # Failure occurs at the final deletion step, not before any unlink work.
        assert api.repository.get_user(user_id).household_link_status == "unlinked"
        raise ConflictError("injected final deletion failure", code="ACCOUNT_DELETION_CONFLICT")

    monkeypatch.setattr(api.repository, "delete_user_account", fail_final_delete)
    response = api.client.request(
        "DELETE", "/me", headers=auth_header(selected),
        json={"current_password": "StrongPassword123" if role == "owner" else "MemberPassword123"},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "ACCOUNT_DELETION_CONFLICT"
    assert visited == [user_id]
    assert memory_snapshot(api.repository) == before
    assert api.client.get("/me", headers=auth_header(selected)).status_code == 200
    assert api.repository.get_household(home).status == "active"


def test_memory_stale_authenticated_password_cannot_withdraw_linked_account(api):
    owner = create_owner(api)
    member = linked_family(api, owner)
    value = api.repository.get_user(member["user"]["user_id"])
    expected_hash = value.password_hash
    api.repository.update_user_password(value.user_id, "changed-password-hash")
    before = memory_snapshot(api.repository)
    with pytest.raises(ConflictError) as error:
        api.repository.withdraw_user_account(value.user_id, NOW, expected_password_hash=expected_hash)
    assert error.value.code == "ACCOUNT_DELETION_CONFLICT"
    assert memory_snapshot(api.repository) == before


@pytest.mark.parametrize("role", ["owner", "member"])
def test_memory_capacity_review_leaves_linked_household_and_identity_intact(api, role):
    owner = create_owner(api)
    member = linked_family(api, owner)
    selected = owner if role == "owner" else member
    value = api.repository.get_user(selected["user"]["user_id"])
    api.repository.legal_consents[value.user_id] = [
        {"sk": f"CONSENT#synthetic-{index}"} for index in range(101)
    ]
    before = memory_snapshot(api.repository)
    with pytest.raises(ConflictError) as error:
        api.repository.withdraw_user_account(value.user_id, NOW, expected_password_hash=value.password_hash)
    assert error.value.code == "ACCOUNT_DELETION_REVIEW_REQUIRED"
    assert memory_snapshot(api.repository) == before


def test_memory_member_success_revokes_only_deleted_identity_and_preserves_peers(api):
    owner = create_owner(api)
    member = linked_family(api, owner)
    peer = linked_family(api, owner, 2)
    home = owner["user"]["household_id"]
    member_id = member["user"]["user_id"]
    peer_id = peer["user"]["user_id"]
    api.repository.set_display_name(home, peer_id, member_id, "탈퇴 대상")
    api.repository.set_display_name(home, peer_id, peer_id, "남는 별명")
    api.repository.legal_consents[member_id] = [{"sk": "CONSENT#synthetic-member"}]
    household_before = asdict(api.repository.get_household(home))
    peer_before = asdict(api.repository.get_user(peer["user"]["user_id"]))
    peer_seen = api.repository.alarm_last_seen[(home, peer["user"]["user_id"])]
    response = api.client.request(
        "DELETE", "/me", headers=auth_header(member),
        json={"current_password": "MemberPassword123"},
    )
    assert response.status_code == 204
    assert api.client.get("/me", headers=auth_header(member)).status_code == 401
    assert api.client.post("/auth/refresh", json={
        "refresh_token": member["tokens"]["refresh_token"],
    }).status_code == 401
    assert api.client.post("/auth/login", json={
        "login_id": "atomicmember1", "password": "MemberPassword123",
    }).status_code == 401
    assert api.client.get("/me", headers=auth_header(owner)).status_code == 200
    assert api.client.get("/me", headers=auth_header(peer)).status_code == 200
    # Household membership fence may advance, but unrelated household data stays.
    household_after = asdict(api.repository.get_household(home))
    household_before.pop("membership_version", None)
    household_after.pop("membership_version", None)
    assert household_after == household_before
    assert asdict(api.repository.get_user(peer["user"]["user_id"])) == peer_before
    assert api.repository.alarm_last_seen[(home, peer["user"]["user_id"])] == peer_seen
    assert (home, member["user"]["user_id"]) not in api.repository.alarm_last_seen
    assert member_id not in api.repository.users_by_login_id.values()
    assert member_id not in api.repository.users_by_phone.values()
    assert member_id not in api.repository.legal_consents
    assert (home, peer_id, member_id) not in api.repository.display_names
    assert api.repository.display_names[(home, peer_id, peer_id)] == "남는 별명"


def test_memory_owner_success_closes_household_sockets_and_unlinks_survivors(api):
    owner = create_owner(api)
    member = linked_family(api, owner)
    home = owner["user"]["household_id"]
    invite_hash = api.repository.get_household(home).invite_hash
    member_id = member["user"]["user_id"]
    api.repository.set_display_name(home, member_id, member_id, "남은 구성원 별명")
    api.repository.create_contact(home, {
        "contact_id": "delete-with-house",
        "name": "긴급 연락처",
        "relationship": "가족",
        "phone_number": "+821011112222",
        "created_at": iso_utc(),
    })
    alert = retained_alert(home)
    assert api.repository.put_alert(alert) is True
    retained = copy.deepcopy(api.repository.alerts[home])
    target_credentials = {
        device.credential_hash
        for (household_id, _), device in api.repository.devices.items()
        if household_id == home and device.credential_hash
    }

    other = create_owner(
        api,
        login_id="unrelatedowner",
        phone_number="010-3333-4444",
        name="다른 보호자",
    )
    other_home = other["user"]["household_id"]
    other_before = memory_household_state(api.repository, other_home)
    with api.client.websocket_connect(f"/ws/households/{home}") as owner_ws:
        with api.client.websocket_connect(f"/ws/households/{home}") as member_ws:
            for websocket, account in ((owner_ws, owner), (member_ws, member)):
                websocket.send_json({"type": "auth", "access_token": account["tokens"]["access_token"]})
                assert websocket.receive_json()["type"] == "connection.ready"
            response = api.client.request(
                "DELETE", "/me", headers=auth_header(owner),
                json={"current_password": "StrongPassword123"},
            )
            assert response.status_code == 204
            for websocket in (owner_ws, member_ws):
                assert websocket.receive_json() == {"type": "household.inactivated", "household_id": home}
                with pytest.raises(WebSocketDisconnect) as error:
                    websocket.receive_json()
                assert error.value.code == 1008
    assert api.repository.get_household(home) is None
    assert invite_hash not in api.repository.invites
    assert home not in api.repository.members
    assert home not in api.repository.contacts
    assert not any(key[0] == home for key in api.repository.devices)
    assert target_credentials.isdisjoint(api.repository.device_credentials)
    assert not any(key[0] == home for key in api.repository.display_names)
    assert not any(key[0] == home for key in api.repository.alarm_last_seen)
    survivor = api.repository.get_user(member_id)
    assert survivor.household_id is None and survivor.role is None and survivor.linked_at is None
    assert survivor.household_link_status == "unlinked"
    assert api.repository.alerts[home] == retained
    assert alert.expires_at_epoch is not None
    assert not {"phone_number", "road_address", "detail_address"} & asdict(alert).keys()
    assert owner["user"]["phone_number"] not in repr(api.repository.alerts[home])
    assert api.repository.put_alert(retained_alert(home, "after-destroy")) is False
    assert memory_household_state(api.repository, other_home) == other_before
    assert api.client.get("/me", headers=auth_header(owner)).status_code == 401
    assert api.client.get("/me", headers=auth_header(member)).status_code == 200


def test_memory_owner_unlink_destroys_owned_core_graph_but_keeps_account_and_alerts(api):
    owner = create_owner(api)
    member = linked_family(api, owner)
    home = owner["user"]["household_id"]
    member_id = member["user"]["user_id"]
    alert = retained_alert(home, "legacy-inactive-retained")
    assert api.repository.put_alert(alert) is True

    unlinked = api.client.delete(
        "/households/current/link", headers=auth_header(owner),
    )
    assert unlinked.status_code == 200
    assert api.repository.get_household(home) is None
    assert home not in api.repository.members and home not in api.repository.contacts
    assert not any(key[0] == home for key in api.repository.devices)
    assert not any(value[0] == home for value in api.repository.device_credentials.values())
    survivor = api.repository.get_user(member_id)
    assert survivor.household_link_status == "unlinked"
    account = api.repository.get_user(owner["user"]["user_id"])
    assert account is not None and account.household_link_status == "unlinked"
    assert api.repository.alerts[home] == [alert]

    removed = api.client.request(
        "DELETE", "/me", headers=auth_header(owner),
        json={"current_password": "StrongPassword123"},
    )
    assert removed.status_code == 204
    assert api.repository.get_household(home) is None
    assert home not in api.repository.members and home not in api.repository.contacts
    assert not any(key[0] == home for key in api.repository.devices)
    assert not any(value[0] == home for value in api.repository.device_credentials.values())
    assert api.repository.alerts[home] == [alert]
    assert api.repository.get_user(member_id).household_link_status == "unlinked"


def test_memory_withdraw_discovers_legacy_inactive_owned_house(api):
    owner = create_owner(api)
    member = linked_family(api, owner)
    home = owner["user"]["household_id"]
    owner_id = owner["user"]["user_id"]
    member_id = member["user"]["user_id"]
    alert = retained_alert(home, "legacy-inactive-retained")
    assert api.repository.put_alert(alert) is True
    household = api.repository.households[home]
    household.status = "inactive"
    household.inactive_at = iso_utc(NOW)
    household.emergency_address = None
    if household.invite_hash:
        api.repository.invites.pop(household.invite_hash, None)
    household.invite_hash = None
    household.invite_nonce = None
    household.invite_expires_at = None
    for user_id in (owner_id, member_id):
        value = api.repository.users[user_id]
        value.household_id = None
        value.role = None
        value.household_link_status = "unlinked"
        value.linked_at = None
        api.repository.alarm_last_seen.pop((home, user_id), None)
    api.repository.members[home] = set()

    removed = api.client.request(
        "DELETE", "/me", headers=auth_header(owner),
        json={"current_password": "StrongPassword123"},
    )

    assert removed.status_code == 204
    assert api.repository.get_household(home) is None
    assert home not in api.repository.members and home not in api.repository.contacts
    assert not any(key[0] == home for key in api.repository.devices)
    assert not any(value[0] == home for value in api.repository.device_credentials.values())
    assert api.repository.get_user(member_id).household_link_status == "unlinked"
    assert api.repository.alerts[home] == [alert]


def test_memory_unlinked_member_withdrawal_never_destroys_shared_household(api):
    owner = create_owner(api)
    member = linked_family(api, owner)
    home = owner["user"]["household_id"]
    api.repository.create_contact(home, {
        "contact_id": "must-survive",
        "name": "보존 연락처",
        "relationship": "가족",
        "phone_number": "+821033334444",
        "created_at": iso_utc(),
    })

    unlinked = api.client.delete(
        "/households/current/link", headers=auth_header(member),
    )
    assert unlinked.status_code == 200
    before = memory_household_state(api.repository, home)
    removed = api.client.request(
        "DELETE", "/me", headers=auth_header(member),
        json={"current_password": "MemberPassword123"},
    )
    assert removed.status_code == 204
    assert memory_household_state(api.repository, home) == before
    assert api.repository.get_household(home).status == "active"


def decode(raw):
    deserializer = TypeDeserializer()
    return {key: deserializer.deserialize(value) for key, value in raw.items()}


def condition_matches(expression, item):
    if expression is None:
        return True
    parts = expression.get_expression()
    values = parts["values"]
    operator = parts["operator"]
    if operator == "AND":
        return all(condition_matches(value, item) for value in values)
    if operator == "OR":
        return any(condition_matches(value, item) for value in values)
    actual = item.get(values[0].name)
    if operator == "=":
        return actual == values[1]
    if operator == "begins_with":
        return isinstance(actual, str) and actual.startswith(values[1])
    raise AssertionError(f"unsupported query condition: {operator}")


def emitted_condition_matches(expression, item, names, values):
    """Evaluate the small condition grammar used by withdrawal against live rows."""
    tokens = re.findall(r"attribute_not_exists|attribute_exists|AND|OR|[#:A-Za-z0-9_.-]+|[=(),]", expression)
    position = 0
    missing = object()

    def operand(token):
        return values[token] if token.startswith(":") else item.get(names.get(token, token), missing)

    def atom():
        nonlocal position
        token = tokens[position]
        position += 1
        if token == "(":
            result = disjunction()
            assert tokens[position] == ")"
            position += 1
            return result
        if token in {"attribute_exists", "attribute_not_exists"}:
            assert tokens[position] == "("
            field = names.get(tokens[position + 1], tokens[position + 1])
            assert tokens[position + 2] == ")"
            position += 3
            exists = field in item
            return exists if token == "attribute_exists" else not exists
        left = operand(token)
        assert tokens[position] == "=", expression
        right = operand(tokens[position + 1])
        position += 2
        return left is not missing and right is not missing and left == right

    def conjunction():
        nonlocal position
        result = atom()
        while position < len(tokens) and tokens[position] == "AND":
            position += 1
            next_result = atom()
            result = result and next_result
        return result

    def disjunction():
        nonlocal position
        result = conjunction()
        while position < len(tokens) and tokens[position] == "OR":
            position += 1
            next_result = conjunction()
            result = result or next_result
        return result

    result = disjunction()
    assert position == len(tokens), expression
    return result


class AtomicTable:
    def __init__(self, items):
        self.items = {(item["pk"], item["sk"]): copy.deepcopy(item) for item in items}
        self.reads, self.scans, self.queries, self.prewrites = [], [], [], []

    def get_item(self, **kwargs):
        self.reads.append(kwargs)
        assert kwargs.get("ConsistentRead") is True
        item = self.items.get((kwargs["Key"]["pk"], kwargs["Key"]["sk"]))
        return {"Item": copy.deepcopy(item)} if item else {}

    def _page(self, kwargs, expression):
        selected = [item for _, item in sorted(self.items.items()) if condition_matches(expression, item)]
        offset = kwargs.get("ExclusiveStartKey", {}).get("page", 0)
        response = {"Items": copy.deepcopy(selected[offset:offset + 2])}
        if offset + 2 < len(selected):
            response["LastEvaluatedKey"] = {"page": offset + 2}
        return response

    def scan(self, **kwargs):
        self.scans.append(kwargs)
        assert kwargs.get("ConsistentRead") is True
        return self._page(kwargs, kwargs.get("FilterExpression"))

    def query(self, **kwargs):
        self.queries.append(kwargs)
        assert kwargs.get("ConsistentRead") is True
        return self._page(kwargs, kwargs.get("KeyConditionExpression"))

    def _forbid_prewrite(self, **kwargs):
        self.prewrites.append(kwargs)
        raise AssertionError("withdrawal wrote outside its final transaction")

    update_item = delete_item = put_item = _forbid_prewrite


class ImmutableAlertTable:
    def __init__(self, household_id):
        expires_at = int((datetime.now(UTC) + timedelta(days=89)).timestamp())
        event_key = f"{iso_utc(datetime.now(UTC) - timedelta(days=1))}#retained-event"
        self.items = {
            (household_id, event_key): {
                "household_id": household_id,
                "event_key": event_key,
                "event_id": "retained-event",
                "source_device_id": "rpi-001",
                "location": "거실",
                "sound": "아기울음소리",
                "expires_at_epoch": expires_at,
            }
        }
        self.writes = []

    def _forbid_write(self, **kwargs):
        self.writes.append(kwargs)
        raise AssertionError("withdrawal must not write the retained alerts table")

    put_item = update_item = delete_item = _forbid_write


class AtomicClient:
    def __init__(self, table, *, cancel=False, race=None):
        self.table, self.cancel, self.race = table, cancel, race
        self.transactions = []
        self.after_race = None

    def transact_write_items(self, *, TransactItems):
        self.transactions.append(copy.deepcopy(TransactItems))
        assert len(TransactItems) <= 100
        identities = []
        for operation in TransactItems:
            value = next(iter(operation.values()))
            assert value["TableName"] == "isolated-core"
            key = decode(value["Key"])
            identities.append((key["pk"], key["sk"]))
        assert len(identities) == len(set(identities)), "duplicate transaction item"
        if self.race:
            self.race(self.table.items)
            self.after_race = copy.deepcopy(self.table.items)
        valid = not self.cancel
        for operation, identity in zip(TransactItems, identities):
            value = next(iter(operation.values()))
            expression = value.get("ConditionExpression")
            if expression and not emitted_condition_matches(
                expression, self.table.items.get(identity, {}),
                value.get("ExpressionAttributeNames", {}),
                decode(value.get("ExpressionAttributeValues", {})),
            ):
                valid = False
        if not valid:
            raise ClientError({
                "Error": {"Code": "TransactionCanceledException", "Message": "simulated condition failure"},
                "CancellationReasons": [{"Code": "ConditionalCheckFailed"}],
            }, "TransactWriteItems")
        # Apply only after ALL conditions pass, mirroring transaction atomicity.
        pending = copy.deepcopy(self.table.items)
        for operation, identity in zip(TransactItems, identities):
            kind, value = next(iter(operation.items()))
            if kind == "Delete":
                pending.pop(identity, None)
            elif kind == "Update":
                item = pending[identity]
                names = value.get("ExpressionAttributeNames", {})
                values = decode(value.get("ExpressionAttributeValues", {}))
                clauses = re.split(r"\b(SET|REMOVE|ADD)\b", value["UpdateExpression"])[1:]
                for keyword, clause in zip(clauses[::2], clauses[1::2]):
                    for assignment in clause.strip().split(","):
                        if keyword == "REMOVE":
                            item.pop(names.get(assignment.strip(), assignment.strip()), None)
                        elif keyword == "SET":
                            field, placeholder = [part.strip() for part in assignment.split("=", 1)]
                            item[names.get(field, field)] = values[placeholder]
                        else:
                            field, placeholder = assignment.split()
                            field = names.get(field, field)
                            item[field] = item.get(field, 0) + values[placeholder]
            else:
                assert kind == "ConditionCheck"
        self.table.items = pending


def dynamo_fixture(*, cancel=False, race=None, token_count=2):
    owner = User(
        "owner-1", "atomicowner", "보호자", "+821090001001", "owner-hash", "household_owner",
        household_id=HOME, role="owner", household_link_status="linked",
        terms_service_agreed=True, privacy_agreed=True, terms_version="v1", privacy_version="p1",
        consented_at="2026-10-07T00:00:00Z", linked_at="2026-10-07T00:00:00Z",
    )
    members = [User(
        f"member-{index}", f"atomicmember{index}", "가족", f"+82109000100{index + 1}",
        f"member-{index}-hash", "family_member", household_id=HOME, role="member",
        household_link_status="linked", linked_at="2026-10-07T00:00:00Z",
    ) for index in (1, 2)]
    household = Household(
        HOME, "집", owner.user_id,
        emergency_address=EmergencyAddress("12345", "합성 주소", "합성 상세", "manual"),
        invite_hash="invite-hash", invite_nonce="invite-nonce", invite_expires_at="2999-01-01T00:00:00Z",
    )
    items = [{"pk": f"HOUSE#{HOME}", "sk": "META", **asdict(household), "membership_version": 0},
             {"pk": "INVITE#invite-hash", "sk": "INVITE", "household_id": HOME},
             {"pk": f"HOUSE#{HOME}", "sk": "CONTACT#delete-with-house",
              "contact_id": "delete-with-house", "name": "긴급 연락처",
              "relationship": "가족", "phone_number": "+821011112222"}]
    for device_id, location, device_type in (
        ("rpi-001", "거실", "hub"),
        ("esp32_1", "안방", "alert_node"),
        ("esp32_2", "현관", "alert_node"),
        ("esp32_3", "화장실", "alert_node"),
    ):
        device = Device(
            HOME, device_id, location, device_type,
            credential_hash=f"credential-{device_id}",
        )
        items.extend([
            {"pk": f"HOUSE#{HOME}", "sk": f"DEVICE#{device_id}", **asdict(device)},
            {"pk": f"DEVICECRED#{device.credential_hash}", "sk": "DEVICE",
             "household_id": HOME, "device_id": device_id},
        ])
    for value in [owner, *members]:
        items.extend([
            {"pk": f"USER#{value.user_id}", "sk": "PROFILE", **asdict(value)},
            {"pk": f"LOGINID#{value.login_id}", "sk": "USER", "user_id": value.user_id},
            {"pk": f"PHONE#{value.phone_number}", "sk": "USER", "user_id": value.user_id},
            {"pk": f"HOUSE#{HOME}", "sk": f"MEMBER#{value.user_id}", "user_id": value.user_id,
             "linked_at": value.linked_at, "alarms_last_seen_at": "2026-10-07T01:00:00Z"},
            {"pk": f"USER#{value.user_id}", "sk": "CONSENT#v1", "terms_version": "v1"},
        ])
        for index in range(token_count):
            items.append({"pk": f"TOKEN#{value.user_id}-{index}", "sk": "REFRESH", "user_id": value.user_id})
        items.append({"pk": f"HOUSE#{HOME}", "sk": f"ALIAS#{value.user_id}#member-2",
                      "viewer_user_id": value.user_id, "member_user_id": "member-2"})
    items.append({"pk": "HOUSE#other", "sk": "ALIAS#outsider#owner-1",
                  "viewer_user_id": "outsider", "member_user_id": owner.user_id})
    other_household = Household(OTHER_HOME, "다른 집", "other-owner")
    other_device = Device(
        OTHER_HOME, "other-rpi", "다른 거실", "hub",
        credential_hash="credential-other-rpi",
    )
    items.extend([
        {"pk": f"HOUSE#{OTHER_HOME}", "sk": "META", **asdict(other_household)},
        {"pk": f"HOUSE#{OTHER_HOME}", "sk": "DEVICE#other-rpi", **asdict(other_device)},
        {"pk": "DEVICECRED#credential-other-rpi", "sk": "DEVICE",
         "household_id": OTHER_HOME, "device_id": "other-rpi"},
        {"pk": f"HOUSE#{OTHER_HOME}", "sk": "CONTACT#other-contact",
         "contact_id": "other-contact", "name": "다른 연락처",
         "relationship": "가족", "phone_number": "+821099998888"},
        {"pk": f"HOUSE#{OTHER_HOME}", "sk": "ALIAS#other-owner#other-member",
         "viewer_user_id": "other-owner", "member_user_id": "other-member",
         "display_name": "다른 별명"},
        {"pk": f"ALERTID#{HOME}#retained-event", "sk": "ALERT",
         "event_key": "retained-event-key",
         "expires_at_epoch": int((datetime.now(UTC) + timedelta(days=89)).timestamp())},
    ])
    repo = DynamoRepository.__new__(DynamoRepository)
    repo.settings = SimpleNamespace(core_table="isolated-core", alerts_table="isolated-alerts")
    repo.core = AtomicTable(items)
    repo.alerts = ImmutableAlertTable(HOME)
    repo.client = AtomicClient(repo.core, cancel=cancel, race=race)
    return repo, owner, members


def withdraw(repo, value):
    return repo.withdraw_user_account(value.user_id, NOW, expected_password_hash=value.password_hash)


def make_legacy_inactive_owned_house(repo, owner, members):
    meta = repo.core.items[(f"HOUSE#{HOME}", "META")]
    meta["status"] = "inactive"
    meta["inactive_at"] = iso_utc(NOW)
    for field in ("emergency_address", "invite_hash", "invite_nonce", "invite_expires_at"):
        meta.pop(field, None)
    repo.core.items.pop(("INVITE#invite-hash", "INVITE"), None)
    for value in (owner, *members):
        profile = repo.core.items[(f"USER#{value.user_id}", "PROFILE")]
        profile["household_link_status"] = "unlinked"
        for field in ("household_id", "role", "linked_at"):
            profile.pop(field, None)
        repo.core.items.pop((f"HOUSE#{HOME}", f"MEMBER#{value.user_id}"), None)


@pytest.mark.parametrize("role", ["owner", "member"])
def test_dynamo_cancellation_preserves_entire_state_and_has_no_preunlink_writes(role):
    repo, owner, members = dynamo_fixture(cancel=True)
    selected = owner if role == "owner" else members[0]
    before = copy.deepcopy(repo.core.items)
    with pytest.raises(ConflictError) as error:
        withdraw(repo, selected)
    assert error.value.code == "ACCOUNT_DELETION_CONFLICT"
    assert len(repo.client.transactions) == 1
    assert repo.core.prewrites == []
    assert repo.core.items == before


def test_dynamo_owner_success_deletes_owned_core_graph_and_preserves_alerts_and_other_house():
    repo, owner, members = dynamo_fixture()
    before = copy.deepcopy(repo.core.items)
    alerts_before = copy.deepcopy(repo.alerts.items)
    other_before = {
        key: copy.deepcopy(item)
        for key, item in before.items()
        if key[0] == f"HOUSE#{OTHER_HOME}"
        or key == ("DEVICECRED#credential-other-rpi", "DEVICE")
    }
    result = withdraw(repo, owner)
    assert result == {"previous_household_id": HOME, "household_status": "inactive"}
    assert len(repo.client.transactions) == 1 and repo.core.prewrites == []
    assert any("ExclusiveStartKey" in call for call in repo.core.scans)
    assert any("ExclusiveStartKey" in call for call in repo.core.queries)
    assert not any(key[0] == f"HOUSE#{HOME}" for key in repo.core.items)
    assert not any(
        key[0].startswith("DEVICECRED#") and item.get("household_id") == HOME
        for key, item in repo.core.items.items()
    )
    for value in members:
        profile = repo.core.items[(f"USER#{value.user_id}", "PROFILE")]
        assert profile["household_link_status"] == "unlinked"
        assert not {"household_id", "role", "linked_at"} & profile.keys()
        assert (f"LOGINID#{value.login_id}", "USER") in repo.core.items
        assert (f"PHONE#{value.phone_number}", "USER") in repo.core.items
        assert (f"TOKEN#{value.user_id}-0", "REFRESH") in repo.core.items
        assert (f"USER#{value.user_id}", "CONSENT#v1") in repo.core.items
    for key, item in before.items():
        if item.get("user_id") == owner.user_id or item.get("viewer_user_id") == owner.user_id or item.get("member_user_id") == owner.user_id or key[0] == f"USER#{owner.user_id}":
            assert key not in repo.core.items
    assert ("INVITE#invite-hash", "INVITE") not in repo.core.items
    assert (f"ALERTID#{HOME}#retained-event", "ALERT") in repo.core.items
    assert repo.alerts.items == alerts_before and repo.alerts.writes == []
    assert {
        key: repo.core.items[key] for key in other_before
    } == other_before


def test_dynamo_unlinked_owner_withdrawal_discovers_and_deletes_legacy_inactive_house():
    repo, owner, members = dynamo_fixture()
    make_legacy_inactive_owned_house(repo, owner, members)
    alerts_before = copy.deepcopy(repo.alerts.items)
    other_before = {
        key: copy.deepcopy(item)
        for key, item in repo.core.items.items()
        if key[0] == f"HOUSE#{OTHER_HOME}"
        or key == ("DEVICECRED#credential-other-rpi", "DEVICE")
    }

    result = withdraw(repo, owner)

    assert result == {"previous_household_id": HOME, "household_status": "inactive"}
    assert len(repo.client.transactions) == 1 and repo.core.prewrites == []
    assert not any(key[0] == f"HOUSE#{HOME}" for key in repo.core.items)
    assert not any(
        key[0].startswith("DEVICECRED#") and item.get("household_id") == HOME
        for key, item in repo.core.items.items()
    )
    for member in members:
        profile = repo.core.items[(f"USER#{member.user_id}", "PROFILE")]
        assert profile["household_link_status"] == "unlinked"
        assert not {"household_id", "role", "linked_at"} & profile.keys()
    assert repo.alerts.items == alerts_before and repo.alerts.writes == []
    assert (f"ALERTID#{HOME}#retained-event", "ALERT") in repo.core.items
    assert {key: repo.core.items[key] for key in other_before} == other_before


def test_dynamo_unlinked_member_withdrawal_cannot_destroy_shared_household():
    repo, owner, members = dynamo_fixture()
    member = members[0]
    profile = repo.core.items[(f"USER#{member.user_id}", "PROFILE")]
    profile["household_link_status"] = "unlinked"
    for field in ("household_id", "role", "linked_at"):
        profile.pop(field, None)
    repo.core.items.pop((f"HOUSE#{HOME}", f"MEMBER#{member.user_id}"))
    repo.core.items[(f"HOUSE#{HOME}", "META")]["membership_version"] += 1
    protected = {
        key: copy.deepcopy(item)
        for key, item in repo.core.items.items()
        if key == (f"HOUSE#{HOME}", "META")
        or key[1].startswith(("DEVICE#", "CONTACT#"))
        or key[0].startswith("DEVICECRED#")
    }
    alerts_before = copy.deepcopy(repo.alerts.items)

    result = withdraw(repo, member)

    assert result == {"previous_household_id": None, "household_status": None}
    assert {key: repo.core.items[key] for key in protected} == protected
    assert repo.core.items[(f"USER#{owner.user_id}", "PROFILE")][
        "household_link_status"
    ] == "linked"
    assert repo.alerts.items == alerts_before and repo.alerts.writes == []


def test_dynamo_member_success_keeps_peer_profile_membership_and_last_seen_untouched():
    repo, owner, members = dynamo_fixture()
    member, peer = members
    before = copy.deepcopy(repo.core.items)
    alerts_before = copy.deepcopy(repo.alerts.items)
    result = withdraw(repo, member)
    assert result == {"previous_household_id": HOME, "household_status": "active"}
    assert len(repo.client.transactions) == 1 and repo.core.prewrites == []
    for value in (owner, peer):
        for key in ((f"USER#{value.user_id}", "PROFILE"), (f"HOUSE#{HOME}", f"MEMBER#{value.user_id}")):
            assert repo.core.items[key] == before[key]
    household = repo.core.items[(f"HOUSE#{HOME}", "META")]
    for field in ("status", "emergency_address", "invite_hash", "invite_nonce", "invite_expires_at"):
        assert household[field] == before[(f"HOUSE#{HOME}", "META")][field]
    assert (f"USER#{member.user_id}", "PROFILE") not in repo.core.items
    assert (f"HOUSE#{HOME}", f"MEMBER#{member.user_id}") not in repo.core.items
    for key, item in before.items():
        if (
            key[1].startswith(("DEVICE#", "CONTACT#"))
            or key[0].startswith("DEVICECRED#")
            or key[0] == f"ALERTID#{HOME}#retained-event"
        ):
            assert repo.core.items[key] == item
    assert repo.alerts.items == alerts_before and repo.alerts.writes == []


def test_dynamo_legacy_household_without_membership_version_withdraws_as_zero():
    repo, owner, _ = dynamo_fixture()
    repo.core.items[(f"HOUSE#{HOME}", "META")].pop("membership_version")
    result = withdraw(repo, owner)
    assert result == {"previous_household_id": HOME, "household_status": "inactive"}
    assert len(repo.client.transactions) == 1
    assert (f"HOUSE#{HOME}", "META") not in repo.core.items
    meta_delete = next(
        operation["Delete"]
        for operation in repo.client.transactions[0]
        if operation.get("Delete")
        and decode(operation["Delete"]["Key"])
        == {"pk": f"HOUSE#{HOME}", "sk": "META"}
    )
    assert "attribute_not_exists(membership_version)" in meta_delete["ConditionExpression"]


@pytest.mark.parametrize("role", ["owner", "member"])
@pytest.mark.parametrize("action_count", [100, 101])
def test_dynamo_exact_transaction_capacity_boundary(role, action_count):
    repo, owner, members = dynamo_fixture()
    selected = owner if role == "owner" else members[0]
    probe, probe_owner, probe_members = dynamo_fixture()
    withdraw(probe, probe_owner if role == "owner" else probe_members[0])
    base_count = len(probe.client.transactions[0])
    assert 0 < base_count < 100
    # Fill from the implementation's complete unique-key plan so both the
    # richer owner graph and the narrower member deletion hit the exact limit.
    for index in range(action_count - base_count):
        key = (f"TOKEN#boundary-{index}", "REFRESH")
        repo.core.items[key] = {"pk": key[0], "sk": key[1], "user_id": selected.user_id}
    before = copy.deepcopy(repo.core.items)
    if action_count == 100:
        withdraw(repo, selected)
        assert len(repo.client.transactions) == 1
        assert len(repo.client.transactions[0]) == 100
        assert (f"USER#{selected.user_id}", "PROFILE") not in repo.core.items
    else:
        with pytest.raises(ConflictError) as error:
            withdraw(repo, selected)
        assert error.value.code == "ACCOUNT_DELETION_REVIEW_REQUIRED"
        assert repo.client.transactions == []
        assert repo.core.items == before
    assert repo.core.prewrites == []


@pytest.mark.parametrize("role", ["owner", "member"])
def test_dynamo_over_100_actions_fails_before_any_write_or_transaction(role):
    repo, owner, members = dynamo_fixture(token_count=101)
    before = copy.deepcopy(repo.core.items)
    with pytest.raises(ConflictError) as error:
        withdraw(repo, owner if role == "owner" else members[0])
    assert error.value.code == "ACCOUNT_DELETION_REVIEW_REQUIRED"
    assert repo.client.transactions == [] and repo.core.prewrites == []
    assert repo.core.items == before


@pytest.mark.parametrize("change", [
    "new-token", "new-alias", "identity-alias", "new-consent", "password",
    "membership", "member-relinked", "new-house-alias", "new-contact",
    "device-credential-rotated",
])
def test_dynamo_concurrent_change_is_rejected_by_emitted_snapshot_conditions(change):
    def race(items):
        owner_profile = items[("USER#owner-1", "PROFILE")]
        if change in {"new-token", "new-alias"}:
            owner_profile["reference_version"] += 1
            if change == "new-token":
                items[("TOKEN#concurrent", "REFRESH")] = {"pk": "TOKEN#concurrent", "sk": "REFRESH", "user_id": "owner-1"}
            else:
                items[("HOUSE#other", "ALIAS#owner-1#outsider")] = {"pk": "HOUSE#other", "sk": "ALIAS#owner-1#outsider", "viewer_user_id": "owner-1", "member_user_id": "outsider"}
        elif change == "identity-alias":
            items[("LOGINID#atomicowner", "USER")]["user_id"] = "another-user"
        elif change == "new-consent":
            owner_profile.update(terms_version="v2", consented_at="2026-10-08T00:00:00Z")
            items[("USER#owner-1", "CONSENT#v2")] = {"pk": "USER#owner-1", "sk": "CONSENT#v2", "terms_version": "v2"}
        elif change == "password":
            owner_profile["password_hash"] = "concurrently-changed-hash"
            owner_profile["token_version"] += 1
        elif change == "membership":
            items[(f"HOUSE#{HOME}", "META")]["membership_version"] += 1
            items[(f"HOUSE#{HOME}", "MEMBER#new-member")] = {"pk": f"HOUSE#{HOME}", "sk": "MEMBER#new-member", "user_id": "new-member"}
        elif change == "member-relinked":
            items[("USER#member-1", "PROFILE")]["household_id"] = "different-household"
        elif change == "new-house-alias":
            meta = items[(f"HOUSE#{HOME}", "META")]
            meta["registration_version"] = meta.get("registration_version", 0) + 1
            for member_id in ("member-1", "member-2"):
                items[(f"USER#{member_id}", "PROFILE")]["reference_version"] += 1
            items[(f"HOUSE#{HOME}", "ALIAS#member-1#member-2-new")] = {
                "pk": f"HOUSE#{HOME}",
                "sk": "ALIAS#member-1#member-2-new",
                "viewer_user_id": "member-1",
                "member_user_id": "member-2",
                "display_name": "경합 별명",
            }
        elif change == "new-contact":
            meta = items[(f"HOUSE#{HOME}", "META")]
            meta["registration_version"] = meta.get("registration_version", 0) + 1
            items[(f"HOUSE#{HOME}", "CONTACT#concurrent")] = {
                "pk": f"HOUSE#{HOME}", "sk": "CONTACT#concurrent",
                "contact_id": "concurrent", "name": "경합 연락처",
                "relationship": "가족", "phone_number": "+821077778888",
            }
        else:
            assert change == "device-credential-rotated"
            meta = items[(f"HOUSE#{HOME}", "META")]
            meta["registration_version"] = meta.get("registration_version", 0) + 1
            device = items[(f"HOUSE#{HOME}", "DEVICE#rpi-001")]
            device["credential_hash"] = "credential-rpi-rotated"
            items.pop(("DEVICECRED#credential-rpi-001", "DEVICE"))
            items[("DEVICECRED#credential-rpi-rotated", "DEVICE")] = {
                "pk": "DEVICECRED#credential-rpi-rotated", "sk": "DEVICE",
                "household_id": HOME, "device_id": "rpi-001",
            }

    repo, owner, _ = dynamo_fixture(race=race)
    with pytest.raises(ConflictError) as error:
        withdraw(repo, owner)
    assert error.value.code == "ACCOUNT_DELETION_CONFLICT"
    assert len(repo.client.transactions) == 1 and repo.core.prewrites == []
    assert repo.client.after_race is not None
    assert repo.core.items == repo.client.after_race


def test_dynamo_stale_password_fails_before_transaction_and_linked_membership_unchanged():
    repo, owner, _ = dynamo_fixture()
    before = copy.deepcopy(repo.core.items)
    with pytest.raises(ConflictError) as error:
        repo.withdraw_user_account(owner.user_id, NOW, expected_password_hash="stale-hash")
    assert error.value.code == "ACCOUNT_DELETION_CONFLICT"
    assert repo.client.transactions == [] and repo.core.items == before


def test_dynamo_requested_profile_identifier_mismatch_is_review_required_without_any_write():
    repo, _, members = dynamo_fixture()
    member = members[0]
    profile = repo.core.items[(f"USER#{member.user_id}", "PROFILE")]
    profile["household_link_status"] = "unlinked"
    profile["user_id"] = "different-user"
    for field in ("household_id", "role", "linked_at"):
        profile.pop(field, None)
    repo.core.items.pop((f"HOUSE#{HOME}", f"MEMBER#{member.user_id}"))
    repo.core.items[(f"HOUSE#{HOME}", "META")]["membership_version"] += 1
    before = copy.deepcopy(repo.core.items)

    with pytest.raises(ConflictError) as error:
        withdraw(repo, member)

    assert error.value.code == "ACCOUNT_DELETION_REVIEW_REQUIRED"
    assert repo.client.transactions == [] and repo.core.prewrites == []
    assert repo.core.items == before


def test_dynamo_member_profile_identifier_mismatch_is_review_required_without_any_write():
    repo, owner, members = dynamo_fixture()
    repo.core.items[(f"USER#{members[0].user_id}", "PROFILE")]["user_id"] = "different-user"
    before = copy.deepcopy(repo.core.items)

    with pytest.raises(ConflictError) as error:
        withdraw(repo, owner)

    assert error.value.code == "ACCOUNT_DELETION_REVIEW_REQUIRED"
    assert repo.client.transactions == [] and repo.core.prewrites == []
    assert repo.core.items == before


@pytest.mark.parametrize("missing", [False, True])
def test_dynamo_inconsistent_membership_is_review_required_without_any_write(missing):
    repo, owner, _ = dynamo_fixture()
    if missing:
        repo.core.items.pop(("USER#member-1", "PROFILE"))
    else:
        repo.core.items[("USER#member-1", "PROFILE")]["household_id"] = "different-household"
    before = copy.deepcopy(repo.core.items)
    with pytest.raises(ConflictError) as error:
        withdraw(repo, owner)
    assert error.value.code == "ACCOUNT_DELETION_REVIEW_REQUIRED"
    assert repo.client.transactions == [] and repo.core.prewrites == []
    assert repo.core.items == before


@pytest.mark.parametrize("inconsistency", [
    "unknown-house-row", "credential-rebound", "invite-rebound",
])
def test_dynamo_unknown_owned_metadata_fails_closed_before_any_write(inconsistency):
    repo, owner, _ = dynamo_fixture()
    if inconsistency == "unknown-house-row":
        repo.core.items[(f"HOUSE#{HOME}", "FUTURE#metadata")] = {
            "pk": f"HOUSE#{HOME}", "sk": "FUTURE#metadata", "value": "unknown",
        }
    elif inconsistency == "credential-rebound":
        repo.core.items[("DEVICECRED#credential-rpi-001", "DEVICE")][
            "household_id"
        ] = OTHER_HOME
    else:
        assert inconsistency == "invite-rebound"
        repo.core.items[("INVITE#invite-hash", "INVITE")]["household_id"] = OTHER_HOME
    before = copy.deepcopy(repo.core.items)

    with pytest.raises(ConflictError) as error:
        withdraw(repo, owner)

    assert error.value.code == "ACCOUNT_DELETION_REVIEW_REQUIRED"
    assert repo.client.transactions == [] and repo.core.prewrites == []
    assert repo.core.items == before


@pytest.mark.parametrize("inconsistency", [
    "owner-membership-missing", "duplicate-owner-role", "owner-id-mismatch",
])
def test_direct_dynamo_owner_unlink_rejects_inconsistent_owner_inventory(inconsistency):
    repo, owner, members = dynamo_fixture()
    if inconsistency == "owner-membership-missing":
        repo.core.items.pop((f"HOUSE#{HOME}", f"MEMBER#{owner.user_id}"))
    elif inconsistency == "duplicate-owner-role":
        repo.core.items[(f"USER#{members[0].user_id}", "PROFILE")]["role"] = "owner"
    else:
        repo.core.items[(f"HOUSE#{HOME}", "META")]["owner_user_id"] = members[0].user_id
    before = copy.deepcopy(repo.core.items)

    with pytest.raises(ConflictError) as error:
        repo.unlink_user(owner.user_id, NOW)

    assert error.value.code == "ACCOUNT_DELETION_REVIEW_REQUIRED"
    assert repo.client.transactions == []
    assert repo.core.prewrites == []
    assert repo.core.items == before
