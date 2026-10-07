"""Minimal immutable consent receipts, not a copy of the user's profile."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from .domain import User, iso_utc


def valid_consent_time(value: object) -> datetime | None:
    """An absent, malformed or timezone-less legacy value is not consent proof."""
    if not isinstance(value, str) or not value:
        return None
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if at.tzinfo is None or at.utcoffset() is None:
            return None
        return at.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def consent_receipt(user: User) -> dict[str, Any] | None:
    versioned = user.terms_version is not None or user.privacy_version is not None
    has_age_declaration = (
        user.age_over_14_agreed is not None or user.age_over_14_agreed_at is not None
    )
    if not versioned and not has_age_declaration:
        # An old unversioned account has no new declaration to record.
        return None
    at_value = valid_consent_time(user.consented_at)
    if versioned and (not user.terms_version or not user.privacy_version):
        raise ValueError("A versioned consent requires both document versions and the actual consent time")
    if (at_value is None or user.terms_service_agreed is not True
            or user.privacy_agreed is not True):
        raise ValueError("A consent receipt requires both agreements and the actual consent time")
    age_at = valid_consent_time(user.age_over_14_agreed_at)
    if has_age_declaration and (user.age_over_14_agreed is not True or age_at is None):
        raise ValueError("An age declaration requires true and its actual server-recorded time")
    at = iso_utc(at_value)
    # Preserve existing versioned receipt keys. New unversioned signup receipts
    # prove actual declarations without falsely assigning unpublished versions.
    fingerprint_input = (
        f"{user.terms_version}\0{user.privacy_version}"
        if versioned else "signup-unversioned\0age-over-14"
    )
    fingerprint = hashlib.sha256(
        fingerprint_input.encode("utf-8")
    ).hexdigest()[:16]
    return {
        "pk": f"USER#{user.user_id}",
        "sk": f"CONSENT#{at}#{fingerprint}",
        "user_id": user.user_id,
        "terms_version": user.terms_version,
        "privacy_version": user.privacy_version,
        "terms_service_agreed": True,
        "privacy_agreed": True,
        "consented_at": at,
        "age_over_14_agreed": user.age_over_14_agreed,
        "age_over_14_agreed_at": iso_utc(age_at) if age_at is not None else None,
    }
