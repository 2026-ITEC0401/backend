from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

from hearo_backend.domain import Alert, Household, iso_utc
from hearo_backend.retention import (
    ALERT_RETENTION_SECONDS,
    alert_expiry_epoch,
    is_expired_timestamp,
)
from hearo_backend.store import DynamoRepository, MemoryRepository, StoreError

from .conftest import auth_header, create_owner


def alert(
    event_id: str,
    occurred_at: datetime,
    *,
    household_id: str = "home-retention",
) -> Alert:
    return Alert(
        household_id=household_id,
        event_id=event_id,
        timestamp=iso_utc(occurred_at),
        source_device_id="rpi-001",
        location="거실",
        sound="아기울음소리",
        raw_label="한국어_분류값",
        type="Urgent",
        confidence=0.91,
    )


def stored_item(value: Alert) -> dict:
    item = asdict(value)
    item["event_key"] = value.event_key
    item["alarm_lookup_key"] = f"{value.household_id}#{value.event_id}"
    return item


def activate_memory_household(repository: MemoryRepository, household_id: str) -> None:
    repository.households[household_id] = Household(
        household_id, "알림 보존 테스트", "owner-retention",
    )


def test_exact_ninety_day_boundary_and_memory_legacy_visibility(monkeypatch):
    fixed_now = datetime(2026, 10, 7, 12, 0, 0, 500_000, tzinfo=UTC)
    exact_boundary = fixed_now - timedelta(days=90)
    inside_boundary = exact_boundary + timedelta(microseconds=1)
    exact_timestamp = iso_utc(exact_boundary)
    inside_timestamp = iso_utc(inside_boundary)

    assert is_expired_timestamp(exact_timestamp, fixed_now)
    assert not is_expired_timestamp(inside_timestamp, fixed_now)
    assert alert_expiry_epoch(exact_timestamp) == int(fixed_now.timestamp()) + 1
    assert datetime.fromtimestamp(alert_expiry_epoch(exact_timestamp), UTC) >= fixed_now
    assert datetime.fromtimestamp(alert_expiry_epoch(inside_timestamp), UTC) >= (
        inside_boundary + timedelta(days=90)
    )

    boundary_repository = MemoryRepository()
    activate_memory_household(boundary_repository, "home-retention")
    with monkeypatch.context() as patch:
        patch.setattr("hearo_backend.retention.utc_now", lambda: fixed_now)
        at_boundary = alert("at-boundary", exact_boundary)
        inside = alert("inside-boundary", inside_boundary)
        assert boundary_repository.put_alert(at_boundary) is False
        assert boundary_repository.put_alert(inside) is True

        # A legacy row without TTL is filtered by the same exact logical boundary.
        at_boundary.expires_at_epoch = None
        boundary_repository.alerts[at_boundary.household_id].append(at_boundary)
        assert boundary_repository.get_alert(
            at_boundary.household_id, at_boundary.event_id
        ) is None
        assert boundary_repository.get_alert(inside.household_id, inside.event_id) is inside
        assert [
            item.event_id
            for item in boundary_repository.latest_alerts(inside.household_id, 10)
        ] == [inside.event_id]

    repository = MemoryRepository()
    activate_memory_household(repository, "home-retention")
    current = datetime.now(UTC)
    visible = alert("현재-알림", current - timedelta(days=7))
    expired_legacy = alert("만료-기존-알림", current - timedelta(days=91))
    expired_legacy.expires_at_epoch = None
    repository.alerts[visible.household_id] = [expired_legacy]

    assert repository.put_alert(visible) is True
    assert visible.expires_at_epoch == alert_expiry_epoch(visible.timestamp)
    assert repository.get_alert(visible.household_id, expired_legacy.event_id) is None
    assert [item.event_id for item in repository.latest_alerts(visible.household_id, 10)] == [
        visible.event_id
    ]
    assert [
        item.event_id
        for item in repository.query_alerts(
            visible.household_id,
            current - timedelta(days=100),
            current + timedelta(days=1),
        )
    ] == [visible.event_id]


def test_memory_rejects_expired_and_invalid_but_accepts_future_and_duplicates():
    repository = MemoryRepository()
    activate_memory_household(repository, "home-retention")
    current = datetime.now(UTC)

    assert repository.put_alert(alert("expired", current - timedelta(days=91))) is False
    future = alert("future", current + timedelta(minutes=4))
    assert repository.put_alert(future) is True
    assert repository.put_alert(alert("future", current + timedelta(minutes=4))) is False

    malformed = alert("bad-time", current)
    malformed.timestamp = "not-a-timestamp"
    with pytest.raises(StoreError) as error:
        repository.put_alert(malformed)
    assert error.value.code == "INVALID_ALERT_TIMESTAMP"


def test_memory_alert_write_requires_an_active_household():
    repository = MemoryRepository()
    current = datetime.now(UTC)
    assert repository.put_alert(alert("missing-house", current)) is False
    activate_memory_household(repository, "home-retention")
    repository.households["home-retention"].status = "inactive"
    assert repository.put_alert(alert("inactive-house", current)) is False
    assert repository.alerts == {}


class RecordingClient:
    class exceptions:
        class TransactionCanceledException(Exception):
            pass

    def __init__(self):
        self.operations = []

    def transact_write_items(self, *, TransactItems):
        self.operations = TransactItems


def decode(raw: dict) -> dict:
    deserializer = TypeDeserializer()
    return {key: deserializer.deserialize(value) for key, value in raw.items()}


def decode_item(operation: dict) -> dict:
    return decode(operation["Put"]["Item"])


def test_dynamo_new_body_and_event_id_alias_share_the_exact_ttl():
    repository = DynamoRepository.__new__(DynamoRepository)
    repository.settings = SimpleNamespace(
        alerts_table="alerts-table", core_table="core-table"
    )
    repository.client = RecordingClient()
    value = alert("same-ttl", datetime.now(UTC) - timedelta(minutes=1))

    assert repository.put_alert(value) is True
    condition = repository.client.operations[0]["ConditionCheck"]
    assert decode(condition["Key"]) == {
        "pk": "HOUSE#home-retention", "sk": "META",
    }
    assert "#status=:active" in condition["ConditionExpression"]
    body = decode_item(repository.client.operations[1])
    alias = decode_item(repository.client.operations[2])
    expected = alert_expiry_epoch(value.timestamp)
    assert body["expires_at_epoch"] == expected
    assert alias["expires_at_epoch"] == expected
    assert value.expires_at_epoch == expected


class PaginatedAlertTable:
    def __init__(self, expired: Alert, visible: Alert, *, fail_index: bool):
        self.expired = stored_item(expired)
        self.visible = stored_item(visible)
        self.fail_index = fail_index
        self.calls: list[dict] = []

    def query(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("IndexName") and self.fail_index:
            raise ClientError(
                {"Error": {"Code": "ValidationException", "Message": "index missing"}},
                "Query",
            )
        if "ExclusiveStartKey" not in kwargs:
            return {"Items": [self.expired], "LastEvaluatedKey": {"page": "next"}}
        return {"Items": [self.visible]}


def test_dynamo_gsi_fallback_and_latest_pagination_skip_expired_legacy_rows():
    current = datetime.now(UTC)
    expired = alert("lookup-id", current - timedelta(days=91))
    visible = alert("lookup-id", current - timedelta(days=1))
    table = PaginatedAlertTable(expired, visible, fail_index=True)
    repository = DynamoRepository.__new__(DynamoRepository)
    repository.alerts = table

    found = repository.get_alert(visible.household_id, visible.event_id)
    assert found is not None
    assert found.timestamp == visible.timestamp
    assert any("FilterExpression" in call for call in table.calls)

    latest_table = PaginatedAlertTable(expired, visible, fail_index=False)
    repository.alerts = latest_table
    latest = repository.latest_alerts(visible.household_id, 1)
    assert [item.timestamp for item in latest] == [visible.timestamp]
    assert len(latest_table.calls) == 2

    history_table = PaginatedAlertTable(expired, visible, fail_index=False)
    repository.alerts = history_table
    history = repository.query_alerts(
        visible.household_id,
        current - timedelta(days=100),
        current + timedelta(days=1),
    )
    assert [item.timestamp for item in history] == [visible.timestamp]
    assert history[0].sound == "아기울음소리"
    assert len(history_table.calls) == 2


def test_internal_mqtt_future_boundary_and_expired_alert_suppresses_websocket(api):
    owner = create_owner(api)
    household_id = owner["user"]["household_id"]
    internal_headers = {"X-Internal-Token": api.settings.internal_token}
    broadcasts: list[dict] = []

    async def record_broadcast(_household_id, event):
        broadcasts.append(event)

    api.client.app.state.realtime.broadcast = record_broadcast

    def payload(event_id: str, occurred_at: datetime) -> dict:
        return {
            "household_id": household_id,
            "event_id": event_id,
            "timestamp": iso_utc(occurred_at),
            "source_device_id": "rpi-001",
            "location": "클라이언트 위치",
            "sound": "도어락소리",
            "raw_label": "도어락_개방음",
            "type": "Visitor",
            "confidence": 0.9,
        }

    accepted = api.client.post(
        "/internal/mqtt/alert",
        headers=internal_headers,
        json=payload("future-five", datetime.now(UTC) + timedelta(minutes=5)),
    )
    assert accepted.status_code == 200, accepted.text

    rejected = api.client.post(
        "/internal/mqtt/alert",
        headers=internal_headers,
        json=payload("future-over", datetime.now(UTC) + timedelta(minutes=5, seconds=1)),
    )
    assert rejected.status_code == 400
    assert rejected.json()["code"] == "INVALID_TIMESTAMP"

    broadcast_count = len(broadcasts)
    expired = api.client.post(
        "/internal/mqtt/alert",
        headers=internal_headers,
        json=payload("already-expired", datetime.now(UTC) - timedelta(days=91)),
    )
    assert expired.status_code == 200, expired.text
    assert len(broadcasts) == broadcast_count
    assert api.repository.get_alert(household_id, "already-expired") is None

    latest = api.client.get(
        f"/households/{household_id}/alarms/latest",
        headers=auth_header(owner),
    )
    assert latest.status_code == 200
    assert latest.json()["alarm"]["id"] == "future-five"
    assert ALERT_RETENTION_SECONDS == 90 * 24 * 60 * 60
