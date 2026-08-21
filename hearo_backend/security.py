from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from .config import Settings


LOGIN_ID_PATTERN = re.compile(r"^[a-z0-9._-]{4,30}$")
PASSWORD_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)


class TokenError(ValueError):
    pass


def normalize_login_id(login_id: str) -> str:
    normalized = login_id.strip().casefold()
    if not LOGIN_ID_PATTERN.fullmatch(normalized):
        raise ValueError("로그인 아이디 형식이 올바르지 않습니다.")
    return normalized


def validate_password(password: str) -> None:
    if len(password) < 10:
        raise ValueError("비밀번호는 10자 이상이어야 합니다.")
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        raise ValueError("비밀번호에는 영문자와 숫자가 모두 필요합니다.")


def hash_password(password: str) -> str:
    validate_password(password)
    return PASSWORD_HASHER.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return PASSWORD_HASHER.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def random_secret(bytes_count: int = 32) -> str:
    return secrets.token_urlsafe(bytes_count)


def derive_invite_code(secret: str, household_id: str, nonce: str) -> str:
    """Recreate a shareable code without storing its plaintext in the database."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    digest = hmac.new(
        secret.encode("utf-8"),
        f"hearo-invite:{household_id}:{nonce}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return "".join(alphabet[value & 31] for value in digest[:6])


class TokenManager:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _encode(self, subject: str, token_type: str, lifetime: timedelta, **claims: Any) -> str:
        now = datetime.now(UTC)
        payload = {
            "sub": subject,
            "typ": token_type,
            "iss": self.settings.jwt_issuer,
            "iat": now,
            "exp": now + lifetime,
            "jti": uuid.uuid4().hex,
            **claims,
        }
        return jwt.encode(payload, self.settings.jwt_secret, algorithm="HS256")

    def access(self, user_id: str, token_version: int) -> str:
        return self._encode(
            user_id,
            "access",
            timedelta(minutes=self.settings.access_token_minutes),
            tv=token_version,
        )

    def refresh(self, user_id: str, token_version: int) -> str:
        return self._encode(
            user_id,
            "refresh",
            timedelta(days=self.settings.refresh_token_days),
            tv=token_version,
        )

    def decode(self, token: str, expected_type: str) -> dict[str, Any]:
        try:
            payload = jwt.decode(
                token,
                self.settings.jwt_secret,
                algorithms=["HS256"],
                issuer=self.settings.jwt_issuer,
            )
        except jwt.PyJWTError as exc:
            raise TokenError("유효하지 않거나 만료된 토큰입니다.") from exc
        if payload.get("typ") != expected_type:
            raise TokenError("토큰 종류가 올바르지 않습니다.")
        return payload


class SlidingWindowLimiter:
    """Single-process rate limiter; deployment uses one API worker."""

    def __init__(self):
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, limit: int, window_seconds: int) -> bool:
        now = time.monotonic()
        boundary = now - window_seconds
        with self._lock:
            events = self._events[key]
            while events and events[0] < boundary:
                events.popleft()
            if len(events) >= limit:
                return False
            events.append(now)
            return True
