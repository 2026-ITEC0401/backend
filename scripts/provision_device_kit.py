#!/usr/bin/env python3
"""Issue one fixed four-device kit; default to a local, non-mutating preview.

The registration code is generated only for --apply and saved in a new private
file before the single conditional inventory transaction. The file is never
automatically removed or overwritten, even if the database result is unknown.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import stat
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from hearo_backend.device_kits import (  # noqa: E402
    hash_claim_code,
    new_claim_code,
    normalize_hardware_id,
    normalize_kit_id,
)
from hearo_backend.domain import FIXED_DEVICES  # noqa: E402


ACCOUNT = "377152274782"
REGION = "ap-south-1"
CORE_TABLES = frozenset({
    "hearo-core-v2-final",
    "hearo-core-v2-r6-staging",
    "hearo-core-v2-r7-staging",
})
AWS_ERROR_CODES = frozenset({
    "AccessDeniedException", "ResourceNotFoundException", "ValidationException",
    "ThrottlingException", "ProvisionedThroughputExceededException",
    "TransactionCanceledException", "ConditionalCheckFailedException",
    "InternalServerError", "ExpiredTokenException", "UnrecognizedClientException",
    "RequestLimitExceeded",
})


class ProvisionGuardError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ProvisionGuardError(code)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--kit-id", required=True)
    value.add_argument("--rpi-hardware-id", required=True)
    for number in (1, 2, 3):
        value.add_argument(f"--esp32-{number}-hardware-id", required=True)
    value.add_argument("--region", required=True)
    value.add_argument("--core-table", required=True)
    value.add_argument("--output-file", required=True,
                       help="new 0600 code file in an existing owner-only 0700 directory")
    value.add_argument("--apply", action="store_true")
    value.add_argument("--confirm-target", help="exact ACCOUNT/REGION/CORE_TABLE; required for --apply")
    return value


def local_plan(arguments: argparse.Namespace) -> dict:
    require(arguments.region == REGION and arguments.core_table in CORE_TABLES,
            "TARGET_NOT_ALLOWED")
    try:
        kit_id = normalize_kit_id(arguments.kit_id)
        physical = [
            normalize_hardware_id(arguments.rpi_hardware_id),
            *[normalize_hardware_id(getattr(arguments, f"esp32_{n}_hardware_id")) for n in (1, 2, 3)],
        ]
    except ValueError as exc:
        raise ProvisionGuardError("INVALID_KIT_OR_HARDWARE_ID") from exc
    require(len(set(physical)) == 4, "DUPLICATE_HARDWARE_ID")
    devices = [
        {"device_id": device_id, "device_type": kind, "hardware_id": hardware_id}
        for (device_id, _location, kind), hardware_id in zip(FIXED_DEVICES, physical, strict=True)
    ]
    target = f"{ACCOUNT}/{arguments.region}/{arguments.core_table}"
    if arguments.apply:
        require(arguments.confirm_target == target, "EXACT_TARGET_CONFIRMATION_REQUIRED")
    return {
        "kit_id": kit_id, "devices": devices,
        "target": {"account": ACCOUNT, "region": arguments.region,
                   "core_table": arguments.core_table,
                   "core_table_arn": f"arn:aws:dynamodb:{arguments.region}:{ACCOUNT}:table/{arguments.core_table}"},
        "confirmation": target,
    }


def private_output_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser().absolute()
    parent = path.parent
    try:
        info = parent.lstat()
    except OSError as exc:
        raise ProvisionGuardError("OUTPUT_DIRECTORY_UNAVAILABLE") from exc
    require(stat.S_ISDIR(info.st_mode) and not parent.is_symlink()
            and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700,
            "OUTPUT_DIRECTORY_NOT_PRIVATE")
    require(not path.is_symlink() and not path.exists(), "OUTPUT_FILE_ALREADY_EXISTS")
    return path


def save_code_file(path: Path, plan: dict, claim_code: str, created_at: str) -> None:
    payload = {
        "version": 1, "kind": "hearo_device_kit_registration_code",
        "kit_id": plan["kit_id"], "claim_code": claim_code,
        "devices": plan["devices"], "target": plan["target"], "created_at": created_at,
        "inventory_outcome": "check_command_result_or_server_before_retry",
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o600, "OUTPUT_FILE_NOT_PRIVATE")
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def cloud_clients(region: str):
    import boto3
    from botocore.config import Config
    config = Config(connect_timeout=5, read_timeout=20,
                    retries={"total_max_attempts": 1, "mode": "standard"})
    session = boto3.Session(region_name=region)
    return (session.client("sts", config=config), session.client("dynamodb", config=config),
            session.resource("dynamodb", config=config))


def validate_live_target(plan: dict, sts, client) -> None:
    target = plan["target"]
    require(sts.get_caller_identity().get("Account") == target["account"], "AWS_ACCOUNT_MISMATCH")
    table = client.describe_table(TableName=target["core_table"])["Table"]
    require(table.get("TableArn") == target["core_table_arn"] and table.get("TableStatus") == "ACTIVE",
            "TABLE_ARN_OR_STATUS_MISMATCH")
    require({(item.get("AttributeName"), item.get("KeyType")) for item in table.get("KeySchema", [])}
            == {("pk", "HASH"), ("sk", "RANGE")}, "TABLE_KEY_SCHEMA_MISMATCH")
    types = {item.get("AttributeName"): item.get("AttributeType") for item in table.get("AttributeDefinitions", [])}
    require(types.get("pk") == types.get("sk") == "S", "TABLE_KEY_TYPE_MISMATCH")


def build_repository(plan: dict, client, resource):
    from hearo_backend.config import Settings
    from hearo_backend.store import DynamoRepository
    target = plan["target"]
    settings = Settings(environment="development", store_backend="dynamodb", mqtt_enabled=False,
                        region=target["region"], core_table=target["core_table"])
    repository = DynamoRepository(settings)
    # Use only the already-verified clients with one SDK attempt. Constructor
    # creation does not issue any DynamoDB requests or initialize the API.
    repository.client = client
    repository.resource = resource
    repository.core = resource.Table(target["core_table"])
    repository.alerts = resource.Table(settings.alerts_table)
    return repository


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    stage, creation_attempted, output_saved, output = "local_input", False, False, None
    try:
        plan = local_plan(arguments)
        output = private_output_path(arguments.output_file)
        if not arguments.apply:
            print(json.dumps({"status": "dry_run", **plan, "output_file": str(output),
                              "db_operations": False, "code_generated": False,
                              "inventory_item_count": 9}, ensure_ascii=False, sort_keys=True))
            return 0
        stage = "target_validation"
        sts, client, resource = cloud_clients(arguments.region)
        validate_live_target(plan, sts, client)
        stage = "private_code_file"
        code = new_claim_code()
        encoded_hash = hash_claim_code(code)
        created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        save_code_file(output, plan, code, created_at)
        output_saved = True
        stage = "inventory_transaction"
        repository = build_repository(plan, client, resource)
        creation_attempted = True
        repository.issue_device_kit(kit_id=plan["kit_id"], claim_code_hash=encoded_hash,
                                    devices=plan["devices"], created_at=created_at)
        print(json.dumps({"status": "kit_inventory_created", "kit_id": plan["kit_id"],
                          "target": plan["target"], "output_file": str(output),
                          "inventory_item_count": 9, "automatic_retry": False,
                          "hardware_auto_configured": False}, ensure_ascii=False, sort_keys=True))
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        failure = {"status": "error", "stage": stage,
                   "inventory_creation_attempted": creation_attempted,
                   "database_result_may_be_unknown": creation_attempted,
                   "output_file_saved": output_saved, "automatic_retry": False}
        if output is not None:
            failure["output_file"] = str(output)
        if isinstance(exc, ProvisionGuardError):
            failure["reason"] = exc.code
        else:
            failure["reason"] = "PROVISION_FAILED"
            domain_code = getattr(exc, "code", None)
            if isinstance(domain_code, str) and domain_code in {
                "KIT_INVENTORY_CONFLICT", "KIT_CONFIGURATION_INVALID", "KIT_SERVICE_UNAVAILABLE"
            }:
                failure["reason"] = domain_code
            response = getattr(exc, "response", None)
            error = response.get("Error") if isinstance(response, dict) else None
            code = error.get("Code") if isinstance(error, dict) else None
            if isinstance(code, str) and code in AWS_ERROR_CODES:
                failure["aws_error_code"] = code
        print(json.dumps(failure, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
