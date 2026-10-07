"""Signup age self-declaration and truthful, immutable consent evidence."""

from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta

import pytest

from hearo_backend import consent
from hearo_backend.domain import Household, User, parse_timestamp
from hearo_backend.legal_storage import consent_receipt
from hearo_backend.schemas import SignupRequest
from hearo_backend.security import hash_password
from hearo_backend.store import DynamoRepository

from .conftest import auth_header
from .test_consent_storage import DeletionTable, decode, dynamo, user
from .test_legal_consent import PRIVACY_VERSION, TERMS_VERSION, consent_payload


MISSING = object()


def signup_payload(kind="family_member", **overrides):
    payload = {
        "login_id": "agetest01", "name": "나이 확인 테스트",
        "phone_number": "010-3456-7890", "password": "AgeTestPassword123",
        "signup_type": kind, "terms_service_agreed": True,
        "privacy_agreed": True, "age_over_14_agreed": True,
    }
    if kind == "new_household":
        payload["household_name"] = "합성 나이 확인 가구"
    payload.update(overrides)
    return payload


def configure_policies(api, *, required=False):
    api.settings = replace(
        api.settings, terms_version=TERMS_VERSION, privacy_version=PRIVACY_VERSION,
        legal_effective_at="2000-01-01T00:00:00Z", terms_url="https://example.test/terms",
        privacy_url="https://example.test/privacy", legal_consent_required=required,
    )
    api.client.app.state.settings = api.settings


@pytest.mark.parametrize("kind", ["family_member", "new_household"])
@pytest.mark.parametrize("value", [
    MISSING, False, None, 0, 1, 0.0, 1.0, "true", "True", "false", "1", [], {}, [True],
])
def test_only_explicit_json_true_can_create_an_account(api, kind, value):
    payload = signup_payload(kind)
    if value is MISSING:
        payload.pop("age_over_14_agreed")
    else:
        payload["age_over_14_agreed"] = value
    response = api.client.post("/auth/signup", json=payload)
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert "age_over_14_agreed" in response.json()["field_errors"]
    # This includes PROFILE/aliases, household/device and receipt/token maps.
    assert all(value == {} for value in vars(api.repository).values() if isinstance(value, dict))


@pytest.mark.parametrize("kind", ["family_member", "new_household"])
@pytest.mark.parametrize("versioned", [False, True])
def test_signup_stores_three_actual_declarations_at_one_server_time(api, kind, versioned):
    payload = signup_payload(kind)
    if versioned:
        configure_policies(api, required=True)
        payload.update(terms_version=TERMS_VERSION, privacy_version=PRIVACY_VERSION)
    response = api.client.post("/auth/signup", json=payload)
    assert response.status_code == 201, response.text
    result = response.json()
    stored = api.repository.get_user(result["user"]["user_id"])
    assert stored.terms_service_agreed is True and stored.privacy_agreed is True
    assert stored.age_over_14_agreed is True
    assert stored.age_over_14_agreed_at == stored.consented_at
    assert parse_timestamp(stored.age_over_14_agreed_at).utcoffset() == timedelta(0)
    assert stored.age_over_14_agreed_at.endswith("Z")
    receipt, = api.repository.legal_consents[stored.user_id]
    assert receipt["age_over_14_agreed"] is True
    assert receipt["age_over_14_agreed_at"] == receipt["consented_at"] == stored.consented_at
    assert receipt["terms_service_agreed"] is True and receipt["privacy_agreed"] is True
    assert receipt["terms_version"] == (TERMS_VERSION if versioned else None)
    assert receipt["privacy_version"] == (PRIVACY_VERSION if versioned else None)
    assert not {"password_hash", "phone_number", "name", "household_id"} & receipt.keys()
    for profile in (result["user"], api.client.get("/me", headers=auth_header(result)).json()):
        assert profile["age_over_14_agreed"] is True
        assert profile["age_over_14_agreed_at"] == stored.consented_at
    status = api.client.get("/me/consents", headers=auth_header(result)).json()
    assert status["age_over_14"] == {"agreed": True, "agreed_at": stored.consented_at}


@pytest.mark.parametrize("field", ["age_over_14_agreed_at", "consented_at"])
def test_client_cannot_supply_server_agreement_time(api, field):
    response = api.client.post("/auth/signup", json=signup_payload(**{field: "2000-01-01T00:00:00Z"}))
    assert response.status_code == 422
    assert field in response.json()["field_errors"]
    assert api.repository.users == {}


def test_openapi_declares_age_as_required_true_boolean(api):
    document = api.client.get("/openapi.json").json()
    schema = document["components"]["schemas"]["SignupRequest"]
    assert "age_over_14_agreed" in schema["required"]
    assert schema["properties"]["age_over_14_agreed"]["type"] == "boolean"
    assert schema["properties"]["age_over_14_agreed"]["const"] is True
    assert "age_over_14_agreed" not in document["components"]["schemas"]["LegalConsentRequest"]["properties"]
    assert SignupRequest.model_validate(signup_payload()).age_over_14_agreed is True


def test_legal_reconsent_does_not_change_initial_age_time_or_signup_receipt(api, monkeypatch):
    result = api.client.post("/auth/signup", json=signup_payload()).json()
    value = api.repository.get_user(result["user"]["user_id"])
    initial = deepcopy(api.repository.legal_consents[value.user_id])
    age_at = value.age_over_14_agreed_at
    later = parse_timestamp(age_at) + timedelta(seconds=10)

    class LaterDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return later if tz is None else later.astimezone(tz)

    configure_policies(api)
    monkeypatch.setattr(consent, "datetime", LaterDateTime)
    response = api.client.patch("/me/consents", headers=auth_header(result), json=consent_payload())
    assert response.status_code == 200, response.text
    assert response.json()["consented_at"] != age_at
    assert response.json()["age_over_14"] == {"agreed": True, "agreed_at": age_at}
    receipts = api.repository.legal_consents[value.user_id]
    assert len(receipts) == 2 and receipts[0] == initial[0]
    assert receipts[1]["age_over_14_agreed_at"] == age_at
    repeated = api.client.patch("/me/consents", headers=auth_header(result), json=consent_payload())
    assert repeated.json() == response.json()
    assert len(receipts) == 2


def test_legacy_login_and_reconsent_leave_age_unknown(api):
    legacy = User(
        "legacy-age-user", "legacyage01", "기존 사용자", "+821034567890",
        hash_password("AgeTestPassword123"), "family_member",
        terms_service_agreed=True, privacy_agreed=True, consented_at="2026-10-01T00:00:00Z",
    )
    api.repository.create_unlinked_user(legacy)
    assert legacy.user_id not in api.repository.legal_consents
    login = api.client.post("/auth/login", json={"login_id": legacy.login_id, "password": "AgeTestPassword123"})
    assert login.status_code == 200
    result = login.json()
    assert result["user"]["age_over_14_agreed"] is None
    assert result["user"]["age_over_14_agreed_at"] is None
    configure_policies(api)
    before = api.client.get("/me/consents", headers=auth_header(result)).json()
    assert before["age_over_14"] == {"agreed": None, "agreed_at": None}
    accepted = api.client.patch("/me/consents", headers=auth_header(result), json=consent_payload())
    assert accepted.status_code == 200
    assert accepted.json()["age_over_14"] == before["age_over_14"]
    receipt, = api.repository.legal_consents[legacy.user_id]
    assert receipt["age_over_14_agreed"] is None and receipt["age_over_14_agreed_at"] is None


def test_legacy_dynamo_profile_deserializes_missing_age_to_null():
    profile = asdict(user())
    profile.pop("age_over_14_agreed")
    profile.pop("age_over_14_agreed_at")
    value = DynamoRepository._user(profile)
    assert value.age_over_14_agreed is None and value.age_over_14_agreed_at is None
    assert value.public()["age_over_14_agreed"] is None


@pytest.mark.parametrize("field, value", [
    ("age_over_14_agreed", True), ("age_over_14_agreed", False),
    ("age_over_14_agreed_at", "2000-01-01T00:00:00Z"),
])
def test_reconsent_cannot_replace_age_declaration_from_the_client(api, field, value):
    result = api.client.post("/auth/signup", json=signup_payload()).json()
    uid = result["user"]["user_id"]
    profile = asdict(api.repository.get_user(uid))
    receipts = deepcopy(api.repository.legal_consents[uid])
    configure_policies(api)
    response = api.client.patch(
        "/me/consents", headers=auth_header(result), json={**consent_payload(), field: value},
    )
    assert response.status_code == 422 and field in response.json()["field_errors"]
    assert asdict(api.repository.get_user(uid)) == profile
    assert api.repository.legal_consents[uid] == receipts


@pytest.mark.parametrize("owner", [False, True])
@pytest.mark.parametrize("versioned", [False, True])
def test_dynamo_creation_includes_profile_and_age_receipt_in_one_transaction(owner, versioned):
    value = user(
        age_over_14_agreed=True, age_over_14_agreed_at="2026-10-07T00:00:00Z",
        terms_version="v1" if versioned else None, privacy_version="p1" if versioned else None,
    )
    repo = dynamo([value])
    if owner:
        repo.create_owner(value, Household("home-1", "집", value.user_id), [])
    else:
        repo.create_unlinked_user(value)
    assert len(repo.client.transactions) == 1
    items = [decode(operation["Put"]["Item"]) for operation in repo.client.transactions[0]]
    profile, = [item for item in items if item["sk"] == "PROFILE"]
    receipt, = [item for item in items if item["sk"].startswith("CONSENT#")]
    assert receipt["age_over_14_agreed"] is profile["age_over_14_agreed"] is True
    assert receipt["age_over_14_agreed_at"] == profile["age_over_14_agreed_at"]
    assert receipt["consented_at"] == profile["consented_at"]


def test_dynamo_reconsent_fences_but_never_updates_age_fields():
    value = user(age_over_14_agreed=True, age_over_14_agreed_at="2026-10-07T00:00:00Z")
    repo = dynamo([value])
    result = repo.record_legal_consent(value.user_id, "v1", "p1", "2026-10-08T00:00:00Z")
    update = repo.client.transactions[0][0]["Update"]
    assert "age_over_14_agreed=:old_age" in update["ConditionExpression"]
    assert "age_over_14_agreed_at=:old_age_at" in update["ConditionExpression"]
    assert "age_over_14" not in update["UpdateExpression"]
    assert result.age_over_14_agreed_at == value.age_over_14_agreed_at


def test_withdrawal_deletes_age_receipt_and_fences_profile_age_proof():
    value = user(age_over_14_agreed=True, age_over_14_agreed_at="2026-10-07T00:00:00Z")
    repo = dynamo([value])
    repo.core = DeletionTable(value, receipts=1)
    repo.delete_user_account(value.user_id)
    operations = repo.client.transactions[0]
    profile = operations[2]["Delete"]
    assert "age_over_14_agreed=" in profile["ConditionExpression"]
    assert "age_over_14_agreed_at=" in profile["ConditionExpression"]
    assert decode(operations[3]["Delete"]["Key"])["sk"].startswith("CONSENT#")


@pytest.mark.parametrize("age, at", [(True, None), (True, "bad-date"), (False, "2026-10-07T00:00:00Z"), (None, "2026-10-07T00:00:00Z")])
def test_invalid_age_proof_cannot_produce_an_immutable_receipt(age, at):
    with pytest.raises(ValueError, match="age declaration"):
        consent_receipt(user(age_over_14_agreed=age, age_over_14_agreed_at=at))
