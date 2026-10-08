"""Small, explicitly approved isolated-DynamoDB API smoke; never production.

No physical devices, MQTT broker or natural DynamoDB capacity-boundary tests.
On any unexpected result stop without retries or automatic cleanup. The private
recovery file is retained so the operator can inspect uncertain writes first.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import uuid
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ACCOUNT = "377152274782"
REGION = "ap-south-1"
CORE = "hearo-core-v2-r6-staging"
ALERTS = "hearo-alerts-v2-r6-staging"
TARGET = f"{ACCOUNT}/{REGION}/{CORE}/{ALERTS}"


def snapshot(table):
    rows, kwargs = {}, {"ConsistentRead": True}
    while True:
        response = table.scan(**kwargs)
        for row in response.get("Items", []):
            key = (row["pk"], row["sk"]) if "pk" in row else (row["household_id"], row["event_key"])
            assert key not in rows, "중복 키 관찰"
            rows[key] = row
        assert len(rows) <= 1000, "격리 시험 예상 크기를 벗어났습니다."
        if not response.get("LastEvaluatedKey"):
            return rows
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def expect(response, status_code, label):
    print(f"{label}: HTTP {response.status_code}", flush=True)
    assert response.status_code == status_code, f"{label} 응답 불일치"
    return response.json() if status_code != 204 else None


def device_snapshot(repo, household_id, device_id):
    item = repo.core.get_item(
        Key={"pk": f"HOUSE#{household_id}", "sk": f"DEVICE#{device_id}"},
        ConsistentRead=True,
    ).get("Item")
    assert item is not None
    return asdict(repo._device(item))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-sha", required=True)
    parser.add_argument("--confirm-target", required=True)
    parser.add_argument("--recovery-file", type=Path)
    args = parser.parse_args(argv)
    assert re.fullmatch(r"[0-9a-f]{40}", args.confirm_sha)
    assert args.confirm_target == TARGET
    assert subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip() == args.confirm_sha
    assert not subprocess.check_output(["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=all"], text=True).strip()

    from hearo_backend import __version__
    from hearo_backend.config import Settings
    from hearo_backend.domain import FIXED_DEVICES, iso_utc
    from hearo_backend.device_kits import hash_claim_code, new_claim_code
    from hearo_backend.integrations import NullMqttPublisher
    from hearo_backend.main import create_app
    from hearo_backend.store import DynamoRepository
    import boto3
    from botocore.config import Config
    from fastapi.testclient import TestClient

    s = Settings()
    assert __version__ == "2.5.0"
    assert (s.environment, s.store_backend, s.region, s.core_table, s.alerts_table, s.mqtt_enabled) == (
        "development", "dynamodb", REGION, CORE, ALERTS, False,
    ), "운영 테이블·MQTT를 사용하는 시험은 허용하지 않습니다."
    assert not s.legal_consent_required
    assert all(getattr(s, key) == "" for key in (
        "terms_version", "privacy_version", "legal_effective_at", "terms_url", "privacy_url",
    ))
    identity = boto3.client("sts", region_name=REGION).get_caller_identity()
    assert identity["Account"] == ACCOUNT
    repo = DynamoRepository(s)
    # Single transaction attempt. Unknown results require inspection, not retry.
    repo.client = boto3.client("dynamodb", region_name=REGION, config=Config(retries={"total_max_attempts": 1}))
    for table, pk, sk in ((repo.core, "pk", "sk"), (repo.alerts, "household_id", "event_key")):
        description = repo.client.describe_table(TableName=table.name)["Table"]
        assert description["TableStatus"] == "ACTIVE"
        assert description["TableArn"] == f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{table.name}"
        assert {v["KeyType"]: v["AttributeName"] for v in description["KeySchema"]} == {"HASH": pk, "RANGE": sk}
    before = snapshot(repo.core)
    before_alerts = snapshot(repo.alerts)
    with TestClient(create_app(s, repo, NullMqttPublisher())) as client:
        expect(client.get("/health"), 200, "격리 API")
        paths = client.get("/openapi.json").json()["paths"]
        for suffix, verb in (("", "get"), ("/claim/preview", "post"), ("/claim", "post")):
            assert verb in paths["/households/{household_id}/device-kit" + suffix]
        if not args.apply:
            assert snapshot(repo.core) == before and snapshot(repo.alerts) == before_alerts
            print("KIT DDB READ-ONLY PREFLIGHT OK / 합성 계정·키트 생성 없음")
            return
        assert args.recovery_file is not None, "복구용 비공개 파일 경로가 필요합니다."
        parent = args.recovery_file.parent
        assert parent.is_dir() and not parent.is_symlink()
        info = parent.stat()
        assert info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700
        run = uuid.uuid4().hex
        kit_id = "HEARO-KIT-STG" + run[:20].upper()
        claim_code = new_claim_code()
        login_id = "kit" + run[:24]
        password = "KitTest" + uuid.uuid4().hex
        phone = "010" + str(int(run[:12], 16) % 100000000).zfill(8)
        hardware = [
            {"device_id": role, "device_type": kind, "hardware_id": f"HR-STG-{run.upper()}-{index}"}
            for index, (role, _, kind) in enumerate(FIXED_DEVICES)
        ]
        recovery = {"run": run, "target": TARGET, "kit_id": kit_id, "claim_code": claim_code,
                    "login_id": login_id, "password": password, "hardware": hardware}
        fd = os.open(args.recovery_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(recovery, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        print(f"합성 실행: {run} / 복구 파일: {args.recovery_file}", flush=True)
        repo.issue_device_kit(kit_id, hash_claim_code(claim_code), hardware, iso_utc())
        owner = expect(client.post("/auth/signup", json={
            "login_id": login_id, "phone_number": phone, "password": password,
            "name": "키트 합성 시험", "signup_type": "new_household", "household_name": "키트 합성 가구",
            "terms_service_agreed": True, "privacy_agreed": True, "age_over_14_agreed": True,
        }), 201, "합성 owner 가입")
        house = owner["user"]["household_id"]
        headers = {"Authorization": "Bearer " + owner["tokens"]["access_token"]}
        url = f"/households/{house}/device-kit"
        saved_devices = {role: device_snapshot(repo, house, role) for role, _, _ in FIXED_DEVICES}
        credentials = owner["device_credentials"].copy()
        payload = {"kit_id": kit_id, "claim_code": claim_code}
        assert expect(client.get(url, headers=headers), 200, "미등록 조회")["status"] == "unregistered"
        initial = snapshot(repo.core)
        expect(client.post(url + "/claim/preview", headers=headers, json=payload), 200, "미리보기")
        assert snapshot(repo.core) == initial, "미리보기에서 DB 변경"
        claimed = expect(client.post(url + "/claim", headers=headers, json=payload), 200, "키트 등록")
        assert claimed["status"] == "claimed"
        assert expect(client.get(url, headers=headers), 200, "등록 조회") == claimed
        assert expect(client.post(url + "/claim", headers=headers, json=payload), 200, "동일 요청 재전송") == claimed
        wrong = {**payload, "claim_code": "0000-0000" if claim_code != "0000-0000" else "1111-1111"}
        assert expect(client.post(url + "/claim", headers=headers, json=wrong), 400, "틀린 코드 차단")["code"] == "INVALID_KIT_CREDENTIALS"
        for index, (role, _, _) in enumerate(FIXED_DEVICES):
            new = device_snapshot(repo, house, role)
            assert new.pop("kit_id") == kit_id and new.pop("hardware_id") == hardware[index]["hardware_id"]
            original = saved_devices[role]
            original.pop("kit_id")
            original.pop("hardware_id")
            assert new == original, "기존 기기 설정 변경"
            expect(client.get("/device/v1/config", headers={"X-Device-Credential": credentials[role]}), 200, f"{role} 기존 credential")
        expect(client.request("DELETE", "/me", headers=headers, json={"current_password": password}), 204, "합성 owner 탈퇴")
        expect(client.get("/me", headers=headers), 401, "탈퇴 토큰 차단")
        # Nine inventory rows remain blocked after owner withdrawal. Remove only
        # this unique synthetic inventory, after exact whole-row conditions.
        after = snapshot(repo.core)
        extra = {key: row for key, row in after.items() if key not in before}
        expected_keys = {(f"KIT#{kit_id}", "META")} | {
            (f"KIT#{kit_id}", f"DEVICE#{item['device_id']}") for item in hardware
        } | {(f"HARDWARE#{item['hardware_id']}", "KIT") for item in hardware}
        assert set(extra) == expected_keys, "예상 밖 합성 잔존: 자동 정리하지 않습니다."
        inventory_meta = extra[(f"KIT#{kit_id}", "META")]
        assert inventory_meta["status"] == "claimed"
        for bound in [inventory_meta] + [extra[(f"HARDWARE#{item['hardware_id']}", "KIT")] for item in hardware]:
            assert bound["claimed_household_id"] == house and bound["claimed_at"] == claimed["claimed_at"]
        assert all(after.get(key) == row for key, row in before.items()), "기존 항목 변경 관찰"
        assert snapshot(repo.alerts) == before_alerts
        operations = []
        for (pk, sk), row in extra.items():
            names = {f"#f{i}": key for i, key in enumerate(row) if key not in {"pk", "sk"}}
            values = {f":f{i}": value for i, (key, value) in enumerate(row.items()) if key not in {"pk", "sk"}}
            condition = "attribute_exists(pk) AND " + " AND ".join(f"{key}=:{key[1:]}" for key in names)
            operations.append({"Delete": {
                "TableName": CORE, "Key": repo._ddb({"pk": pk, "sk": sk}),
                "ConditionExpression": condition, "ExpressionAttributeNames": names,
                "ExpressionAttributeValues": repo._ddb(values),
            }})
        result = repo.client.transact_write_items(TransactItems=operations, ClientRequestToken=uuid.uuid4().hex)
        assert result.get("ResponseMetadata", {}).get("HTTPStatusCode") == 200
        assert snapshot(repo.core) == before and snapshot(repo.alerts) == before_alerts
        print("KIT REAL DYNAMODB API SMOKE OK / 합성 계정·키트 정리 및 기존 DB 보존")
        print("미검증: 운영 프론트·실기기·MQTT broker·자연 용량 경계. 복구 파일은 비공개로 보관하세요.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"검증 중단: {type(exc).__name__}. 자동 재시도·자동 정리 없음. 복구 파일과 현재 DB부터 확인하세요.", file=sys.stderr)
        raise SystemExit(1) from None
