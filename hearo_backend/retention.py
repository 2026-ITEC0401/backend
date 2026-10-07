from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import stat
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from .domain import parse_timestamp


ALERT_RETENTION_DAYS = 90
ALERT_RETENTION_SECONDS = ALERT_RETENTION_DAYS * 24 * 60 * 60
RETENTION_PLAN_VERSION = 1


class AlertTimestampError(ValueError):
    """The source event timestamp cannot safely define a retention boundary."""


class RetentionPlanError(ValueError):
    """A retention plan is malformed, unsafe, or does not match its target."""


def utc_now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def alert_expiry_at(timestamp: str) -> datetime:
    if not isinstance(timestamp, str) or not timestamp.strip():
        raise AlertTimestampError("alert timestamp must be a non-empty ISO 8601 string")
    try:
        occurred_at = parse_timestamp(timestamp)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AlertTimestampError("alert timestamp must be a valid ISO 8601 value") from exc
    try:
        return occurred_at + timedelta(seconds=ALERT_RETENTION_SECONDS)
    except (OverflowError, OSError, ValueError) as exc:
        raise AlertTimestampError("alert timestamp is outside the supported range") from exc


def alert_expiry_epoch(timestamp: str) -> int:
    """Return a whole-second TTL that never shortens the logical 90-day period."""

    try:
        return math.ceil(alert_expiry_at(timestamp).timestamp())
    except (OverflowError, OSError, ValueError) as exc:
        raise AlertTimestampError("alert timestamp is outside the supported range") from exc


def current_epoch(now: datetime | None = None) -> int:
    return int(_utc(now or utc_now()).timestamp())


def is_expired_timestamp(timestamp: str, now: datetime | None = None) -> bool:
    return alert_expiry_at(timestamp) <= _utc(now or utc_now())


def is_visible_alert(alert: Any, now: datetime | None = None) -> bool:
    """Use occurrence time as the authority, including for records predating TTL."""

    try:
        return not is_expired_timestamp(alert.timestamp, now)
    except (AlertTimestampError, AttributeError):
        # A malformed legacy record must not escape the retention boundary.
        return False


@dataclass(frozen=True)
class RetentionTarget:
    region: str
    alerts_table: str
    core_table: str
    alerts_table_arn: str
    core_table_arn: str


@dataclass
class RetentionPlan:
    mode: Literal["purge", "migrate"]
    target: RetentionTarget
    generated_at: str
    cutoff_epoch: int
    actions: list[dict[str, Any]]
    report: dict[str, Any]
    version: int = RETENTION_PLAN_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "mode": self.mode,
            "target": asdict(self.target),
            "generated_at": self.generated_at,
            "cutoff_epoch": self.cutoff_epoch,
            "actions": self.actions,
            "report": self.report,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RetentionPlan:
        try:
            version = value["version"]
            mode = value["mode"]
            target = RetentionTarget(**value["target"])
            generated_at = value["generated_at"]
            cutoff_epoch = value["cutoff_epoch"]
            actions = value["actions"]
            report = value["report"]
        except (KeyError, TypeError, ValueError) as exc:
            raise RetentionPlanError("retention plan has an invalid structure") from exc
        if isinstance(version, bool) or version != RETENTION_PLAN_VERSION:
            raise RetentionPlanError("unsupported retention plan version")
        if mode not in {"purge", "migrate"}:
            raise RetentionPlanError("retention plan mode is invalid")
        if (
            isinstance(cutoff_epoch, bool)
            or not isinstance(cutoff_epoch, int)
            or cutoff_epoch < 0
        ):
            raise RetentionPlanError("retention plan cutoff is invalid")
        if not isinstance(generated_at, str):
            raise RetentionPlanError("retention plan generation time is invalid")
        try:
            generated_epoch = current_epoch(parse_timestamp(generated_at))
        except (TypeError, ValueError, OverflowError) as exc:
            raise RetentionPlanError("retention plan generation time is invalid") from exc
        if generated_epoch != cutoff_epoch:
            raise RetentionPlanError(
                "retention plan cutoff does not match its generation time"
            )
        if not all(
            isinstance(item, str) and item for item in asdict(target).values()
        ):
            raise RetentionPlanError("retention plan target is invalid")
        if not isinstance(actions, list) or not isinstance(report, dict):
            raise RetentionPlanError("retention plan actions or report are invalid")
        return cls(
            mode=mode,
            target=target,
            generated_at=generated_at,
            cutoff_epoch=cutoff_epoch,
            actions=actions,
            report=report,
            version=version,
        )


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else str(value)
    if isinstance(value, bytes):
        return {"base16": value.hex()}
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _dynamodb_backup_image(item: dict[str, Any]) -> dict[str, Any]:
    """Preserve DynamoDB scalar types so a reviewed plan is also a faithful backup."""

    from boto3.dynamodb.types import TypeSerializer

    serializer = TypeSerializer()

    def encode_binary(value: Any) -> Any:
        if isinstance(value, bytes):
            return base64.b64encode(value).decode("ascii")
        if isinstance(value, dict):
            return {key: encode_binary(child) for key, child in value.items()}
        if isinstance(value, list):
            return [encode_binary(child) for child in value]
        return value

    try:
        return {
            key: encode_binary(serializer.serialize(value))
            for key, value in item.items()
        }
    except (TypeError, ValueError) as exc:
        raise RetentionPlanError("DynamoDB item could not be encoded for backup") from exc


def _scan_all(table: Any, **initial: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    kwargs = {"ConsistentRead": True, **initial}
    while True:
        response = table.scan(**kwargs)
        page = response.get("Items", [])
        if not isinstance(page, list):
            raise RetentionPlanError("DynamoDB scan returned an invalid Items value")
        items.extend(page)
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return items
        kwargs["ExclusiveStartKey"] = last_key


def _ttl_matches(item: dict[str, Any], expected: int) -> bool:
    value = item.get("expires_at_epoch")
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        return False
    try:
        return int(value) == expected and Decimal(str(value)) == Decimal(expected)
    except (ArithmeticError, TypeError, ValueError):
        return False


def _alert_key(item: dict[str, Any]) -> dict[str, str] | None:
    household_id = item.get("household_id")
    event_key = item.get("event_key")
    if not isinstance(household_id, str) or not household_id:
        return None
    if not isinstance(event_key, str) or not event_key:
        return None
    return {"household_id": household_id, "event_key": event_key}


def _alias_key(item: dict[str, Any]) -> dict[str, str] | None:
    pk = item.get("pk")
    sk = item.get("sk")
    if not isinstance(pk, str) or not pk.startswith("ALERTID#") or pk == "ALERTID#":
        return None
    if sk != "ALERT":
        return None
    return {"pk": pk, "sk": "ALERT"}


def _expected_alias_key(item: dict[str, Any]) -> tuple[str, str] | None:
    household_id = item.get("household_id")
    event_id = item.get("event_id")
    if not isinstance(household_id, str) or not household_id:
        return None
    if not isinstance(event_id, str) or not event_id:
        return None
    return (f"ALERTID#{household_id}#{event_id}", "ALERT")


def _orphan_body_key(alias: dict[str, Any]) -> tuple[dict[str, str], datetime] | None:
    key = _alias_key(alias)
    event_key = alias.get("event_key")
    if key is None or not isinstance(event_key, str) or "#" not in event_key:
        return None
    timestamp, event_id = event_key.split("#", 1)
    if not event_id:
        return None
    try:
        occurred_at = parse_timestamp(timestamp)
        alert_expiry_epoch(timestamp)
    except (AlertTimestampError, TypeError, ValueError, OverflowError):
        return None
    alias_identity = key["pk"][len("ALERTID#") :]
    event_suffix = f"#{event_id}"
    if not alias_identity.endswith(event_suffix):
        return None
    household_id = alias_identity[: -len(event_suffix)]
    if not household_id:
        return None
    return {"household_id": household_id, "event_key": event_key}, occurred_at


def _ttl_epoch(item: dict[str, Any]) -> int | None:
    value = item.get("expires_at_epoch")
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        return None
    try:
        numeric = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None
    if numeric != numeric.to_integral_value():
        return None
    return int(numeric)


def _action(
    operation: Literal["delete", "set_ttl"],
    table: Literal["alerts", "core"],
    key: dict[str, str],
    item: dict[str, Any],
    reason: str,
    *,
    expires_at_epoch: int | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "operation": operation,
        "table": table,
        "key": key,
        "reason": reason,
        "before": _dynamodb_backup_image(item),
    }
    if expires_at_epoch is not None:
        value["expires_at_epoch"] = expires_at_epoch
    timestamp = item.get("timestamp")
    event_key = item.get("event_key")
    if isinstance(timestamp, str):
        value["expected_timestamp"] = timestamp
    if isinstance(event_key, str):
        value["expected_event_key"] = event_key
    return value


def build_retention_plan(
    alerts_table: Any,
    core_table: Any,
    target: RetentionTarget,
    *,
    mode: Literal["purge", "migrate"] = "migrate",
    now: datetime | None = None,
) -> RetentionPlan:
    """Inspect both tables and build a replayable, non-mutating retention plan."""

    if mode not in {"purge", "migrate"}:
        raise ValueError("mode must be purge or migrate")
    from boto3.dynamodb.conditions import Attr

    current = _utc(now or utc_now())
    cutoff_epoch = current_epoch(current)
    alert_items = _scan_all(alerts_table)
    alias_candidates = _scan_all(
        core_table,
        FilterExpression=Attr("pk").begins_with("ALERTID#"),
    )
    aliases: dict[tuple[str, str], dict[str, Any]] = {}
    report: dict[str, Any] = {
        "scanned_alerts": len(alert_items),
        "scanned_alias_candidates": len(alias_candidates),
        "expired_alerts": 0,
        "ttl_updates": 0,
        "orphan_aliases": 0,
        "skipped_invalid_timestamp": 0,
        "skipped_future_timestamp": 0,
        "skipped_malformed_item": 0,
        "skipped_alias_mismatch": 0,
        "skipped_orphan_alias": 0,
        "issues": [],
    }
    for item in alias_candidates:
        key = _alias_key(item)
        if key is None:
            report["skipped_malformed_item"] += 1
            report["issues"].append({"kind": "malformed_alias_key"})
            continue
        aliases[(key["pk"], key["sk"])] = item

    actions: list[dict[str, Any]] = []
    body_alias_keys: set[tuple[str, str]] = set()
    for item in alert_items:
        key = _alert_key(item)
        expected_alias = _expected_alias_key(item)
        if expected_alias is not None:
            # Even a malformed body proves that this alias is not an orphan. The
            # body is reported and skipped below rather than risking its reservation.
            body_alias_keys.add(expected_alias)
        if key is None or expected_alias is None:
            report["skipped_malformed_item"] += 1
            report["issues"].append(
                {"kind": "malformed_alert_key", "key": _json_safe(key or {})}
            )
            continue
        timestamp = item.get("timestamp")
        try:
            if not isinstance(timestamp, str):
                raise AlertTimestampError("timestamp is missing")
            occurred_at = parse_timestamp(timestamp)
            expires_at_epoch = alert_expiry_epoch(timestamp)
        except (AlertTimestampError, TypeError, ValueError, OverflowError):
            report["skipped_invalid_timestamp"] += 1
            report["issues"].append({"kind": "invalid_timestamp", "key": key})
            continue
        if occurred_at > current:
            # A future source clock may be valid after NTP convergence. Do not mutate it
            # in an offline migration; the ingest boundary is enforced separately.
            report["skipped_future_timestamp"] += 1
            report["issues"].append({"kind": "future_timestamp", "key": key})
            continue

        alias = aliases.get(expected_alias)
        matching_alias = bool(alias and alias.get("event_key") == item.get("event_key"))
        if alias and not matching_alias:
            report["skipped_alias_mismatch"] += 1
            report["issues"].append(
                {
                    "kind": "alias_event_key_mismatch",
                    "key": {"pk": expected_alias[0], "sk": expected_alias[1]},
                }
            )

        if is_expired_timestamp(timestamp, current):
            report["expired_alerts"] += 1
            actions.append(_action("delete", "alerts", key, item, "expired_alert"))
            if matching_alias and alias is not None:
                actions.append(
                    _action(
                        "delete",
                        "core",
                        {"pk": expected_alias[0], "sk": expected_alias[1]},
                        alias,
                        "expired_alert_alias",
                    )
                )
            continue

        if mode == "migrate" and not _ttl_matches(item, expires_at_epoch):
            report["ttl_updates"] += 1
            actions.append(
                _action(
                    "set_ttl",
                    "alerts",
                    key,
                    item,
                    "alert_ttl_backfill",
                    expires_at_epoch=expires_at_epoch,
                )
            )
        if (
            mode == "migrate"
            and matching_alias
            and alias is not None
            and not _ttl_matches(alias, expires_at_epoch)
        ):
            report["ttl_updates"] += 1
            actions.append(
                _action(
                    "set_ttl",
                    "core",
                    {"pk": expected_alias[0], "sk": expected_alias[1]},
                    alias,
                    "alert_alias_ttl_backfill",
                    expires_at_epoch=expires_at_epoch,
                )
            )

    # Only aliases with the exact reserved key shape and no corresponding body are
    # eligible. USER/TOKEN/INVITE/device/kit records can never enter this action set.
    for alias_key, alias in aliases.items():
        if alias_key in body_alias_keys:
            continue
        body_reference = _orphan_body_key(alias)
        if body_reference is None:
            event_key = alias.get("event_key")
            if isinstance(event_key, str) and "#" in event_key:
                try:
                    parse_timestamp(event_key.split("#", 1)[0])
                except (TypeError, ValueError, OverflowError):
                    report["skipped_invalid_timestamp"] += 1
            report["skipped_orphan_alias"] += 1
            report["issues"].append(
                {
                    "kind": "orphan_alias_identity_invalid",
                    "key": {"pk": alias_key[0], "sk": alias_key[1]},
                }
            )
            continue
        body_key, occurred_at = body_reference
        if occurred_at > current:
            report["skipped_future_timestamp"] += 1
            report["skipped_orphan_alias"] += 1
            report["issues"].append(
                {"kind": "future_orphan_alias", "key": body_key}
            )
            continue
        orphan_timestamp = body_key["event_key"].split("#", 1)[0]
        if not is_expired_timestamp(orphan_timestamp, current):
            report["skipped_orphan_alias"] += 1
            report["issues"].append(
                {"kind": "unexpired_orphan_alias", "key": body_key}
            )
            continue
        if mode == "purge":
            stored_expiry = _ttl_epoch(alias)
            if (
                stored_expiry is None
                or stored_expiry > cutoff_epoch
            ):
                report["skipped_orphan_alias"] += 1
                report["issues"].append(
                    {"kind": "unexpired_or_unversioned_orphan_alias", "key": body_key}
                )
                continue
        report["orphan_aliases"] += 1
        orphan_action = _action(
            "delete",
            "core",
            {"pk": alias_key[0], "sk": alias_key[1]},
            alias,
            "orphan_alert_alias",
        )
        orphan_action["expected_body_key"] = body_key
        actions.append(orphan_action)

    report["action_count"] = len(actions)
    return RetentionPlan(
        mode=mode,
        target=target,
        generated_at=current.isoformat().replace("+00:00", "Z"),
        cutoff_epoch=cutoff_epoch,
        actions=actions,
        report=report,
    )


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def write_plan_file(plan: RetentionPlan, path: str | os.PathLike[str]) -> None:
    payload = plan.as_dict()
    envelope = {
        "sha256": hashlib.sha256(_canonical_json(payload)).hexdigest(),
        "plan": payload,
    }
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    file_descriptor = os.open(os.fspath(path), flags, 0o600)
    try:
        with os.fdopen(file_descriptor, "wb") as stream:
            stream.write(_canonical_json(envelope))
            stream.write(b"\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            os.close(file_descriptor)
        except OSError:
            pass
        raise


def load_plan_file(path: str | os.PathLike[str]) -> RetentionPlan:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        file_descriptor = os.open(os.fspath(path), flags)
        with os.fdopen(file_descriptor, "r", encoding="utf-8") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise RetentionPlanError("retention plan must be a regular file")
            if stat.S_IMODE(metadata.st_mode) & 0o077:
                raise RetentionPlanError(
                    "retention plan permissions must be 0600 or stricter"
                )
            envelope = json.load(stream)
        payload = envelope["plan"]
        checksum = envelope["sha256"]
    except (KeyError, OSError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise RetentionPlanError("retention plan file is invalid") from exc
    if not isinstance(payload, dict) or not isinstance(checksum, str):
        raise RetentionPlanError("retention plan envelope is invalid")
    expected = hashlib.sha256(_canonical_json(payload)).hexdigest()
    if not hmac.compare_digest(checksum, expected):
        raise RetentionPlanError("retention plan checksum does not match")
    return RetentionPlan.from_dict(payload)


def validate_plan_target(plan: RetentionPlan, actual: RetentionTarget) -> None:
    if plan.target != actual:
        raise RetentionPlanError("retention plan target does not match the live DynamoDB target")


def _conditional_failure(exc: BaseException) -> bool:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code")
        if code == "ConditionalCheckFailedException":
            return True
        if code == "TransactionCanceledException":
            return any(
                reason.get("Code") == "ConditionalCheckFailed"
                for reason in response.get("CancellationReasons", [])
                if isinstance(reason, dict)
            )
    return exc.__class__.__name__ == "ConditionalCheckFailedException"

def _backup_string(before: dict[str, Any], field: str) -> str | None:
    value = before.get(field)
    if not isinstance(value, dict) or set(value) != {"S"}:
        return None
    return value["S"] if isinstance(value["S"], str) else None


def _backup_integer(before: dict[str, Any], field: str) -> int | None:
    value = before.get(field)
    if not isinstance(value, dict) or set(value) != {"N"}:
        return None
    try:
        numeric = Decimal(value["N"])
    except (ArithmeticError, TypeError, ValueError):
        return None
    if numeric != numeric.to_integral_value():
        return None
    return int(numeric)


def _validate_action(
    action: dict[str, Any],
    cutoff_epoch: int,
    cutoff_at: datetime,
    mode: Literal["purge", "migrate"],
) -> None:
    operation = action.get("operation")
    table = action.get("table")
    key = action.get("key")
    if operation not in {"delete", "set_ttl"} or table not in {"alerts", "core"}:
        raise RetentionPlanError("retention plan contains an unsupported action")
    allowed_shapes = {
        ("alerts", "delete", "expired_alert"),
        ("alerts", "set_ttl", "alert_ttl_backfill"),
        ("core", "delete", "expired_alert_alias"),
        ("core", "delete", "orphan_alert_alias"),
        ("core", "set_ttl", "alert_alias_ttl_backfill"),
    }
    if (table, operation, action.get("reason")) not in allowed_shapes:
        raise RetentionPlanError("retention plan action has an invalid scope or reason")
    before = action.get("before")
    if not isinstance(before, dict):
        raise RetentionPlanError("retention plan action is missing its typed backup image")
    if not isinstance(key, dict):
        raise RetentionPlanError("retention plan action key is invalid")
    if table == "alerts":
        if set(key) != {"household_id", "event_key"} or not all(
            isinstance(key[name], str) and key[name]
            for name in ("household_id", "event_key")
        ):
            raise RetentionPlanError("retention plan contains an invalid alert key")
        if not isinstance(action.get("expected_timestamp"), str):
            raise RetentionPlanError("alert action is missing its timestamp condition")
        if not key["event_key"].startswith(action["expected_timestamp"] + "#"):
            raise RetentionPlanError("alert action key does not match its timestamp")
        if (
            _backup_string(before, "household_id") != key["household_id"]
            or _backup_string(before, "event_key") != key["event_key"]
            or _backup_string(before, "timestamp") != action["expected_timestamp"]
        ):
            raise RetentionPlanError("alert action does not match its typed backup image")
    else:
        if (
            set(key) != {"pk", "sk"}
            or not isinstance(key.get("pk"), str)
            or not key["pk"].startswith("ALERTID#")
            or key.get("sk") != "ALERT"
        ):
            raise RetentionPlanError("retention plan attempts to touch a non-alert core item")
        event_key = action.get("expected_event_key")
        reference = _orphan_body_key(
            {"pk": key["pk"], "sk": key["sk"], "event_key": event_key}
        )
        if reference is None:
            raise RetentionPlanError("alert alias key does not match its event identity")
        if (
            _backup_string(before, "pk") != key["pk"]
            or _backup_string(before, "sk") != key["sk"]
            or _backup_string(before, "event_key") != event_key
        ):
            raise RetentionPlanError("alert alias action does not match its typed backup image")
        if action.get("reason") == "orphan_alert_alias":
            body_key = action.get("expected_body_key")
            if (
                not isinstance(body_key, dict)
                or set(body_key) != {"household_id", "event_key"}
                or not all(isinstance(value, str) and value for value in body_key.values())
            ):
                raise RetentionPlanError("orphan alias action is missing its alert-body check")
            event_key = action.get("expected_event_key")
            if event_key != body_key["event_key"]:
                raise RetentionPlanError("orphan alias body check does not match its event key")
            if reference[0] != body_key:
                raise RetentionPlanError("orphan alias body check does not match its alias key")
            orphan_timestamp = body_key["event_key"].split("#", 1)[0]
            if not is_expired_timestamp(orphan_timestamp, cutoff_at):
                raise RetentionPlanError(
                    "retention plan attempts to delete an unexpired orphan alias"
                )
            if mode == "purge":
                stored_expiry = _backup_integer(before, "expires_at_epoch")
                if (
                    stored_expiry is None
                    or stored_expiry > cutoff_epoch
                ):
                    raise RetentionPlanError(
                        "purge plan contains an unexpired or unversioned orphan alias"
                    )
    if operation == "set_ttl":
        ttl = action.get("expires_at_epoch")
        if isinstance(ttl, bool) or not isinstance(ttl, int):
            raise RetentionPlanError("retention TTL update is invalid")
        if table == "alerts":
            expected_expiry = alert_expiry_epoch(action["expected_timestamp"])
        else:
            event_key = action.get("expected_event_key")
            if not isinstance(event_key, str) or "#" not in event_key:
                raise RetentionPlanError("alert alias TTL action has an invalid event key")
            expected_expiry = alert_expiry_epoch(event_key.split("#", 1)[0])
        if ttl != expected_expiry:
            raise RetentionPlanError("retention TTL does not match the fixed 90-day policy")
    if operation == "delete" and action.get("reason") in {
        "expired_alert",
        "expired_alert_alias",
    }:
        timestamp = (
            action.get("expected_timestamp")
            if table == "alerts"
            else str(action.get("expected_event_key", "")).split("#", 1)[0]
        )
        if not is_expired_timestamp(timestamp, cutoff_at):
            raise RetentionPlanError("retention plan attempts to delete an unexpired alert")


def apply_retention_plan(
    plan: RetentionPlan,
    alerts_table: Any,
    core_table: Any,
    client: Any | None = None,
) -> dict[str, int]:
    """Apply a validated plan with per-item identity conditions and rerun safety."""

    if not isinstance(plan.target, RetentionTarget) or not all(
        isinstance(item, str) and item for item in asdict(plan.target).values()
    ):
        raise RetentionPlanError("retention plan target is invalid")
    alerts_name = getattr(alerts_table, "name", None)
    core_name = getattr(core_table, "name", None)
    if alerts_name is not None and alerts_name != plan.target.alerts_table:
        raise RetentionPlanError("alerts table does not match the retention plan target")
    if core_name is not None and core_name != plan.target.core_table:
        raise RetentionPlanError("core table does not match the retention plan target")
    if not isinstance(plan.actions, list) or not isinstance(plan.report, dict):
        raise RetentionPlanError("retention plan actions or report are invalid")
    if not all(isinstance(action, dict) for action in plan.actions):
        raise RetentionPlanError("retention plan action is invalid")
    if plan.version != RETENTION_PLAN_VERSION or plan.mode not in {"purge", "migrate"}:
        raise RetentionPlanError("retention plan metadata is invalid")
    if (
        isinstance(plan.cutoff_epoch, bool)
        or not isinstance(plan.cutoff_epoch, int)
        or plan.cutoff_epoch < 0
    ):
        raise RetentionPlanError("retention plan cutoff is invalid")
    try:
        generated_at = parse_timestamp(plan.generated_at)
        generated_epoch = current_epoch(generated_at)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RetentionPlanError("retention plan generation time is invalid") from exc
    if generated_epoch != plan.cutoff_epoch:
        raise RetentionPlanError(
            "retention plan cutoff does not match its generation time"
        )
    server_now = utc_now()
    if generated_at > server_now or plan.cutoff_epoch > current_epoch(server_now):
        raise RetentionPlanError("retention plan cutoff cannot be in the future")
    if plan.mode == "purge" and any(
        action.get("operation") != "delete" for action in plan.actions
    ):
        raise RetentionPlanError("a purge plan cannot contain TTL update actions")
    seen_actions: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    for action in plan.actions:
        _validate_action(action, plan.cutoff_epoch, generated_at, plan.mode)
        identity = (
            action["table"],
            tuple(sorted(action["key"].items())),
        )
        if identity in seen_actions:
            raise RetentionPlanError("retention plan contains a duplicate action")
        seen_actions.add(identity)
    if any(action.get("reason") == "orphan_alert_alias" for action in plan.actions):
        client = client or getattr(getattr(core_table, "meta", None), "client", None)
        if client is None:
            raise RetentionPlanError("orphan alias cleanup requires a DynamoDB transaction client")

    result = {"applied": 0, "conditional_skips": 0}
    for action in plan.actions:
        table = alerts_table if action["table"] == "alerts" else core_table
        names: dict[str, str] = {}
        values: dict[str, Any] = {}
        if action["table"] == "alerts":
            names["#timestamp"] = "timestamp"
            values[":timestamp"] = action["expected_timestamp"]
            condition = "attribute_exists(household_id) AND #timestamp = :timestamp"
        else:
            condition = "attribute_exists(pk)"
            if "expected_event_key" in action:
                names["#event_key"] = "event_key"
                values[":event_key"] = action["expected_event_key"]
                condition += " AND #event_key = :event_key"
            else:
                condition += " AND attribute_not_exists(event_key)"
        kwargs: dict[str, Any] = {
            "Key": action["key"],
            "ConditionExpression": condition,
        }
        if names:
            kwargs["ExpressionAttributeNames"] = names
        if values:
            kwargs["ExpressionAttributeValues"] = values
        try:
            if action.get("reason") == "orphan_alert_alias":
                from boto3.dynamodb.types import TypeSerializer

                serializer = TypeSerializer()
                body_key = action["expected_body_key"]
                delete_values = {
                    ":event_key": serializer.serialize(action["expected_event_key"])
                }
                alias_condition = "attribute_exists(pk) AND event_key = :event_key"
                if plan.mode == "purge":
                    delete_values[":cutoff"] = serializer.serialize(plan.cutoff_epoch)
                    alias_condition += " AND expires_at_epoch <= :cutoff"
                client.transact_write_items(
                    TransactItems=[
                        {
                            "ConditionCheck": {
                                "TableName": plan.target.alerts_table,
                                "Key": {
                                    key: serializer.serialize(value)
                                    for key, value in body_key.items()
                                },
                                "ConditionExpression": "attribute_not_exists(household_id)",
                            }
                        },
                        {
                            "Delete": {
                                "TableName": plan.target.core_table,
                                "Key": {
                                    key: serializer.serialize(value)
                                    for key, value in action["key"].items()
                                },
                                "ConditionExpression": alias_condition,
                                "ExpressionAttributeValues": delete_values,
                            }
                        },
                    ]
                )
            elif action["operation"] == "delete":
                table.delete_item(**kwargs)
            else:
                kwargs["UpdateExpression"] = "SET expires_at_epoch = :expires_at_epoch"
                kwargs.setdefault("ExpressionAttributeValues", {})[
                    ":expires_at_epoch"
                ] = action["expires_at_epoch"]
                kwargs["ConditionExpression"] += (
                    " AND (attribute_not_exists(expires_at_epoch) "
                    "OR expires_at_epoch <> :expires_at_epoch)"
                )
                table.update_item(**kwargs)
            result["applied"] += 1
        except Exception as exc:
            if _conditional_failure(exc):
                result["conditional_skips"] += 1
                continue
            raise
    return result
