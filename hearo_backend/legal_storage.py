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
    if user.terms_version is None and user.privacy_version is None:
        return None
    at_value = valid_consent_time(user.consented_at)
    if (not user.terms_version or not user.privacy_version or at_value is None
            or user.terms_service_agreed is not True or user.privacy_agreed is not True):
        raise ValueError("A versioned consent requires both document versions and the actual consent time")
    at = iso_utc(at_value)
    fingerprint = hashlib.sha256(
        f"{user.terms_version}\0{user.privacy_version}".encode("utf-8")
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
    }
