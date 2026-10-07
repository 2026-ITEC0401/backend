from dataclasses import replace

import pytest

from hearo_backend.config import Settings


def configured(**overrides):
    settings = Settings(
        environment="test", terms_version="2026-10-20.v1",
        privacy_version="2026-10-20.v1", legal_effective_at="2026-10-19T15:00:00Z",
        terms_url="https://example.com/legal/terms/2026-10-20.v1",
        privacy_url="https://example.com/legal/privacy/2026-10-20.v1",
    )
    return replace(settings, **overrides)


def test_unpublished_documents_leave_safe_legacy_mode():
    settings = Settings(environment="test")
    settings.validate_legal_policy()
    assert settings.legal_consent_required is False
    assert settings.terms_version == settings.privacy_version == ""


def test_complete_legal_configuration_is_valid():
    configured().validate_legal_policy()


@pytest.mark.parametrize("overrides", [
    {"privacy_version": ""}, {"legal_effective_at": "2026-10-20"},
    {"legal_effective_at": "invalid"}, {"terms_version": "공개버전"},
    {"terms_version": "x" * 65}, {"terms_url": "http://example.com/terms"},
    {"terms_url": "https://username:password@example.com/terms"},
    {"privacy_url": "https://example.com/privacy?access_token=do-not-log"},
    {"privacy_url": "https://example.com/privacy#fragment"},
    {"privacy_url": "https://example.com/pri\nvacy"},
])
def test_invalid_configuration_fails_closed(overrides):
    with pytest.raises(RuntimeError):
        configured(**overrides).validate_legal_policy()


def test_required_consent_cannot_be_enabled_without_documents():
    with pytest.raises(RuntimeError):
        Settings(environment="test", legal_consent_required=True).validate_legal_policy()


def test_http_localhost_is_only_a_development_exception():
    configured(terms_url="http://localhost:5173/terms").validate_legal_policy()
    with pytest.raises(RuntimeError):
        configured(environment="production", terms_url="http://localhost:5173/terms").validate_legal_policy()


@pytest.mark.parametrize("value", ["1", "yes", "tru", "", "off"])
def test_required_consent_env_typo_cannot_silently_disable_it(monkeypatch, value):
    monkeypatch.setenv("HEARO_LEGAL_CONSENT_REQUIRED", value)
    with pytest.raises(RuntimeError, match="must be true or false"):
        Settings(environment="test")


def test_required_consent_env_allows_whitespace_and_case(monkeypatch):
    monkeypatch.setenv("HEARO_LEGAL_CONSENT_REQUIRED", " TRUE ")
    assert Settings(environment="test").legal_consent_required is True
