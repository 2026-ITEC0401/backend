from __future__ import annotations

import json
import os
import stat
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

from scripts import provision_device_kit as cli


def arguments(tmp_path, *, apply=False, table="hearo-core-v2-r6-staging"):
    os.chmod(tmp_path, 0o700)
    values = [
        "--kit-id", " hearo-kit-test0001 ",
        "--rpi-hardware-id", "hr-rpi-test01",
        "--esp32-1-hardware-id", "HR-ESP-TEST01",
        "--esp32-2-hardware-id", "HR-ESP-TEST02",
        "--esp32-3-hardware-id", "HR-ESP-TEST03",
        "--region", cli.REGION, "--core-table", table,
        "--output-file", str(tmp_path / "code.json"),
    ]
    if apply:
        values.extend(("--apply", "--confirm-target", f"{cli.ACCOUNT}/{cli.REGION}/{table}"))
    return values


def replace_argument(values, flag, value):
    updated = values.copy()
    updated[updated.index(flag) + 1] = value
    return updated


class StsStub:
    def __init__(self, account=cli.ACCOUNT):
        self.account = account
        self.calls = 0

    def get_caller_identity(self):
        self.calls += 1
        return {"Account": self.account}


class ClientStub:
    def __init__(self, table="hearo-core-v2-r6-staging"):
        self.calls = []
        self.table = {
            "TableArn": f"arn:aws:dynamodb:{cli.REGION}:{cli.ACCOUNT}:table/{table}",
            "TableStatus": "ACTIVE",
            "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"},
                          {"AttributeName": "sk", "KeyType": "RANGE"}],
            "AttributeDefinitions": [{"AttributeName": "pk", "AttributeType": "S"},
                                     {"AttributeName": "sk", "AttributeType": "S"}],
        }

    def describe_table(self, **kwargs):
        self.calls.append(kwargs)
        return {"Table": self.table}


def install_cloud(monkeypatch, tmp_path, *, error=None):
    sts, client, calls = StsStub(), ClientStub(), []
    secret = "T9QW-R7NM"
    monkeypatch.setattr(cli, "new_claim_code", lambda: secret)
    monkeypatch.setattr(cli, "hash_claim_code", lambda code: "PRIVATE-ARGON-HASH")
    monkeypatch.setattr(cli, "cloud_clients", lambda region: (sts, client, object()))

    class RepositoryStub:
        def issue_device_kit(self, **kwargs):
            # The code must be recoverable before the only database write.
            file = tmp_path / "code.json"
            payload = json.loads(file.read_text(encoding="utf-8"))
            assert payload["claim_code"] == secret
            assert stat.S_IMODE(file.stat().st_mode) == 0o600
            calls.append(kwargs)
            if error is not None:
                raise error

    monkeypatch.setattr(cli, "build_repository", lambda *unused: RepositoryStub())
    return SimpleNamespace(sts=sts, client=client, calls=calls, secret=secret)


def test_default_preview_has_no_aws_code_or_files(monkeypatch, tmp_path, capsys):
    def forbidden(*unused):
        pytest.fail("preview must not create clients, generate a code or build a repository")
    for name in ("cloud_clients", "new_claim_code", "hash_claim_code", "build_repository"):
        monkeypatch.setattr(cli, name, forbidden)
    assert cli.main(arguments(tmp_path)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "dry_run"
    assert result["db_operations"] is False
    assert result["code_generated"] is False
    assert result["kit_id"] == "HEARO-KIT-TEST0001"
    assert [d["device_id"] for d in result["devices"]] == ["rpi-001", "esp32_1", "esp32_2", "esp32_3"]
    assert len({d["hardware_id"] for d in result["devices"]}) == 4
    assert not (tmp_path / "code.json").exists()


@pytest.mark.parametrize("flag,value,reason", [
    ("--kit-id", "invalid", "INVALID_KIT_OR_HARDWARE_ID"),
    ("--kit-id", "HEARO-KIT-ＴＥＳＴ", "INVALID_KIT_OR_HARDWARE_ID"),
    ("--rpi-hardware-id", "a", "INVALID_KIT_OR_HARDWARE_ID"),
    ("--esp32-1-hardware-id", "ＨＲ-ESP-0001", "INVALID_KIT_OR_HARDWARE_ID"),
    ("--esp32-2-hardware-id", " hr-esp-test01 ", "DUPLICATE_HARDWARE_ID"),
    ("--region", "us-east-1", "TARGET_NOT_ALLOWED"),
    ("--core-table", "other-table", "TARGET_NOT_ALLOWED"),
])
def test_invalid_preview_stops_before_aws(monkeypatch, tmp_path, capsys, flag, value, reason):
    monkeypatch.setattr(cli, "cloud_clients", lambda *_: pytest.fail("no AWS calls permitted"))
    values = replace_argument(arguments(tmp_path), flag, value)
    assert cli.main(values) == 2
    failure = json.loads(capsys.readouterr().err)
    assert failure["reason"] == reason
    assert failure["inventory_creation_attempted"] is False
    assert not (tmp_path / "code.json").exists()


@pytest.mark.parametrize("confirmation", [None, "", "377152274782/ap-south-1/other-table"])
def test_apply_requires_exact_confirmation(monkeypatch, tmp_path, capsys, confirmation):
    monkeypatch.setattr(cli, "cloud_clients", lambda *_: pytest.fail("no AWS before confirmation"))
    values = arguments(tmp_path) + ["--apply"]
    if confirmation is not None:
        values.extend(("--confirm-target", confirmation))
    assert cli.main(values) == 2
    assert json.loads(capsys.readouterr().err)["reason"] == "EXACT_TARGET_CONFIRMATION_REQUIRED"
    assert not (tmp_path / "code.json").exists()


def test_apply_private_file_precedes_single_issue(monkeypatch, tmp_path, capsys):
    cloud = install_cloud(monkeypatch, tmp_path)
    assert cli.main(arguments(tmp_path, apply=True)) == 0
    logs = capsys.readouterr()
    assert cloud.secret not in logs.out + logs.err
    assert "PRIVATE-ARGON-HASH" not in logs.out + logs.err
    assert len(cloud.calls) == 1
    assert cloud.sts.calls == 1
    assert len(cloud.client.calls) == 1
    issued = cloud.calls[0]
    assert issued["claim_code_hash"] == "PRIVATE-ARGON-HASH"
    assert len(issued["devices"]) == 4
    assert "claim_code" not in issued
    assert "expires_at_epoch" not in json.dumps(issued)
    saved = json.loads((tmp_path / "code.json").read_text(encoding="utf-8"))
    assert saved["claim_code"] == cloud.secret
    assert saved["created_at"] == issued["created_at"]
    assert json.loads(logs.out)["hardware_auto_configured"] is False


@pytest.mark.parametrize("error", [
    ReadTimeoutError(endpoint_url="https://dynamodb.ap-south-1.amazonaws.com"),
    ClientError({"Error": {"Code": "ThrottlingException", "Message": "PRIVATE SECRET BODY"}}, "TransactWriteItems"),
    RuntimeError("PRIVATE SECRET BODY T9QW-R7NM PRIVATE-ARGON-HASH"),
])
def test_unknown_result_keeps_file_and_never_retries(monkeypatch, tmp_path, capsys, error):
    cloud = install_cloud(monkeypatch, tmp_path, error=error)
    assert cli.main(arguments(tmp_path, apply=True)) == 2
    logs = capsys.readouterr()
    assert "PRIVATE SECRET BODY" not in logs.out + logs.err
    assert cloud.secret not in logs.out + logs.err
    assert "PRIVATE-ARGON-HASH" not in logs.out + logs.err
    assert len(cloud.calls) == 1
    assert (tmp_path / "code.json").exists()
    failure = json.loads(logs.err)
    assert failure["database_result_may_be_unknown"] is True
    assert failure["output_file_saved"] is True
    assert failure["automatic_retry"] is False


@pytest.mark.parametrize("wrong", ["account", "arn", "status", "keys", "types"])
def test_live_target_mismatch_never_creates_file_or_inventory(monkeypatch, tmp_path, capsys, wrong):
    cloud = install_cloud(monkeypatch, tmp_path)
    if wrong == "account":
        cloud.sts.account = "000000000000"
    elif wrong == "arn":
        cloud.client.table["TableArn"] = "arn:aws:dynamodb:us-east-1:000000000000:table/other"
    elif wrong == "status":
        cloud.client.table["TableStatus"] = "UPDATING"
    elif wrong == "keys":
        cloud.client.table["KeySchema"] = [{"AttributeName": "different", "KeyType": "HASH"}]
    else:
        cloud.client.table["AttributeDefinitions"][0]["AttributeType"] = "N"
    assert cli.main(arguments(tmp_path, apply=True)) == 2
    failure = json.loads(capsys.readouterr().err)
    assert failure["inventory_creation_attempted"] is False
    assert not (tmp_path / "code.json").exists()
    assert cloud.calls == []


def test_existing_file_is_preserved_no_aws(monkeypatch, tmp_path, capsys):
    values = arguments(tmp_path, apply=True)
    file = tmp_path / "code.json"
    file.write_text("keep original", encoding="utf-8")
    monkeypatch.setattr(cli, "cloud_clients", lambda *_: pytest.fail("no AWS for existing file"))
    assert cli.main(values) == 2
    assert json.loads(capsys.readouterr().err)["reason"] == "OUTPUT_FILE_ALREADY_EXISTS"
    assert file.read_text(encoding="utf-8") == "keep original"


def test_symlink_output_is_rejected(monkeypatch, tmp_path, capsys):
    values = arguments(tmp_path, apply=True)
    (tmp_path / "code.json").symlink_to(tmp_path / "nonexistent")
    monkeypatch.setattr(cli, "cloud_clients", lambda *_: pytest.fail("no AWS for linked file"))
    assert cli.main(values) == 2
    assert json.loads(capsys.readouterr().err)["reason"] == "OUTPUT_FILE_ALREADY_EXISTS"


def test_nonprivate_directory_is_rejected(monkeypatch, tmp_path, capsys):
    values = arguments(tmp_path, apply=True)
    os.chmod(tmp_path, 0o755)
    monkeypatch.setattr(cli, "cloud_clients", lambda *_: pytest.fail("no AWS for public directory"))
    assert cli.main(values) == 2
    assert json.loads(capsys.readouterr().err)["reason"] == "OUTPUT_DIRECTORY_NOT_PRIVATE"


def test_private_file_failure_stops_before_repository(monkeypatch, tmp_path, capsys):
    cloud = install_cloud(monkeypatch, tmp_path)
    def fail_save(*unused):
        raise OSError("PRIVATE SECRET BODY")
    monkeypatch.setattr(cli, "save_code_file", fail_save)
    assert cli.main(arguments(tmp_path, apply=True)) == 2
    logs = capsys.readouterr()
    assert "PRIVATE SECRET BODY" not in logs.err
    assert json.loads(logs.err)["inventory_creation_attempted"] is False
    assert cloud.calls == []


def test_one_sdk_attempt_for_every_cloud_client(monkeypatch):
    import boto3
    calls = []
    class SessionStub:
        def client(self, kind, *, config):
            calls.append((kind, config))
            return object()
        def resource(self, kind, *, config):
            calls.append((kind, config))
            return object()
    monkeypatch.setattr(boto3, "Session", lambda **unused: SessionStub())
    cli.cloud_clients(cli.REGION)
    assert [kind for kind, _config in calls] == ["sts", "dynamodb", "dynamodb"]
    assert all(config.retries["total_max_attempts"] == 1 for _kind, config in calls)


def test_cli_real_issuer_is_exact_nine_conditional_puts(monkeypatch, tmp_path, capsys):
    from boto3.dynamodb.types import TypeDeserializer
    from hearo_backend.device_kits import hash_claim_code, verify_claim_code
    from hearo_backend.store import DynamoRepository
    cloud = install_cloud(monkeypatch, tmp_path)
    transactions = []
    def transact_write_items(**kwargs):
        transactions.append(kwargs)
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}
    cloud.client.transact_write_items = transact_write_items
    repository = DynamoRepository.__new__(DynamoRepository)
    repository.settings = SimpleNamespace(core_table="hearo-core-v2-r6-staging")
    repository.client = cloud.client
    monkeypatch.setattr(cli, "build_repository", lambda *_: repository)
    monkeypatch.setattr(cli, "hash_claim_code", hash_claim_code)
    assert cli.main(arguments(tmp_path, apply=True)) == 0
    logs = capsys.readouterr()
    assert cloud.secret not in logs.out + logs.err
    assert len(transactions) == 1
    operations = transactions[0]["TransactItems"]
    assert len(operations) == 9
    decoder = TypeDeserializer()
    items = []
    for operation in operations:
        assert set(operation) == {"Put"}
        put = operation["Put"]
        assert put["TableName"] == "hearo-core-v2-r6-staging"
        assert "attribute_not_exists" in put["ConditionExpression"]
        item = {key: decoder.deserialize(value) for key, value in put["Item"].items()}
        assert "expires_at_epoch" not in item
        assert "credential" not in item
        assert "claim_code" not in item
        items.append(item)
    assert len({(item["pk"], item["sk"]) for item in items}) == 9
    assert sum(item["pk"].startswith("KIT#") for item in items) == 5
    assert sum(item["pk"].startswith("HARDWARE#") for item in items) == 4
    metadata = next(item for item in items if item["sk"] == "META")
    assert metadata["status"] == "unclaimed"
    assert verify_claim_code(cloud.secret, metadata["claim_code_hash"])
