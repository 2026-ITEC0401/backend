#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from hearo_backend.retention import (  # noqa: E402
    RetentionPlanError,
    RetentionTarget,
    apply_retention_plan,
    build_retention_plan,
    load_plan_file,
    validate_plan_target,
    write_plan_file,
)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description=(
            "Preview or apply the fixed 90-day alert retention policy. "
            "Without --apply this command only writes a private, reviewable plan."
        )
    )
    value.add_argument("--mode", choices=("purge", "migrate"), default="migrate")
    value.add_argument("--region", required=True)
    value.add_argument("--alerts-table", required=True)
    value.add_argument("--core-table", required=True)
    value.add_argument("--plan-file")
    value.add_argument("--apply", action="store_true")
    value.add_argument("--scheduled", action="store_true")
    value.add_argument("--confirm-region")
    value.add_argument("--confirm-alerts-table")
    value.add_argument("--confirm-core-table")
    value.add_argument("--confirm-alerts-table-arn")
    value.add_argument("--confirm-core-table-arn")
    return value


def _validate_mode_arguments(
    arguments: argparse.Namespace, value: argparse.ArgumentParser
) -> None:
    if arguments.scheduled:
        if arguments.mode != "purge" or not arguments.apply:
            value.error("--scheduled requires both --mode purge and --apply")
        if arguments.plan_file:
            value.error("--scheduled does not accept --plan-file")
    elif not arguments.plan_file:
        value.error("manual preview and apply require --plan-file")
    if arguments.apply:
        required = {
            "--confirm-region": arguments.confirm_region,
            "--confirm-alerts-table": arguments.confirm_alerts_table,
            "--confirm-core-table": arguments.confirm_core_table,
            "--confirm-alerts-table-arn": arguments.confirm_alerts_table_arn,
            "--confirm-core-table-arn": arguments.confirm_core_table_arn,
        }
        missing = [flag for flag, item in required.items() if not item]
        if missing:
            value.error("--apply requires explicit target confirmation: " + ", ".join(missing))


def _live_target(client, arguments: argparse.Namespace) -> RetentionTarget:
    alerts = client.describe_table(TableName=arguments.alerts_table)["Table"]
    core = client.describe_table(TableName=arguments.core_table)["Table"]
    return RetentionTarget(
        region=arguments.region,
        alerts_table=arguments.alerts_table,
        core_table=arguments.core_table,
        alerts_table_arn=alerts["TableArn"],
        core_table_arn=core["TableArn"],
    )


def _validate_confirmations(
    arguments: argparse.Namespace, target: RetentionTarget
) -> None:
    confirmations = RetentionTarget(
        region=arguments.confirm_region,
        alerts_table=arguments.confirm_alerts_table,
        core_table=arguments.confirm_core_table,
        alerts_table_arn=arguments.confirm_alerts_table_arn,
        core_table_arn=arguments.confirm_core_table_arn,
    )
    if confirmations != target:
        raise RetentionPlanError(
            "apply confirmation does not exactly match region, table names, and live table ARNs"
        )


def run(arguments: argparse.Namespace) -> dict[str, object]:
    import boto3

    session = boto3.session.Session(region_name=arguments.region)
    client = session.client("dynamodb")
    resource = session.resource("dynamodb")
    target = _live_target(client, arguments)
    alerts_table = resource.Table(arguments.alerts_table)
    core_table = resource.Table(arguments.core_table)

    if arguments.apply:
        _validate_confirmations(arguments, target)

    if arguments.scheduled:
        plan = build_retention_plan(
            alerts_table,
            core_table,
            target,
            mode="purge",
        )
        applied = apply_retention_plan(plan, alerts_table, core_table, client)
        return {
            "status": "scheduled_purge_applied",
            "target": {
                "region": target.region,
                "alerts_table": target.alerts_table,
                "core_table": target.core_table,
            },
            "report": plan.report,
            "apply": applied,
        }

    if arguments.apply:
        plan = load_plan_file(arguments.plan_file)
        if plan.mode != arguments.mode:
            raise RetentionPlanError("--mode does not match the saved retention plan")
        validate_plan_target(plan, target)
        applied = apply_retention_plan(plan, alerts_table, core_table, client)
        return {
            "status": "plan_applied",
            "plan_file": arguments.plan_file,
            "report": plan.report,
            "apply": applied,
        }

    plan = build_retention_plan(
        alerts_table,
        core_table,
        target,
        mode=arguments.mode,
    )
    write_plan_file(plan, arguments.plan_file)
    return {
        "status": "dry_run_plan_created",
        "plan_file": arguments.plan_file,
        "report": plan.report,
    }


def main(argv: list[str] | None = None) -> int:
    argument_parser = parser()
    arguments = argument_parser.parse_args(argv)
    _validate_mode_arguments(arguments, argument_parser)
    try:
        result = run(arguments)
    except (OSError, RetentionPlanError, ValueError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
