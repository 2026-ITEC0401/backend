from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .domain import User, iso_utc, parse_timestamp
from .legal_storage import valid_consent_time


class LegalConsentError(Exception):
    """A client-correctable legal-policy or consent state conflict."""

    def __init__(
        self,
        code: str,
        message: str,
        field_errors: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.field_errors = field_errors or {}


@dataclass(frozen=True)
class LegalPolicyState:
    terms_version: str | None
    privacy_version: str | None
    effective_at: str | None
    terms_url: str | None
    privacy_url: str | None
    required: bool
    configured: bool
    effective: bool


def _optional_setting(settings, name: str) -> str | None:
    value = getattr(settings, name, "")
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def legal_policy_state(
    settings,
    now: datetime | None = None,
) -> LegalPolicyState:
    """Return the public policy state without assigning versions to a user."""

    terms_version = _optional_setting(settings, "terms_version")
    privacy_version = _optional_setting(settings, "privacy_version")
    effective_at = _optional_setting(settings, "legal_effective_at")
    terms_url = _optional_setting(settings, "terms_url")
    privacy_url = _optional_setting(settings, "privacy_url")
    required = bool(getattr(settings, "legal_consent_required", False))

    configured = all(
        (terms_version, privacy_version, effective_at, terms_url, privacy_url)
    )
    effective = False
    if configured:
        try:
            starts_at = parse_timestamp(effective_at)
        except (TypeError, ValueError):
            configured = False
        else:
            current = (now or datetime.now(UTC)).astimezone(UTC)
            effective = current >= starts_at

    return LegalPolicyState(
        terms_version=terms_version,
        privacy_version=privacy_version,
        effective_at=effective_at,
        terms_url=terms_url,
        privacy_url=privacy_url,
        required=required,
        configured=configured,
        effective=effective,
    )


def policies_response(settings, now: datetime | None = None) -> dict[str, Any]:
    policy = legal_policy_state(settings, now)
    return {
        "configured": policy.configured,
        "effective": policy.effective,
        "required": policy.required,
        "effective_at": policy.effective_at,
        "terms": {
            "version": policy.terms_version,
            "url": policy.terms_url,
        },
        "privacy": {
            "version": policy.privacy_version,
            "url": policy.privacy_url,
        },
    }


def _not_configured() -> LegalConsentError:
    return LegalConsentError(
        "LEGAL_POLICY_NOT_CONFIGURED",
        "현재 동의할 약관과 개인정보 처리방침이 설정되지 않았습니다.",
    )


def _not_effective() -> LegalConsentError:
    return LegalConsentError(
        "LEGAL_POLICY_NOT_EFFECTIVE",
        "약관과 개인정보 처리방침의 시행일 전에는 동의할 수 없습니다.",
    )


def _consent_required() -> LegalConsentError:
    return LegalConsentError(
        "LEGAL_CONSENT_REQUIRED",
        "현재 약관과 개인정보 처리방침에 대한 동의가 필요합니다.",
        {
            "terms_version": "현재 약관 버전을 확인해 주세요.",
            "privacy_version": "현재 개인정보 처리방침 버전을 확인해 주세요.",
        },
    )


def _version_mismatch(policy: LegalPolicyState) -> LegalConsentError:
    return LegalConsentError(
        "LEGAL_VERSION_MISMATCH",
        "동의한 문서 버전이 현재 버전과 일치하지 않습니다.",
        {
            "terms_version": f"현재 약관 버전은 {policy.terms_version}입니다.",
            "privacy_version": (
                f"현재 개인정보 처리방침 버전은 {policy.privacy_version}입니다."
            ),
        },
    )


def validate_signup_versions(
    settings,
    terms_version: str | None,
    privacy_version: str | None,
    now: datetime | None = None,
) -> tuple[str | None, str | None]:
    """Validate optional signup versions without silently filling either value."""

    policy = legal_policy_state(settings, now)
    submitted = terms_version is not None or privacy_version is not None

    if policy.required and policy.configured and not policy.effective:
        raise _not_effective()

    if not submitted:
        if policy.required:
            if not policy.configured:
                raise _not_configured()
            raise _consent_required()
        return None, None

    if not policy.configured:
        raise _not_configured()
    if not policy.effective:
        raise _not_effective()
    if (
        terms_version != policy.terms_version
        or privacy_version != policy.privacy_version
    ):
        raise _version_mismatch(policy)
    return policy.terms_version, policy.privacy_version


def validate_current_versions(
    settings,
    terms_version: str,
    privacy_version: str,
    now: datetime | None = None,
) -> LegalPolicyState:
    policy = legal_policy_state(settings, now)
    if not policy.configured:
        raise _not_configured()
    if not policy.effective:
        raise _not_effective()
    if (
        terms_version != policy.terms_version
        or privacy_version != policy.privacy_version
    ):
        raise _version_mismatch(policy)
    return policy


def consent_status(
    user: User,
    settings,
    now: datetime | None = None,
) -> dict[str, Any]:
    policy = legal_policy_state(settings, now)
    has_consent_time = valid_consent_time(user.consented_at) is not None
    terms_current = bool(
        policy.effective
        and user.terms_service_agreed is True
        and has_consent_time
        and user.terms_version is not None
        and user.terms_version == policy.terms_version
    )
    privacy_current = bool(
        policy.effective
        and user.privacy_agreed is True
        and has_consent_time
        and user.privacy_version is not None
        and user.privacy_version == policy.privacy_version
    )
    all_current = terms_current and privacy_current

    if not policy.configured:
        status = "policy_not_configured"
    elif not policy.effective:
        status = "policy_not_effective"
    elif all_current:
        status = "current"
    elif user.terms_version is None and user.privacy_version is None:
        status = "legacy_unversioned"
    else:
        status = "outdated"

    can_consent = policy.configured and policy.effective and not all_current
    return {
        "status": status,
        "consent_required": bool(policy.required and can_consent),
        "can_consent": can_consent,
        "consented_at": user.consented_at,
        "terms": {
            "agreed": user.terms_service_agreed,
            "accepted_version": user.terms_version,
            "current_version": policy.terms_version,
            "is_current": terms_current,
        },
        "privacy": {
            "agreed": user.privacy_agreed,
            "accepted_version": user.privacy_version,
            "current_version": policy.privacy_version,
            "is_current": privacy_current,
        },
    }


def record_current_consent(
    repository,
    user: User,
    settings,
    terms_version: str,
    privacy_version: str,
    now: datetime | None = None,
) -> User:
    policy = validate_current_versions(
        settings,
        terms_version,
        privacy_version,
        now,
    )
    if (
        user.terms_version == policy.terms_version
        and user.privacy_version == policy.privacy_version
        and user.terms_service_agreed is True
        and user.privacy_agreed is True
        and valid_consent_time(user.consented_at) is not None
    ):
        return user
    accepted_at = iso_utc(now or datetime.now(UTC))
    return repository.record_legal_consent(
        user.user_id,
        policy.terms_version,
        policy.privacy_version,
        accepted_at,
    )
