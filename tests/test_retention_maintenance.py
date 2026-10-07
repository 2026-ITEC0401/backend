from __future__ import annotations

import stat
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from hearo_backend.domain import iso_utc
from hearo_backend.retention import (
    RetentionPlan,
    RetentionPlanError,
    RetentionTarget,
    alert_expiry_epoch,
    apply_retention_plan,
    build_retention_plan,
    load_plan_file,
    write_plan_file,
)
from scripts.alert_retention import _validate_mode_arguments, parser


class ConditionalCheckFailedException(Exception):
    def __init__(self):
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}
        super().__init__("condition failed")


class TransactionCanceledException(Exception):
    def __init__(self):
        self.response = {
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [{"Code": "ConditionalCheckFailed"}, {"Code": "None"}],
        }
        super().__init__("transaction condition failed")


class TableStub:
    def __init__(self, pages: list[list[dict]], *, key_names: tuple[str, str]):
        self.pages = pages
        self.key_names = key_names
        self.items = {
            self._key(item): dict(item)
            for page in pages
            for item in page
            if all(name in item for name in key_names)
        }
        self.scan_calls: list[dict] = []
        self.write_calls: list[tuple[str, dict]] = []

    def _key(self, item: dict) -> tuple:
        return tuple(item[name] for name in self.key_names)

    def scan(self, **kwargs):
        self.scan_calls.append(kwargs)
        page_index = int(kwargs.get("ExclusiveStartKey", {}).get("page", 0))
        response = {"Items": self.pages[page_index]}
        if page_index + 1 < len(self.pages):
            response["LastEvaluatedKey"] = {"page": page_index + 1}
        return response

    def _condition_matches(self, item: dict | None, kwargs: dict) -> bool:
        if item is None:
            return False
        values = kwargs.get("ExpressionAttributeValues", {})
        expected_timestamp = values.get(":timestamp")
        if expected_timestamp is not None and item.get("timestamp") != expected_timestamp:
            return False
        expected_event_key = values.get(":event_key")
        if expected_event_key is not None and item.get("event_key") != expected_event_key:
            return False
        if "attribute_not_exists(event_key)" in kwargs["ConditionExpression"]:
            if "event_key" in item:
                return False
        new_ttl = values.get(":expires_at_epoch")
        if new_ttl is not None and item.get("expires_at_epoch") == new_ttl:
            return False
        return True

    def delete_item(self, **kwargs):
        key = tuple(kwargs["Key"][name] for name in self.key_names)
        item = self.items.get(key)
        if not self._condition_matches(item, kwargs):
            raise ConditionalCheckFailedException()
        self.items.pop(key)
        self.write_calls.append(("delete", kwargs))

    def update_item(self, **kwargs):
        key = tuple(kwargs["Key"][name] for name in self.key_names)
        item = self.items.get(key)
        if not self._condition_matches(item, kwargs):
            raise ConditionalCheckFailedException()
        item["expires_at_epoch"] = kwargs["ExpressionAttributeValues"][
            ":expires_at_epoch"
        ]
        self.write_calls.append(("update", kwargs))


class TransactionClientStub:
    def __init__(self, alerts: TableStub, core: TableStub):
        self.alerts = alerts
        self.core = core
        self.calls: list[list[dict]] = []

    @staticmethod
    def _decode_key(raw: dict) -> dict:
        deserializer = TypeDeserializer()
        return {key: deserializer.deserialize(value) for key, value in raw.items()}

    def transact_write_items(self, *, TransactItems):
        self.calls.append(TransactItems)
        body_key = self._decode_key(TransactItems[0]["ConditionCheck"]["Key"])
        body_identity = tuple(body_key[name] for name in self.alerts.key_names)
        alias_delete = TransactItems[1]["Delete"]
        alias_key = self._decode_key(alias_delete["Key"])
        alias_identity = tuple(alias_key[name] for name in self.core.key_names)
        alias_item = self.core.items.get(alias_identity)
        expected_event_key = TypeDeserializer().deserialize(
            alias_delete["ExpressionAttributeValues"][":event_key"]
        )
        if body_identity in self.alerts.items:
            raise TransactionCanceledException()
        if not alias_item or alias_item.get("event_key") != expected_event_key:
            raise TransactionCanceledException()
        self.core.items.pop(alias_identity)


def target() -> RetentionTarget:
    return RetentionTarget(
        region="ap-northeast-2",
        alerts_table="hearo-alerts-prod",
        core_table="hearo-core-prod",
        alerts_table_arn="arn:aws:dynamodb:ap-northeast-2:123:table/hearo-alerts-prod",
        core_table_arn="arn:aws:dynamodb:ap-northeast-2:123:table/hearo-core-prod",
    )


def body(household: str, event_id: str, timestamp: str, **extra) -> dict:
    return {
        "household_id": household,
        "event_id": event_id,
        "timestamp": timestamp,
        "event_key": f"{timestamp}#{event_id}",
        "source_device_id": "rpi-001",
        "location": "거실",
        "sound": "아기울음소리",
        "type": "Urgent",
        **extra,
    }


def alias(item: dict, **extra) -> dict:
    return {
        "pk": f"ALERTID#{item['household_id']}#{item['event_id']}",
        "sk": "ALERT",
        "event_key": item["event_key"],
        **extra,
    }


def retention_tables(now: datetime) -> tuple[TableStub, TableStub, dict[str, dict]]:
    active_time = iso_utc(now - timedelta(days=1))
    expired_time = iso_utc(now - timedelta(days=90))
    partial_time = iso_utc(now - timedelta(days=91))
    future_time = iso_utc(now + timedelta(minutes=2))
    active = body(
        "home-1",
        "active",
        active_time,
        confidence=Decimal("0.687"),
        expires_at_epoch=alert_expiry_epoch(active_time) + 3600,
    )
    expired = body("home-1", "expired", expired_time)
    partial = body("home-2", "partial", partial_time)
    future = body("home-1", "future", future_time)
    invalid = body("home-1", "invalid", "bad-clock")
    alert_table = TableStub(
        [[active, expired, future], [partial, invalid]],
        key_names=("household_id", "event_key"),
    )
    core_table = TableStub(
        [
            [
                alias(active),
                alias(expired),
                alias(future),
                {"pk": "USER#owner", "sk": "PROFILE"},
            ],
            [
                alias(invalid),
                {
                    "pk": "ALERTID#home-orphan#event-orphan",
                    "sk": "ALERT",
                    "event_key": (
                        f"{iso_utc(now - timedelta(days=91))}#event-orphan"
                    ),
                },
                {"pk": "TOKEN#refresh", "sk": "REFRESH"},
                {"pk": "KIT#serial", "sk": "META"},
            ],
        ],
        key_names=("pk", "sk"),
    )
    return alert_table, core_table, {
        "active": active,
        "expired": expired,
        "partial": partial,
        "future": future,
        "invalid": invalid,
    }


def test_migration_plan_is_paginated_scoped_typed_and_safe_to_rerun(tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    alerts, core, values = retention_tables(now)

    plan = build_retention_plan(alerts, core, target(), mode="migrate", now=now)

    assert len(alerts.scan_calls) == 2
    assert len(core.scan_calls) == 2
    assert "FilterExpression" in core.scan_calls[0]
    assert plan.report["skipped_future_timestamp"] == 1
    assert plan.report["skipped_invalid_timestamp"] == 1
    assert plan.report["expired_alerts"] == 2
    assert plan.report["orphan_aliases"] == 1
    assert plan.report["ttl_updates"] == 2
    assert len(plan.actions) == 6
    assert not any(
        action["key"].get("pk", "").startswith(("USER#", "TOKEN#", "KIT#"))
        for action in plan.actions
    )

    active_update = next(
        action
        for action in plan.actions
        if action["table"] == "alerts" and action["operation"] == "set_ttl"
    )
    assert active_update["expires_at_epoch"] == alert_expiry_epoch(
        values["active"]["timestamp"]
    )
    assert active_update["before"]["confidence"] == {"N": "0.687"}
    deserializer = TypeDeserializer()
    restored = {
        key: deserializer.deserialize(value)
        for key, value in active_update["before"].items()
    }
    assert restored["confidence"] == Decimal("0.687")
    assert not isinstance(restored["confidence"], str)

    plan_path = tmp_path / "alert-retention-plan.json"
    write_plan_file(plan, plan_path)
    assert stat.S_IMODE(plan_path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        write_plan_file(plan, plan_path)
    loaded = load_plan_file(plan_path)
    assert loaded.as_dict() == plan.as_dict()

    transaction_client = TransactionClientStub(alerts, core)
    first = apply_retention_plan(loaded, alerts, core, transaction_client)
    assert first == {"applied": 6, "conditional_skips": 0}
    assert alerts.items[
        (values["active"]["household_id"], values["active"]["event_key"])
    ]["expires_at_epoch"] == alert_expiry_epoch(values["active"]["timestamp"])
    assert (
        values["expired"]["household_id"],
        values["expired"]["event_key"],
    ) not in alerts.items
    assert (
        values["partial"]["household_id"],
        values["partial"]["event_key"],
    ) not in alerts.items
    assert ("ALERTID#home-orphan#event-orphan", "ALERT") not in core.items
    assert ("USER#owner", "PROFILE") in core.items
    assert ("TOKEN#refresh", "REFRESH") in core.items
    assert ("KIT#serial", "META") in core.items

    second = apply_retention_plan(loaded, alerts, core, transaction_client)
    assert second == {"applied": 0, "conditional_skips": 6}


def test_purge_mode_does_not_backfill_active_or_future_items():
    now = datetime.now(UTC).replace(microsecond=0)
    alerts, core, _ = retention_tables(now)
    plan = build_retention_plan(alerts, core, target(), mode="purge", now=now)

    assert all(action["operation"] == "delete" for action in plan.actions)
    assert plan.report["ttl_updates"] == 0
    assert plan.report["skipped_future_timestamp"] == 1
    assert plan.report["skipped_invalid_timestamp"] == 1
    assert plan.report["orphan_aliases"] == 0
    assert plan.report["skipped_orphan_alias"] == 1


@pytest.mark.parametrize("prefix", ["USER#owner", "TOKEN#secret", "KIT#serial"])
def test_apply_rejects_out_of_scope_core_namespaces(prefix):
    now = datetime.now(UTC).replace(microsecond=0)
    serializer = TypeSerializer()
    malicious = RetentionPlan(
        mode="purge",
        target=target(),
        generated_at=iso_utc(now),
        cutoff_epoch=int(now.timestamp()),
        actions=[
            {
                "operation": "delete",
                "table": "core",
                "key": {"pk": prefix, "sk": "META"},
                "reason": "orphan_alert_alias",
                "before": {
                    "pk": serializer.serialize(prefix),
                    "sk": serializer.serialize("META"),
                },
            }
        ],
        report={},
    )
    alerts = TableStub([[]], key_names=("household_id", "event_key"))
    core = TableStub([[]], key_names=("pk", "sk"))

    with pytest.raises(RetentionPlanError):
        apply_retention_plan(malicious, alerts, core)
    assert core.write_calls == []


def test_apply_rejects_delete_of_unexpired_alert_even_with_valid_namespace():
    now = datetime.now(UTC).replace(microsecond=0)
    timestamp = iso_utc(now - timedelta(days=1))
    item = body("home-1", "still-active", timestamp)
    serializer = TypeSerializer()
    plan = RetentionPlan(
        mode="purge",
        target=target(),
        generated_at=iso_utc(now),
        cutoff_epoch=int(now.timestamp()),
        actions=[
            {
                "operation": "delete",
                "table": "alerts",
                "key": {
                    "household_id": item["household_id"],
                    "event_key": item["event_key"],
                },
                "reason": "expired_alert",
                "expected_timestamp": timestamp,
                "before": {
                    key: serializer.serialize(value) for key, value in item.items()
                },
            }
        ],
        report={},
    )
    alerts = TableStub([[item]], key_names=("household_id", "event_key"))
    core = TableStub([[]], key_names=("pk", "sk"))

    with pytest.raises(RetentionPlanError):
        apply_retention_plan(plan, alerts, core)
    assert alerts.write_calls == []


def test_orphan_alias_transaction_rechecks_body_and_closes_scan_race():
    now = datetime.now(UTC).replace(microsecond=0)
    timestamp = iso_utc(now - timedelta(days=91))
    newly_created = body("home-race", "new-event", timestamp)
    orphan = alias(newly_created)
    alerts = TableStub([[]], key_names=("household_id", "event_key"))
    core = TableStub([[orphan]], key_names=("pk", "sk"))
    plan = build_retention_plan(alerts, core, target(), mode="migrate", now=now)
    assert plan.report["orphan_aliases"] == 1

    # Simulate the alert transaction becoming visible after the alerts scan but
    # before the core scan/apply. The transaction condition must preserve its alias.
    body_identity = (newly_created["household_id"], newly_created["event_key"])
    alerts.items[body_identity] = newly_created
    client = TransactionClientStub(alerts, core)
    result = apply_retention_plan(plan, alerts, core, client)

    assert result == {"applied": 0, "conditional_skips": 1}
    assert (orphan["pk"], orphan["sk"]) in core.items


def test_migration_preserves_recent_orphan_and_rejects_a_forged_delete_plan():
    now = datetime.now(UTC).replace(microsecond=0)
    timestamp = iso_utc(now - timedelta(days=2))
    missing_body = body("home-recent", "recent-event", timestamp)
    recent_orphan = alias(missing_body)
    alerts = TableStub([[]], key_names=("household_id", "event_key"))
    core = TableStub([[recent_orphan]], key_names=("pk", "sk"))

    preview = build_retention_plan(alerts, core, target(), mode="migrate", now=now)

    assert preview.report["orphan_aliases"] == 0
    assert preview.report["skipped_orphan_alias"] == 1
    assert preview.actions == []
    assert (recent_orphan["pk"], recent_orphan["sk"]) in core.items

    serializer = TypeSerializer()
    forged = RetentionPlan(
        mode="migrate",
        target=target(),
        generated_at=iso_utc(now),
        cutoff_epoch=int(now.timestamp()),
        actions=[
            {
                "operation": "delete",
                "table": "core",
                "key": {"pk": recent_orphan["pk"], "sk": recent_orphan["sk"]},
                "reason": "orphan_alert_alias",
                "before": {
                    key: serializer.serialize(value)
                    for key, value in recent_orphan.items()
                },
                "expected_event_key": recent_orphan["event_key"],
                "expected_body_key": {
                    "household_id": missing_body["household_id"],
                    "event_key": missing_body["event_key"],
                },
            }
        ],
        report={},
    )
    client = TransactionClientStub(alerts, core)

    with pytest.raises(RetentionPlanError, match="unexpired orphan"):
        apply_retention_plan(forged, alerts, core, client)
    assert client.calls == []
    assert (recent_orphan["pk"], recent_orphan["sk"]) in core.items


def test_scheduled_purge_only_deletes_a_versioned_expired_orphan_transactionally():
    now = datetime.now(UTC).replace(microsecond=0)
    timestamp = iso_utc(now - timedelta(days=91))
    missing_body = body("home-old", "old-event", timestamp)
    expired_orphan = alias(
        missing_body,
        expires_at_epoch=alert_expiry_epoch(timestamp),
    )
    alerts = TableStub([[]], key_names=("household_id", "event_key"))
    core = TableStub([[expired_orphan]], key_names=("pk", "sk"))
    plan = build_retention_plan(alerts, core, target(), mode="purge", now=now)

    assert plan.report["orphan_aliases"] == 1
    assert len(plan.actions) == 1
    client = TransactionClientStub(alerts, core)
    result = apply_retention_plan(plan, alerts, core, client)

    assert result == {"applied": 1, "conditional_skips": 0}
    delete = client.calls[0][1]["Delete"]
    assert "expires_at_epoch <= :cutoff" in delete["ConditionExpression"]
    assert ":cutoff" in delete["ExpressionAttributeValues"]
    assert (expired_orphan["pk"], expired_orphan["sk"]) not in core.items


def test_all_actions_are_validated_before_the_first_write():
    now = datetime.now(UTC).replace(microsecond=0)
    expired_timestamp = iso_utc(now - timedelta(days=91))
    expired = body("home-1", "expired-valid", expired_timestamp)
    serializer = TypeSerializer()
    valid_action = {
        "operation": "delete",
        "table": "alerts",
        "key": {
            "household_id": expired["household_id"],
            "event_key": expired["event_key"],
        },
        "reason": "expired_alert",
        "expected_timestamp": expired_timestamp,
        "before": {
            key: serializer.serialize(value) for key, value in expired.items()
        },
    }
    invalid_later_action = {
        "operation": "delete",
        "table": "core",
        "key": {"pk": "USER#must-not-delete", "sk": "PROFILE"},
        "reason": "orphan_alert_alias",
        "before": {
            "pk": serializer.serialize("USER#must-not-delete"),
            "sk": serializer.serialize("PROFILE"),
        },
    }
    plan = RetentionPlan(
        mode="purge",
        target=target(),
        generated_at=iso_utc(now),
        cutoff_epoch=int(now.timestamp()),
        actions=[valid_action, invalid_later_action],
        report={},
    )
    alerts = TableStub([[expired]], key_names=("household_id", "event_key"))
    core = TableStub([[]], key_names=("pk", "sk"))

    with pytest.raises(RetentionPlanError):
        apply_retention_plan(plan, alerts, core)
    assert alerts.write_calls == []
    assert (expired["household_id"], expired["event_key"]) in alerts.items


def test_apply_rejects_future_cutoff_and_ttl_updates_in_purge_plan():
    now = datetime.now(UTC).replace(microsecond=0)
    timestamp = iso_utc(now - timedelta(days=1))
    active = body("home-1", "active", timestamp)
    serializer = TypeSerializer()
    action = {
        "operation": "set_ttl",
        "table": "alerts",
        "key": {
            "household_id": active["household_id"],
            "event_key": active["event_key"],
        },
        "reason": "alert_ttl_backfill",
        "expected_timestamp": timestamp,
        "expires_at_epoch": alert_expiry_epoch(timestamp),
        "before": {
            key: serializer.serialize(value) for key, value in active.items()
        },
    }
    alerts = TableStub([[active]], key_names=("household_id", "event_key"))
    core = TableStub([[]], key_names=("pk", "sk"))
    future_cutoff = RetentionPlan(
        mode="migrate",
        target=target(),
        generated_at=iso_utc(now + timedelta(hours=1)),
        cutoff_epoch=int((now + timedelta(hours=1)).timestamp()),
        actions=[action],
        report={},
    )
    with pytest.raises(RetentionPlanError, match="future"):
        apply_retention_plan(future_cutoff, alerts, core)

    purge_with_update = RetentionPlan(
        mode="purge",
        target=target(),
        generated_at=iso_utc(now),
        cutoff_epoch=int(now.timestamp()),
        actions=[action],
        report={},
    )
    with pytest.raises(RetentionPlanError, match="purge"):
        apply_retention_plan(purge_with_update, alerts, core)
    assert alerts.write_calls == []


def test_cli_defaults_to_dry_run_plan_and_scheduled_apply_is_strict(tmp_path):
    argument_parser = parser()
    base = [
        "--region",
        "ap-northeast-2",
        "--alerts-table",
        "alerts",
        "--core-table",
        "core",
    ]
    preview = argument_parser.parse_args(
        [*base, "--plan-file", str(tmp_path / "plan.json")]
    )
    _validate_mode_arguments(preview, argument_parser)
    assert preview.apply is False
    assert preview.mode == "migrate"

    with pytest.raises(SystemExit):
        missing_plan = argument_parser.parse_args(base)
        _validate_mode_arguments(missing_plan, argument_parser)
    with pytest.raises(SystemExit):
        scheduled_dry_run = argument_parser.parse_args(
            [*base, "--mode", "purge", "--scheduled"]
        )
        _validate_mode_arguments(scheduled_dry_run, argument_parser)

    confirmations = [
        "--confirm-region",
        "ap-northeast-2",
        "--confirm-alerts-table",
        "alerts",
        "--confirm-core-table",
        "core",
        "--confirm-alerts-table-arn",
        "arn:alerts",
        "--confirm-core-table-arn",
        "arn:core",
    ]
    scheduled = argument_parser.parse_args(
        [*base, "--mode", "purge", "--scheduled", "--apply", *confirmations]
    )
    _validate_mode_arguments(scheduled, argument_parser)
    assert scheduled.plan_file is None
