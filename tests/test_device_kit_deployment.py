"""Offline deployment checks. Moto is not evidence of live AWS or EC2 cutover."""
from __future__ import annotations

import copy
import importlib.util
import json
import re
import stat
import subprocess
from pathlib import Path

import boto3
from botocore.client import BaseClient
from boto3.dynamodb.types import TypeDeserializer
import pytest

moto = pytest.importorskip("moto")
ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40


@pytest.fixture
def deployment_smoke(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "kit_deployment_smoke_offline", ROOT / "scripts/verify_device_kit_ddb.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ACCOUNT", "123456789012")
    monkeypatch.setattr(module, "TARGET", f"{module.ACCOUNT}/{module.REGION}/{module.CORE}/{module.ALERTS}")

    def git_guard(argv, *, text):
        assert text is True
        if argv == ["git", "-C", str(ROOT), "rev-parse", "HEAD"]:
            return SHA + "\n"
        assert argv == ["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=all"]
        return ""

    monkeypatch.setattr(module.subprocess, "check_output", git_guard)
    for key, value in {
        "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing", "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_REGION": module.REGION, "HEARO_ENV": "development", "HEARO_STORE": "dynamodb",
        "HEARO_CORE_TABLE": module.CORE, "HEARO_ALERTS_TABLE": module.ALERTS,
        "HEARO_MQTT_ENABLED": "false", "HEARO_LEGAL_CONSENT_REQUIRED": "false",
        "HEARO_TERMS_VERSION": "", "HEARO_PRIVACY_VERSION": "", "HEARO_LEGAL_EFFECTIVE_AT": "",
        "HEARO_TERMS_URL": "", "HEARO_PRIVACY_URL": "",
    }.items():
        monkeypatch.setenv(key, value)
    with moto.mock_aws():
        resource = boto3.resource("dynamodb", region_name=module.REGION)
        for name, partition, sort in ((module.CORE, "pk", "sk"), (module.ALERTS, "household_id", "event_key")):
            resource.create_table(
                TableName=name, BillingMode="PAY_PER_REQUEST",
                KeySchema=[{"AttributeName": partition, "KeyType": "HASH"}, {"AttributeName": sort, "KeyType": "RANGE"}],
                AttributeDefinitions=[{"AttributeName": partition, "AttributeType": "S"}, {"AttributeName": sort, "AttributeType": "S"}],
            )
        writes = []
        original = BaseClient._make_api_call

        def observe(client, operation, params):
            if operation in {"PutItem", "UpdateItem", "DeleteItem", "TransactWriteItems", "BatchWriteItem"}:
                writes.append((operation, copy.deepcopy(params), copy.deepcopy(client.meta.config.retries)))
            return original(client, operation, params)

        monkeypatch.setattr(BaseClient, "_make_api_call", observe)
        yield module, resource, writes


def test_kit_smoke_default_read_only_preserves_exact_staging_tables(deployment_smoke, capsys):
    module, resource, writes = deployment_smoke
    resource.Table(module.CORE).put_item(Item={"pk": "SENTINEL", "sk": "KEEP", "value": "untouched"})
    resource.Table(module.ALERTS).put_item(Item={"household_id": "sentinel", "event_key": "keep", "expires_at_epoch": 2000000000})
    before = [module.snapshot(resource.Table(name)) for name in (module.CORE, module.ALERTS)]
    writes.clear()
    module.main(["--confirm-sha", SHA, "--confirm-target", module.TARGET])
    assert writes == []
    assert [module.snapshot(resource.Table(name)) for name in (module.CORE, module.ALERTS)] == before
    assert "KIT DDB READ-ONLY PREFLIGHT OK" in capsys.readouterr().out


def test_kit_smoke_moto_full_claim_withdraw_and_nine_row_conditional_cleanup(deployment_smoke, tmp_path, capsys):
    module, resource, writes = deployment_smoke
    tmp_path.chmod(0o700)
    recovery = tmp_path / "recovery.json"
    module.main(["--apply", "--confirm-sha", SHA, "--confirm-target", module.TARGET, "--recovery-file", str(recovery)])
    assert stat.S_IMODE(recovery.stat().st_mode) == 0o600
    data = json.loads(recovery.read_text(encoding="utf-8"))
    output = capsys.readouterr().out
    assert "KIT REAL DYNAMODB API SMOKE OK" in output
    for sensitive in (data["claim_code"], data["password"], data["login_id"]):
        assert sensitive not in output
    assert module.snapshot(resource.Table(module.CORE)) == {}
    assert module.snapshot(resource.Table(module.ALERTS)) == {}
    transactions = [(params, retry) for operation, params, retry in writes if operation == "TransactWriteItems"]
    assert any(len(p["TransactItems"]) == 11 and sum("Update" in item for item in p["TransactItems"]) == 10 for p, _ in transactions)
    cleanup, retries = transactions[-1]
    assert retries["total_max_attempts"] == 1
    assert len(cleanup["TransactItems"]) == 9
    decode = TypeDeserializer().deserialize
    keys = set()
    for operation in cleanup["TransactItems"]:
        assert set(operation) == {"Delete"}
        value = operation["Delete"]
        assert value["TableName"] == module.CORE
        key = tuple(decode(value["Key"][field]) for field in ("pk", "sk"))
        keys.add(key)
        assert value["ConditionExpression"].startswith("attribute_exists(pk) AND ")
        assert value["ExpressionAttributeNames"] and value["ExpressionAttributeValues"]
        for token in value["ExpressionAttributeNames"]:
            assert f"{token}=:{token[1:]}" in value["ConditionExpression"]
    expected = {(f"KIT#{data['kit_id']}", "META")} | {
        (f"KIT#{data['kit_id']}", f"DEVICE#{row['device_id']}") for row in data["hardware"]
    } | {(f"HARDWARE#{row['hardware_id']}", "KIT") for row in data["hardware"]}
    assert keys == expected


def test_cutover_shell_syntax_existing_unit_names_and_no_retention_mutations():
    script = ROOT / "scripts/deploy_v250_cutover.sh"
    subprocess.run(["/bin/bash", "-n", str(script)], check=True, capture_output=True, text=True)
    text = script.read_text(encoding="utf-8")
    assert "r6=/opt/hearo-backend-v2-r6" in text and "r7=/opt/hearo-backend-v2-r7" in text
    assert "a3376800ed036049f1d2290f3dea847135a6c022" in text
    assert text.index("if test \"$mode\" = --check-only; then exit 0; fi") < text.index("phase=backup")
    assert text.index("test \"${3:-}\" = --staging-verified") < text.index("phase=backup")
    assert text.index("systemctl stop hearo-mqtt-bridge-v2-final") < text.index("systemctl stop hearo-api-v2-final")
    assert text.index("systemctl start hearo-api-v2-final") < text.index("phase=start-new-bridge")
    assert "hearo-alert-retention" not in text
    assert not re.search(r"update-time-to-live|delete-table|--mode\s+migrate|systemctl\s+(?:start|stop|restart|enable|disable)\s+hearo-alert", text)
    for template, final, env, executable in (
        ("hearo-api-v2-r7.service", "hearo-api-v2-final.service", "hearo-api-v2.env", "-m uvicorn dashboard_api_v2:app --host 127.0.0.1 --port 8001"),
        ("hearo-mqtt-bridge-v2-r7.service", "hearo-mqtt-bridge-v2-final.service", "hearo-mqtt-bridge-v2.env", "-u -m hearo_backend.mqtt_bridge"),
    ):
        unit = (ROOT / "infra/systemd" / template).read_text(encoding="utf-8")
        assert "WorkingDirectory=/opt/hearo-backend-v2-r7\n" in unit
        assert f"EnvironmentFile=/etc/hearo/{env}\n" in unit
        assert "ExecStart=/opt/hearo-backend-v2-r7/.venv/bin/python -B -E " in unit
        assert executable in unit
        assert f'"$r7/infra/systemd/{template}" /etc/systemd/system/{final}' in text
        assert "hearo-alert-retention" not in unit


@pytest.mark.parametrize(
    "started, trigger, expected_status, bridge_stop_fails",
    [
        (False, "false", 1, False),
        (False, 'kill -s TERM "$$"', 143, False),
        (True, "false", 1, False),
        (True, "false", 1, True),
        (True, 'kill -s INT "$$"', 130, False),
        (True, 'kill -s TERM "$$"', 143, False),
        (True, 'kill -s HUP "$$"', 129, False),
    ],
)
def test_cutover_actual_failure_handler_with_only_mocked_service_commands(
    started, trigger, expected_status, bridge_stop_fails,
):
    text = (ROOT / "scripts/deploy_v250_cutover.sh").read_text(encoding="utf-8")
    # Execute only the unchanged handler/trap prefix, never the operational body.
    # The shell has no external-command PATH; systemctl is our local function.
    header, separator, _ = text.partition("\nmode=")
    assert separator and "on_failure()" in header
    assert text.index("phase=backup") < text.index("\ncutover_started=1\n")
    transition = text[text.index("phase=stop-old-writers"):]
    assert transition.index("cutover_started=1") < transition.index("systemctl stop")
    harness = (
        'systemctl() { printf "MOCK_SERVICE %s\\n" "$*"; '
        + ('if test "$1 $2" = "stop hearo-mqtt-bridge-v2-final"; then return 42; fi; ' if bridge_stop_fails else "")
        + "return 0; }\n"
        + header
        + f"\ncutover_started={int(started)}\nphase=offline-test\n"
        + trigger
        + '\nprintf "UNREACHABLE\\n"\n'
    )
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc"], input=harness,
        env={"PATH": "/offline-no-external-commands"},
        capture_output=True, text=True, timeout=3,
    )
    assert result.returncode == expected_status
    assert "UNREACHABLE" not in result.stdout
    assert "단계=offline-test" in result.stderr
    assert "자동 롤백·재시도 없음" in result.stderr
    assert "start " not in result.stdout
    if not started:
        assert result.stdout == ""
    else:
        assert result.stdout.splitlines() == [
            "MOCK_SERVICE stop hearo-mqtt-bridge-v2-final",
            "MOCK_SERVICE stop hearo-api-v2-final",
            "MOCK_SERVICE show hearo-api-v2-final hearo-mqtt-bridge-v2-final -p Id -p ActiveState -p WorkingDirectory --no-pager",
        ]
