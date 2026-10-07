from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from hearo_backend.config import Settings
from hearo_backend.domain import iso_utc
from hearo_backend.integrations import NullMqttPublisher
from hearo_backend.main import create_app
from hearo_backend.store import MemoryRepository

from .conftest import auth_header, create_owner


TERMS_VERSION = "terms-test-v1"
PRIVACY_VERSION = "privacy-test-v1"
PAST_EFFECTIVE_AT = "2000-01-01T00:00:00Z"
FUTURE_EFFECTIVE_AT = "2999-01-01T00:00:00Z"


def legal_settings(
    *,
    required: bool = False,
    effective_at: str = PAST_EFFECTIVE_AT,
) -> Settings:
    return Settings(
        environment="test",
        store_backend="memory",
        jwt_secret="test-jwt-secret-that-is-at-least-thirty-two-characters",
        internal_token="test-internal-secret-that-is-at-least-thirty-two-characters",
        terms_version=TERMS_VERSION,
        privacy_version=PRIVACY_VERSION,
        legal_effective_at=effective_at,
        terms_url="https://example.test/terms",
        privacy_url="https://example.test/privacy",
        legal_consent_required=required,
    )


@contextmanager
def running_api(settings: Settings, repository: MemoryRepository | None = None):
    repo = repository or MemoryRepository()
    mqtt = NullMqttPublisher()
    app = create_app(settings, repo, mqtt)
    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client,
            repository=repo,
            mqtt=mqtt,
            settings=settings,
        )


def owner_signup_payload(**overrides):
    payload = {
        "login_id": "legalowner01",
        "phone_number": "010-2345-6789",
        "password": "StrongPassword123",
        "name": "법률동의",
        "signup_type": "new_household",
        "household_name": "법률동의 가구",
        "terms_service_agreed": True,
        "privacy_agreed": True,
    }
    payload.update(overrides)
    return payload


def consent_payload(
    *,
    terms_version: str = TERMS_VERSION,
    privacy_version: str = PRIVACY_VERSION,
):
    return {
        "terms_service_agreed": True,
        "privacy_agreed": True,
        "terms_version": terms_version,
        "privacy_version": privacy_version,
    }


def test_unconfigured_policy_keeps_legacy_signup_compatible(api):
    policies = api.client.get("/legal/policies")
    assert policies.status_code == 200
    assert policies.headers["cache-control"] == "no-store"
    assert policies.json() == {
        "configured": False,
        "effective": False,
        "required": False,
        "effective_at": None,
        "terms": {"version": None, "url": None},
        "privacy": {"version": None, "url": None},
    }

    owner = create_owner(api)
    assert owner["user"]["terms_version"] is None
    assert owner["user"]["privacy_version"] is None
    assert owner["user"]["consented_at"] is not None

    status = api.client.get("/me/consents", headers=auth_header(owner))
    assert status.status_code == 200
    assert status.headers["cache-control"] == "no-store"
    assert status.json()["status"] == "policy_not_configured"
    assert status.json()["can_consent"] is False

    rejected = api.client.patch(
        "/me/consents",
        headers=auth_header(owner),
        json=consent_payload(),
    )
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "LEGAL_POLICY_NOT_CONFIGURED"


def test_signup_rejects_versions_when_policy_is_not_configured(api):
    response = api.client.post(
        "/auth/signup",
        json=owner_signup_payload(
            terms_version=TERMS_VERSION,
            privacy_version=PRIVACY_VERSION,
        ),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "LEGAL_POLICY_NOT_CONFIGURED"
    assert api.repository.get_user_by_login_id("legalowner01") is None


def test_optional_active_policy_never_assigns_omitted_versions_and_supports_reconsent():
    with running_api(legal_settings(required=False)) as api:
        owner = create_owner(api)
        assert owner["user"]["terms_version"] is None
        assert owner["user"]["privacy_version"] is None

        before = api.client.get("/me/consents", headers=auth_header(owner))
        assert before.status_code == 200
        assert before.json()["status"] == "legacy_unversioned"
        assert before.json()["consent_required"] is False
        assert before.json()["can_consent"] is True

        wrong = api.client.patch(
            "/me/consents",
            headers=auth_header(owner),
            json=consent_payload(privacy_version="privacy-wrong"),
        )
        assert wrong.status_code == 409
        assert wrong.json()["code"] == "LEGAL_VERSION_MISMATCH"

        accepted = api.client.patch(
            "/me/consents",
            headers=auth_header(owner),
            json=consent_payload(),
        )
        assert accepted.status_code == 200
        assert accepted.headers["cache-control"] == "no-store"
        assert accepted.json()["status"] == "current"
        assert accepted.json()["can_consent"] is False
        assert accepted.json()["terms"]["accepted_version"] == TERMS_VERSION
        assert accepted.json()["privacy"]["accepted_version"] == PRIVACY_VERSION
        first_accepted_at = accepted.json()["consented_at"]

        repeated = api.client.patch(
            "/me/consents",
            headers=auth_header(owner),
            json=consent_payload(),
        )
        assert repeated.status_code == 200
        assert repeated.json()["consented_at"] == first_accepted_at

        stored = api.repository.get_user(owner["user"]["user_id"])
        assert stored.terms_version == TERMS_VERSION
        assert stored.privacy_version == PRIVACY_VERSION


def test_consent_update_only_accepts_true_agreement_values():
    with running_api(legal_settings()) as api:
        owner = create_owner(api)
        response = api.client.patch(
            "/me/consents",
            headers=auth_header(owner),
            json={**consent_payload(), "privacy_agreed": False},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize(
    ("terms_agreed", "privacy_agreed", "consented_at"),
    [
        (False, True, "2026-10-07T00:00:00Z"),
        (True, False, "2026-10-07T00:00:00+09:00"),
        (True, True, None),
        (True, True, "2026-10-07T00:00:00"),
        (True, True, "not-a-timestamp"),
    ],
)
def test_patch_repairs_incomplete_or_invalid_current_version_consent(
    terms_agreed,
    privacy_agreed,
    consented_at,
):
    repository = MemoryRepository()
    with running_api(legal_settings(), repository) as api:
        owner = create_owner(api)
        user_id = owner["user"]["user_id"]
        corrupted = repository.get_user(user_id)
        corrupted.terms_version = TERMS_VERSION
        corrupted.privacy_version = PRIVACY_VERSION
        corrupted.terms_service_agreed = terms_agreed
        corrupted.privacy_agreed = privacy_agreed
        corrupted.consented_at = consented_at

        before = api.client.get("/me/consents", headers=auth_header(owner))
        assert before.status_code == 200
        assert before.json()["status"] == "outdated"
        assert before.json()["can_consent"] is True

        accepted = api.client.patch(
            "/me/consents",
            headers=auth_header(owner),
            json=consent_payload(),
        )
        assert accepted.status_code == 200
        assert accepted.json()["status"] == "current"
        assert accepted.json()["terms"]["is_current"] is True
        assert accepted.json()["privacy"]["is_current"] is True
        assert accepted.json()["consented_at"].endswith("Z")

        stored = repository.get_user(user_id)
        assert stored.terms_service_agreed is True
        assert stored.privacy_agreed is True
        assert len(repository.legal_consents[user_id]) == 1


def test_required_active_policy_requires_both_current_versions_at_signup():
    with running_api(legal_settings(required=True)) as api:
        missing = api.client.post("/auth/signup", json=owner_signup_payload())
        assert missing.status_code == 409
        assert missing.json()["code"] == "LEGAL_CONSENT_REQUIRED"
        assert api.repository.get_user_by_login_id("legalowner01") is None

        partial = api.client.post(
            "/auth/signup",
            json=owner_signup_payload(terms_version=TERMS_VERSION),
        )
        assert partial.status_code == 409
        assert partial.json()["code"] == "LEGAL_VERSION_MISMATCH"

        accepted = api.client.post(
            "/auth/signup",
            json=owner_signup_payload(
                terms_version=TERMS_VERSION,
                privacy_version=PRIVACY_VERSION,
            ),
        )
        assert accepted.status_code == 201
        body = accepted.json()
        assert body["user"]["terms_version"] == TERMS_VERSION
        assert body["user"]["privacy_version"] == PRIVACY_VERSION
        assert body["user"]["consented_at"] is not None

        current = api.client.get("/me/consents", headers=auth_header(body))
        assert current.json()["status"] == "current"
        assert current.json()["consent_required"] is False


def test_policy_cannot_be_accepted_before_its_effective_time():
    with running_api(
        legal_settings(required=True, effective_at=FUTURE_EFFECTIVE_AT)
    ) as required_api:
        missing = required_api.client.post(
            "/auth/signup",
            json=owner_signup_payload(),
        )
        assert missing.status_code == 409
        assert missing.json()["code"] == "LEGAL_POLICY_NOT_EFFECTIVE"

        submitted = required_api.client.post(
            "/auth/signup",
            json=owner_signup_payload(
                terms_version=TERMS_VERSION,
                privacy_version=PRIVACY_VERSION,
            ),
        )
        assert submitted.status_code == 409
        assert submitted.json()["code"] == "LEGAL_POLICY_NOT_EFFECTIVE"

    with running_api(
        legal_settings(required=False, effective_at=FUTURE_EFFECTIVE_AT)
    ) as optional_api:
        owner = create_owner(optional_api)
        status = optional_api.client.get(
            "/me/consents", headers=auth_header(owner)
        )
        assert status.json()["status"] == "policy_not_effective"
        assert status.json()["can_consent"] is False

        rejected = optional_api.client.patch(
            "/me/consents",
            headers=auth_header(owner),
            json=consent_payload(),
        )
        assert rejected.status_code == 409
        assert rejected.json()["code"] == "LEGAL_POLICY_NOT_EFFECTIVE"


def test_required_reconsent_does_not_block_existing_login_or_device_apis():
    repository = MemoryRepository()
    with running_api(legal_settings(required=False), repository) as initial_api:
        owner = create_owner(initial_api)
        household_id = owner["user"]["household_id"]

    with running_api(legal_settings(required=True), repository) as required_api:
        login = required_api.client.post(
            "/auth/login",
            json={"login_id": "owner01", "password": "StrongPassword123"},
        )
        assert login.status_code == 200
        logged_in = login.json()

        consent = required_api.client.get(
            "/me/consents", headers=auth_header(logged_in)
        )
        assert consent.json()["status"] == "legacy_unversioned"
        assert consent.json()["consent_required"] is True

        devices = required_api.client.get(
            f"/households/{household_id}/devices",
            headers=auth_header(logged_in),
        )
        assert devices.status_code == 200
        assert len(devices.json()["devices"]) == 4

        alert = required_api.client.post(
            "/internal/mqtt/alert",
            headers={"X-Internal-Token": required_api.settings.internal_token},
            json={
                "household_id": household_id,
                "event_id": "reconsent-compatible-alert",
                "timestamp": iso_utc(),
                "source_device_id": "rpi-001",
                "location": "거실",
                "sound": "도어락소리",
                "type": "Visitor",
            },
        )
        assert alert.status_code == 200

        deleted = required_api.client.request(
            "DELETE",
            "/me",
            headers=auth_header(logged_in),
            json={"current_password": "StrongPassword123"},
        )
        assert deleted.status_code == 204


def test_reconsent_endpoint_is_rate_limited_per_user():
    with running_api(legal_settings()) as api:
        owner = create_owner(api)
        headers = auth_header(owner)
        for _ in range(10):
            response = api.client.patch(
                "/me/consents",
                headers=headers,
                json=consent_payload(),
            )
            assert response.status_code == 200

        limited = api.client.patch(
            "/me/consents",
            headers=headers,
            json=consent_payload(),
        )
        assert limited.status_code == 429
        assert limited.json()["code"] == "LEGAL_CONSENT_RATE_LIMITED"


def test_partial_legal_policy_configuration_fails_application_startup():
    settings = Settings(
        environment="test",
        store_backend="memory",
        jwt_secret="test-jwt-secret-that-is-at-least-thirty-two-characters",
        internal_token="test-internal-secret-that-is-at-least-thirty-two-characters",
        terms_version=TERMS_VERSION,
    )
    with pytest.raises(RuntimeError):
        create_app(settings, MemoryRepository(), NullMqttPublisher())
