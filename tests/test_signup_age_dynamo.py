"""Optional Moto checks: local DynamoDB simulation, never actual AWS evidence.

Install the test-only dependency moto[dynamodb] to run this module.
"""

from dataclasses import asdict, replace

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from fastapi.testclient import TestClient

moto = pytest.importorskip("moto", reason="Optional test-only moto[dynamodb] dependency")

from hearo_backend.config import Settings
from hearo_backend.domain import Household
from hearo_backend.legal_storage import consent_receipt
from hearo_backend.main import create_app
from hearo_backend.store import ConflictError, DynamoRepository

from .conftest import auth_header
from .test_consent_storage import user
from .test_legal_consent import PRIVACY_VERSION, TERMS_VERSION, consent_payload
from .test_signup_age_consent import signup_payload


@pytest.fixture
def ddb():
    with moto.mock_aws():
        resource = boto3.resource("dynamodb", region_name="ap-south-1")
        resource.create_table(
            TableName="age-consent-core-test",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        settings = Settings(
            environment="test", store_backend="dynamodb", mqtt_enabled=False,
            region="ap-south-1", core_table="age-consent-core-test", alerts_table="unused-alerts-test",
            terms_version="", privacy_version="", legal_effective_at="",
            terms_url="", privacy_url="", legal_consent_required=False,
        )
        yield settings, DynamoRepository(settings)


@pytest.mark.parametrize("owner", [False, True])
def test_actual_moto_transaction_failure_keeps_only_existing_receipt(ddb, owner):
    _, repo = ddb
    value = user(age_over_14_agreed=True, age_over_14_agreed_at="2026-10-07T00:00:00Z")
    receipt = consent_receipt(value)
    repo.core.put_item(Item=receipt)
    with pytest.raises(ConflictError):
        if owner:
            repo.create_owner(value, Household("home-age-test", "합성 가구", value.user_id), [])
        else:
            repo.create_unlinked_user(value)
    assert repo.core.scan(ConsistentRead=True)["Items"] == [receipt]


@pytest.mark.parametrize("kind", ["family_member", "new_household"])
@pytest.mark.parametrize("versioned", [False, True])
def test_moto_public_signup_reconsent_and_withdrawal_preserve_then_delete_age_proof(ddb, kind, versioned):
    settings, repo = ddb
    if versioned:
        settings = replace(
            settings, terms_version=TERMS_VERSION, privacy_version=PRIVACY_VERSION,
            legal_effective_at="2000-01-01T00:00:00Z", terms_url="https://example.test/terms",
            privacy_url="https://example.test/privacy", legal_consent_required=True,
        )
    app = create_app(settings, repository=repo)
    with TestClient(app) as client:
        rejected = client.post("/auth/signup", json=signup_payload(kind, age_over_14_agreed=1))
        assert rejected.status_code == 422 and repo.core.scan()["Items"] == []
        payload = signup_payload(kind)
        if versioned:
            payload.update(terms_version=TERMS_VERSION, privacy_version=PRIVACY_VERSION)
        signed = client.post("/auth/signup", json=payload)
        assert signed.status_code == 201, signed.text
        result = signed.json()
        uid = result["user"]["user_id"]
        profile_key = {"pk": f"USER#{uid}", "sk": "PROFILE"}
        raw_profile = repo.client.get_item(
            TableName=settings.core_table, Key=repo._ddb(profile_key), ConsistentRead=True,
        )["Item"]
        assert raw_profile["age_over_14_agreed"] == {"BOOL": True}
        assert raw_profile["age_over_14_agreed_at"] == raw_profile["consented_at"]
        before = repo.core.query(KeyConditionExpression=Key("pk").eq(f"USER#{uid}"), ConsistentRead=True)["Items"]
        initial, = [item for item in before if item["sk"].startswith("CONSENT#")]
        assert initial["age_over_14_agreed"] is True
        age_at = initial["age_over_14_agreed_at"]
        app.state.settings = replace(
            settings, terms_version="terms-next-v2", privacy_version=PRIVACY_VERSION,
            legal_effective_at="2000-01-01T00:00:00Z", terms_url="https://example.test/terms",
            privacy_url="https://example.test/privacy", legal_consent_required=False,
        )
        patched = client.patch(
            "/me/consents", headers=auth_header(result),
            json=consent_payload(terms_version="terms-next-v2"),
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["age_over_14"] == {"agreed": True, "agreed_at": age_at}
        after = repo.core.query(KeyConditionExpression=Key("pk").eq(f"USER#{uid}"), ConsistentRead=True)["Items"]
        receipts = [item for item in after if item["sk"].startswith("CONSENT#")]
        assert len(receipts) == 2 and initial in receipts
        assert all(item["age_over_14_agreed_at"] == age_at for item in receipts)
        removed = client.request("DELETE", "/me", headers=auth_header(result), json={"current_password": payload["password"]})
        assert removed.status_code == 204, removed.text
        assert repo.core.query(KeyConditionExpression=Key("pk").eq(f"USER#{uid}"), ConsistentRead=True)["Items"] == []
        assert client.get("/me", headers=auth_header(result)).status_code == 401


def test_moto_missing_legacy_age_fields_stay_missing_during_legal_reconsent(ddb):
    settings, repo = ddb
    value = user()
    item = {"pk": f"USER#{value.user_id}", "sk": "PROFILE", **asdict(value)}
    item.pop("age_over_14_agreed")
    item.pop("age_over_14_agreed_at")
    repo.core.put_item(Item=item)
    updated = repo.record_legal_consent(value.user_id, "v1", "p1", "2026-10-08T00:00:00Z")
    assert updated.age_over_14_agreed is None and updated.age_over_14_agreed_at is None
    stored = repo.core.get_item(Key={"pk": item["pk"], "sk": "PROFILE"}, ConsistentRead=True)["Item"]
    assert "age_over_14_agreed" not in stored and "age_over_14_agreed_at" not in stored
    receipt, = [row for row in repo.core.query(KeyConditionExpression=Key("pk").eq(item["pk"]), ConsistentRead=True)["Items"] if row["sk"].startswith("CONSENT#")]
    assert receipt["age_over_14_agreed"] is None and receipt["age_over_14_agreed_at"] is None
