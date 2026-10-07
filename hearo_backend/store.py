from __future__ import annotations

import copy
import threading
import uuid
from dataclasses import MISSING, asdict, fields, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from . import retention as alert_retention
from .config import Settings
from .domain import Alert, Device, EmergencyAddress, Household, User, iso_utc, parse_timestamp
from .legal_storage import consent_receipt, valid_consent_time
from .retention import (
    AlertTimestampError,
    alert_expiry_epoch,
    is_expired_timestamp,
    is_visible_alert,
)


class StoreError(ValueError):
    default_code = "STORE_ERROR"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        field_errors: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.code = code or self.default_code
        self.field_errors = field_errors or {}


class ConflictError(StoreError):
    default_code = "CONFLICT"


class NotFoundError(StoreError):
    default_code = "NOT_FOUND"


class InvalidInviteError(StoreError):
    default_code = "INVALID_INVITE_CODE"


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value) if value % 1 else int(value)
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return value


class MemoryRepository:
    def __init__(self):
        self.users: dict[str, User] = {}
        self.users_by_login_id: dict[str, str] = {}
        self.users_by_phone: dict[str, str] = {}
        self.households: dict[str, Household] = {}
        self.members: dict[str, set[str]] = {}
        self.display_names: dict[tuple[str, str, str], str] = {}
        self.devices: dict[tuple[str, str], Device] = {}
        self.device_credentials: dict[str, tuple[str, str]] = {}
        self.invites: dict[str, dict[str, Any]] = {}
        self.refresh_tokens: dict[str, dict[str, Any]] = {}
        self.contacts: dict[str, dict[str, dict[str, Any]]] = {}
        self.alerts: dict[str, list[Alert]] = {}
        self.alarm_last_seen: dict[tuple[str, str], str] = {}
        self.legal_consents: dict[str, list[dict[str, Any]]] = {}
        self._lock = threading.RLock()

    def create_owner(
        self,
        user: User,
        household: Household,
        devices: list[Device],
    ) -> None:
        with self._lock:
            self._check_identity_conflicts(user)
            receipt = consent_receipt(user)
            self.users[user.user_id] = user
            self.users_by_login_id[user.login_id] = user.user_id
            self.users_by_phone[user.phone_number] = user.user_id
            self.households[household.household_id] = household
            self.members[household.household_id] = {user.user_id}
            self.alarm_last_seen[(household.household_id, user.user_id)] = (
                user.linked_at or user.created_at
            )
            self.contacts[household.household_id] = {}
            self.alerts[household.household_id] = []
            if receipt is not None:
                self.legal_consents[user.user_id] = [receipt]
            for device in devices:
                self.devices[(device.household_id, device.device_id)] = device
                if device.credential_hash:
                    self.device_credentials[device.credential_hash] = (
                        device.household_id,
                        device.device_id,
                    )

            if household.invite_hash and household.invite_expires_at:
                self.invites[household.invite_hash] = {
                    "household_id": household.household_id,
                    "expires_at": household.invite_expires_at,
                }

    def _check_identity_conflicts(self, user: User) -> None:
        if user.login_id in self.users_by_login_id:
            raise ConflictError(
                "이미 사용 중인 아이디입니다.",
                code="LOGIN_ID_ALREADY_EXISTS",
                field_errors={"login_id": "다른 아이디를 입력해 주세요."},
            )
        if user.phone_number in self.users_by_phone:
            raise ConflictError(
                "이미 가입된 휴대폰 번호입니다.",
                code="PHONE_NUMBER_ALREADY_EXISTS",
                field_errors={"phone_number": "다른 휴대폰 번호를 입력해 주세요."},
            )

    def create_unlinked_user(self, user: User) -> None:
        with self._lock:
            self._check_identity_conflicts(user)
            receipt = consent_receipt(user)
            self.users[user.user_id] = user
            self.users_by_login_id[user.login_id] = user.user_id
            self.users_by_phone[user.phone_number] = user.user_id
            if receipt is not None:
                self.legal_consents[user.user_id] = [receipt]

    def record_legal_consent(
        self, user_id: str, terms_version: str, privacy_version: str, consented_at: str,
    ) -> User:
        with self._lock:
            user = self.users.get(user_id)
            if user is None:
                raise NotFoundError("사용자를 찾을 수 없습니다.")
            if (user.terms_version == terms_version and user.privacy_version == privacy_version
                    and user.terms_service_agreed is True and user.privacy_agreed is True
                    and valid_consent_time(user.consented_at) is not None):
                return user
            previous_at = valid_consent_time(user.consented_at)
            if previous_at and parse_timestamp(consented_at) < previous_at:
                raise ConflictError("동의 시각의 일관성을 확인할 수 없습니다.", code="LEGAL_CONSENT_CONFLICT")
            updated = replace(
                user, terms_version=terms_version, privacy_version=privacy_version,
                terms_service_agreed=True, privacy_agreed=True,
                consented_at=iso_utc(parse_timestamp(consented_at)),
            )
            receipt = consent_receipt(updated)
            self.legal_consents.setdefault(user_id, []).append(receipt)
            self.users[user_id] = updated
            return updated

    def get_user(self, user_id: str) -> User | None:
        with self._lock:
            return self.users.get(user_id)

    def get_user_consistent(self, user_id: str) -> User | None:
        return self.get_user(user_id)

    def get_user_by_login_id(self, login_id: str) -> User | None:
        with self._lock:
            user_id = self.users_by_login_id.get(login_id)
            return self.users.get(user_id) if user_id else None

    def update_user_password(self, user_id: str, password_hash: str) -> User:
        with self._lock:
            user = self.users.get(user_id)
            if user is None:
                raise NotFoundError("사용자를 찾을 수 없습니다.")
            user.password_hash = password_hash
            user.token_version += 1
            return user

    def delete_user_account(self, user_id: str) -> None:
        with self._lock:
            user = self.users.get(user_id)
            if not user:
                raise NotFoundError("사용자를 찾을 수 없습니다.")
            if user.household_link_status == "linked":
                raise ConflictError(
                    "가구 연결을 먼저 해제해야 합니다.",
                    code="ACCOUNT_STILL_LINKED",
                )

            if self.users_by_login_id.get(user.login_id) == user_id:
                self.users_by_login_id.pop(user.login_id, None)
            if self.users_by_phone.get(user.phone_number) == user_id:
                self.users_by_phone.pop(user.phone_number, None)

            self.refresh_tokens = {
                token_hash: value
                for token_hash, value in self.refresh_tokens.items()
                if value["user_id"] != user_id
            }
            self.display_names = {
                key: value
                for key, value in self.display_names.items()
                if key[1] != user_id and key[2] != user_id
            }
            self.alarm_last_seen = {
                key: value
                for key, value in self.alarm_last_seen.items()
                if key[1] != user_id
            }
            self.users.pop(user_id, None)
            self.legal_consents.pop(user_id, None)

    def withdraw_user_account(
        self,
        user_id: str,
        now: datetime,
        *,
        expected_password_hash: str,
        _unlink_only: bool = False,
    ) -> dict[str, Any]:
        """Atomically apply membership effects and remove a user in memory."""
        with self._lock:
            user = self.users.get(user_id)
            if user is None:
                raise NotFoundError("사용자를 찾을 수 없습니다.")
            if user.user_id != user_id:
                self._raise_account_deletion_review_required()
            if user.password_hash != expected_password_hash:
                raise ConflictError(
                    "계정 상태가 변경되어 삭제를 완료하지 못했습니다.",
                    code="ACCOUNT_DELETION_CONFLICT",
                )

            previous_household_id = user.household_id
            household_status: str | None = None
            household: Household | None = None
            linked_members: list[User] = []
            if user.household_link_status == "linked":
                if not previous_household_id or user.role not in {"owner", "member"}:
                    self._raise_account_deletion_review_required()
                household = self.households.get(previous_household_id)
                member_ids = self.members.get(previous_household_id)
                if (
                    household is None
                    or household.household_id != previous_household_id
                    or household.status != "active"
                    or not isinstance(household.membership_version, int)
                    or isinstance(household.membership_version, bool)
                    or household.membership_version < 0
                    or member_ids is None
                ):
                    self._raise_account_deletion_review_required()
                if user_id not in member_ids:
                    self._raise_account_deletion_review_required()
                if user.role == "owner" and household.owner_user_id != user_id:
                    self._raise_account_deletion_review_required()
                if user.role == "member" and household.owner_user_id == user_id:
                    self._raise_account_deletion_review_required()
                for member_id in sorted(member_ids):
                    member = self.users.get(member_id)
                    if (
                        member is None
                        or member.user_id != member_id
                        or member.household_id != previous_household_id
                        or member.household_link_status != "linked"
                        or member.role not in {"owner", "member"}
                        or (member_id == household.owner_user_id) != (member.role == "owner")
                        or (member.role == "owner")
                        != (member.account_type == "household_owner")
                    ):
                        self._raise_account_deletion_review_required()
                    linked_members.append(member)
                if not any(member.user_id == household.owner_user_id for member in linked_members):
                    self._raise_account_deletion_review_required()
                household_status = "inactive" if user.role == "owner" else "active"
            elif user.household_link_status != "unlinked":
                self._raise_account_deletion_review_required()
            elif (
                user.household_id is not None
                or user.role is not None
                or user.linked_at is not None
                or any(user_id in member_ids for member_ids in self.members.values())
            ):
                self._raise_account_deletion_review_required()

            if _unlink_only and (household is None or user.role != "owner"):
                self._raise_account_deletion_review_required()
            owned_houses = [
                value for value in self.households.values()
                if value.owner_user_id == user_id
            ]
            for owned in owned_houses:
                if (
                    user.account_type != "household_owner"
                    or self.households.get(owned.household_id) is not owned
                    or owned.status not in {"active", "inactive"}
                    or (owned.status == "active" and owned is not household)
                    or (owned.status == "inactive" and self.members.get(owned.household_id))
                ):
                    self._raise_account_deletion_review_required()
                if owned.invite_hash:
                    invite = self.invites.get(owned.invite_hash)
                    if invite is not None and invite.get("household_id") != owned.household_id:
                        self._raise_account_deletion_review_required()
                for (home, device_id), device in self.devices.items():
                    if home != owned.household_id:
                        continue
                    if device.household_id != home or device.device_id != device_id:
                        self._raise_account_deletion_review_required()
                    alias = self.device_credentials.get(device.credential_hash)
                    if alias is not None and alias != (home, device_id):
                        self._raise_account_deletion_review_required()
            if previous_household_id is None and owned_houses:
                previous_household_id = sorted(value.household_id for value in owned_houses)[0]
                household_status = "inactive"

            reference_count = sum(
                value.get("user_id") == user_id for value in self.refresh_tokens.values()
            ) + sum(
                viewer_id == user_id or member_id == user_id
                for _, viewer_id, member_id in self.display_names
            )
            action_count = (
                1 if _unlink_only else
                3 + reference_count + len(self.legal_consents.get(user_id, []))
            )
            if household is not None:
                action_count += 2  # HOUSE metadata plus the withdrawing MEMBER row.
                if user.role == "owner":
                    action_count += len(linked_members) - 1
                    action_count += len(linked_members) - 1
            for owned in owned_houses:
                if owned is not household:
                    action_count += 1  # Historical inactive HOUSE metadata.
                action_count += self._household_registration_count(
                    owned.household_id, None if _unlink_only else user_id
                )
            if action_count > 100:
                self._raise_account_deletion_review_required()

            dictionary_names = [
                "users",
                "users_by_login_id",
                "users_by_phone",
                "households",
                "members",
                "display_names",
                "invites",
                "refresh_tokens",
                "alarm_last_seen",
                "legal_consents",
                "devices",
                "device_credentials",
                "contacts",
            ]
            snapshot = {
                name: copy.deepcopy(getattr(self, name)) for name in dictionary_names
            }
            original_users = dict(self.users)
            original_households = dict(self.households)
            # Mutate detached dataclass copies while holding the lock. A caller
            # that retained an earlier object reference therefore observes
            # either the old state (until commit) or the committed state, never
            # a rollback-only intermediate unlink.
            working_user_ids = (
                [member.user_id for member in linked_members]
                if household is not None and user.role == "owner"
                else [user_id]
            )
            working_users = {
                member_id: copy.deepcopy(original_users[member_id])
                for member_id in working_user_ids
            }
            self.users.update(working_users)
            user = working_users[user_id]
            if household is not None and previous_household_id is not None:
                household = copy.deepcopy(original_households[previous_household_id])
                self.households[previous_household_id] = household
                if user.role == "owner":
                    linked_members = [
                        working_users[member.user_id] for member in linked_members
                    ]
            try:
                if household is not None:
                    household.membership_version += 1
                    if user.role == "owner":
                        household.status = "inactive"
                        household.inactive_at = iso_utc(now)
                        household.emergency_address = None
                        if household.invite_hash:
                            self.invites.pop(household.invite_hash, None)
                        household.invite_hash = None
                        household.invite_nonce = None
                        household.invite_expires_at = None
                        for member in linked_members:
                            self.alarm_last_seen.pop(
                                (previous_household_id, member.user_id), None
                            )
                            member.household_id = None
                            member.role = None
                            member.household_link_status = "unlinked"
                            member.linked_at = None
                        self.members[previous_household_id] = set()
                    else:
                        self.members[previous_household_id].discard(user_id)
                        self.alarm_last_seen.pop((previous_household_id, user_id), None)
                        user.household_id = None
                        user.role = None
                        user.household_link_status = "unlinked"
                        user.linked_at = None
                for owned in owned_houses:
                    self._purge_household_registration(owned.household_id)
                if not _unlink_only:
                    self.delete_user_account(user_id)
            except Exception:
                for name in dictionary_names:
                    current = getattr(self, name)
                    current.clear()
                    if name == "users":
                        current.update(original_users)
                    elif name == "households":
                        current.update(original_households)
                    else:
                        current.update(snapshot[name])
                raise
            # Publish committed fields to any object references handed out
            # before the transaction, while still under the same lock.
            for member_id, working in working_users.items():
                original = original_users[member_id]
                for item_field in fields(User):
                    setattr(original, item_field.name, getattr(working, item_field.name))
                if member_id in self.users:
                    self.users[member_id] = original
            if household is not None and previous_household_id is not None:
                original_household = original_households[previous_household_id]
                for item_field in fields(Household):
                    setattr(
                        original_household,
                        item_field.name,
                        getattr(household, item_field.name),
                    )
                if previous_household_id in self.households:
                    self.households[previous_household_id] = original_household
            return {
                "previous_household_id": previous_household_id,
                "household_status": household_status,
            }

    @staticmethod
    def _raise_account_deletion_review_required() -> None:
        raise ConflictError(
            "관련 기록을 자동으로 검증할 수 없어 관리자 확인이 필요합니다.",
            code="ACCOUNT_DELETION_REVIEW_REQUIRED",
        )

    def _household_registration_count(self, household_id: str, deleting_user_id: str | None) -> int:
        return (
            sum(home == household_id for home, _ in self.devices)
            + sum(home == household_id for home, _ in self.device_credentials.values())
            + len(self.contacts.get(household_id, {}))
            + sum(
                home == household_id and deleting_user_id not in (viewer, member)
                for home, viewer, member in self.display_names
            )
            + sum(value.get("household_id") == household_id for value in self.invites.values())
        )

    def _purge_household_registration(self, household_id: str) -> None:
        """Called under the repository lock; alarm history has a separate lifetime."""
        self.households.pop(household_id, None)
        self.members.pop(household_id, None)
        self.contacts.pop(household_id, None)
        for mapping in (self.devices, self.display_names, self.alarm_last_seen):
            for key in list(mapping):
                if key[0] == household_id:
                    mapping.pop(key)
        for credential, target in list(self.device_credentials.items()):
            if target[0] == household_id:
                self.device_credentials.pop(credential)
        for invite, value in list(self.invites.items()):
            if value.get("household_id") == household_id:
                self.invites.pop(invite)

    def get_household(self, household_id: str) -> Household | None:
        with self._lock:
            return self.households.get(household_id)

    def get_household_consistent(self, household_id: str) -> Household | None:
        return self.get_household(household_id)

    def update_household_emergency_address(
        self, household_id: str, address: EmergencyAddress
    ) -> EmergencyAddress:
        with self._lock:
            household = self.households.get(household_id)
            if not household or household.status != "active":
                raise NotFoundError("가구를 찾을 수 없습니다.")
            household.emergency_address = address
            return address

    def list_members(self, household_id: str) -> list[User]:
        with self._lock:
            return [
                self.users[user_id]
                for user_id in sorted(self.members.get(household_id, set()))
            ]

    def get_display_name(
        self, household_id: str, viewer_user_id: str, member_user_id: str
    ) -> str | None:
        with self._lock:
            return self.display_names.get((household_id, viewer_user_id, member_user_id))

    def set_display_name(
        self,
        household_id: str,
        viewer_user_id: str,
        member_user_id: str,
        display_name: str,
    ) -> None:
        with self._lock:
            viewer = self.users.get(viewer_user_id)
            member = self.users.get(member_user_id)
            linked_users = self.members.get(household_id, set())
            if (
                viewer is None
                or member is None
                or viewer_user_id not in linked_users
                or member_user_id not in linked_users
                or viewer.household_link_status != "linked"
                or member.household_link_status != "linked"
                or viewer.household_id != household_id
                or member.household_id != household_id
                or self.households.get(household_id) is None
                or self.households[household_id].status != "active"
            ):
                raise NotFoundError("가족 구성원을 찾을 수 없습니다.")
            viewer.reference_version += 1
            self.households[household_id].registration_version += 1
            if member_user_id != viewer_user_id:
                member.reference_version += 1
            self.display_names[(household_id, viewer_user_id, member_user_id)] = display_name

    def delete_display_name(
        self, household_id: str, viewer_user_id: str, member_user_id: str
    ) -> None:
        with self._lock:
            if member_user_id not in self.members.get(household_id, set()):
                raise NotFoundError("가족 구성원을 찾을 수 없습니다.")
            self.display_names.pop((household_id, viewer_user_id, member_user_id), None)

    def member_count(self, household_id: str) -> int:
        with self._lock:
            return len(self.members.get(household_id, set()))

    def link_member(
        self, user_id: str, invite_hash: str, now: datetime
    ) -> User:
        with self._lock:
            user = self.users.get(user_id)
            if not user:
                raise NotFoundError("사용자를 찾을 수 없습니다.")
            if user.account_type != "family_member":
                raise ConflictError("가족 계정만 가구에 연동할 수 있습니다.", code="ACCOUNT_TYPE_NOT_LINKABLE")
            if user.household_link_status == "linked":
                raise ConflictError("이미 가구에 연동되어 있습니다.", code="HOUSEHOLD_ALREADY_LINKED")
            invite = self.invites.get(invite_hash)
            if not invite:
                raise InvalidInviteError("초대 코드를 찾을 수 없습니다.", code="INVITE_CODE_NOT_FOUND")
            if parse_timestamp(invite["expires_at"]) <= now:
                raise InvalidInviteError("초대 코드가 만료되었습니다.", code="INVITE_CODE_EXPIRED")
            household = self.households.get(invite["household_id"])
            if not household or household.status != "active" or household.invite_hash != invite_hash:
                raise InvalidInviteError("사용할 수 없는 초대 코드입니다.", code="INVITE_CODE_INACTIVE")
            user.household_id = household.household_id
            user.role = "member"
            user.household_link_status = "linked"
            user.linked_at = iso_utc(now)
            self.members.setdefault(household.household_id, set()).add(user.user_id)
            self.alarm_last_seen[(household.household_id, user.user_id)] = user.linked_at
            household.membership_version += 1
            return user

    def get_or_initialize_alarm_last_seen(
        self,
        household_id: str,
        user_id: str,
        baseline_at: str,
    ) -> str:
        with self._lock:
            if user_id not in self.members.get(household_id, set()):
                raise NotFoundError("가구 구성원을 찾을 수 없습니다.")
            key = (household_id, user_id)
            return self.alarm_last_seen.setdefault(key, baseline_at)

    def mark_alarms_seen(
        self,
        household_id: str,
        user_id: str,
        seen_at: str,
    ) -> str:
        with self._lock:
            if user_id not in self.members.get(household_id, set()):
                raise NotFoundError("가구 구성원을 찾을 수 없습니다.")
            key = (household_id, user_id)
            current = self.alarm_last_seen.get(key)
            if current is None or parse_timestamp(current) <= parse_timestamp(seen_at):
                self.alarm_last_seen[key] = seen_at
            return self.alarm_last_seen[key]

    def rotate_invite(
        self,
        household_id: str,
        invite_hash: str,
        invite_nonce: str,
        expires_at: str,
    ) -> Household:
        with self._lock:
            household = self.households.get(household_id)
            if not household or household.status != "active":
                raise NotFoundError("가구를 찾을 수 없습니다.")
            if invite_hash in self.invites:
                raise ConflictError("이미 등록된 초대 코드입니다.")
            if household.invite_hash:
                previous = self.invites.get(household.invite_hash)
                if previous is not None and previous.get("household_id") != household_id:
                    raise ConflictError("초대 코드의 가구 정보가 일치하지 않습니다.")
                self.invites.pop(household.invite_hash, None)
            household.registration_version += 1
            household.invite_hash = invite_hash
            household.invite_nonce = invite_nonce
            household.invite_expires_at = expires_at
            self.invites[invite_hash] = {"household_id": household_id, "expires_at": expires_at}
            return household

    def get_invite(self, invite_hash: str) -> dict[str, Any] | None:
        with self._lock:
            value = self.invites.get(invite_hash)
            return dict(value) if value else None

    def unlink_user(self, user_id: str, now: datetime) -> dict[str, Any]:
        with self._lock:
            user = self.users.get(user_id)
            if not user or user.household_link_status != "linked" or not user.household_id:
                raise ConflictError("가구에 연동되어 있지 않습니다.", code="HOUSEHOLD_LINK_REQUIRED")
            household_id = user.household_id
            household = self.households.get(household_id)
            if not household or household.status != "active":
                raise ConflictError("활성 가구를 찾을 수 없습니다.", code="HOUSEHOLD_INACTIVE")
            if user.role == "owner":
                self.withdraw_user_account(
                    user_id, now, expected_password_hash=user.password_hash,
                    _unlink_only=True,
                )
                return {"household_link_status": "unlinked", "household_status": "inactive"}
            household.membership_version += 1
            self.members.get(household_id, set()).discard(user.user_id)
            self.alarm_last_seen.pop((household_id, user.user_id), None)
            user.household_id = None
            user.role = None
            user.household_link_status = "unlinked"
            user.linked_at = None
            return {"household_link_status": "unlinked", "household_status": "active"}

    def save_refresh_token(self, token_hash: str, user_id: str, expires_at: str) -> None:
        with self._lock:
            user = self.users.get(user_id)
            if user is None:
                raise NotFoundError("사용자를 찾을 수 없습니다.")
            user.reference_version += 1
            self.refresh_tokens[token_hash] = {
                "user_id": user_id,
                "expires_at": expires_at,
                "used_at": None,
            }

    def consume_refresh_token(self, token_hash: str, now: datetime) -> str:
        with self._lock:
            item = self.refresh_tokens.get(token_hash)
            if not item or item["used_at"] or parse_timestamp(item["expires_at"]) <= now:
                raise NotFoundError("refresh token이 폐기되었거나 만료되었습니다.")
            item["used_at"] = iso_utc(now)
            return item["user_id"]

    def list_devices(self, household_id: str) -> list[Device]:
        return sorted(
            [device for (home, _), device in self.devices.items() if home == household_id],
            key=lambda item: item.device_id,
        )

    def get_device(self, household_id: str, device_id: str) -> Device | None:
        return self.devices.get((household_id, device_id))

    def get_device_by_credential(self, credential_hash: str) -> Device | None:
        with self._lock:
            key = self.device_credentials.get(credential_hash)
            device = self.devices.get(key) if key else None
            if device is None or device.credential_hash != credential_hash:
                return None
            return device

    def rotate_device_credential(self, household_id: str, device_id: str, credential_hash: str) -> Device:
        with self._lock:
            device = self.devices.get((household_id, device_id))
            house = self.households.get(household_id)
            if not device or house is None or house.status != "active":
                raise NotFoundError("기기를 찾을 수 없습니다.")
            if credential_hash in self.device_credentials:
                raise ConflictError("이미 등록된 기기 인증정보입니다.")
            if device.credential_hash:
                self.device_credentials.pop(device.credential_hash, None)
            device.credential_hash = credential_hash
            self.device_credentials[credential_hash] = (household_id, device_id)
            house.registration_version += 1
            return device

    def update_device_desired(self, household_id: str, device_id: str, enabled: bool) -> Device:
        with self._lock:
            device = self.get_device(household_id, device_id)
            if not device:
                raise NotFoundError("기기를 찾을 수 없습니다.")
            device.desired_mqtt_connected = enabled
            device.config_version += 1
            device.desired_updated_at = iso_utc()
            return device

    def update_device_settings(
        self,
        household_id: str,
        device_id: str,
        led_alert_enabled: bool | None,
    ) -> Device:
        with self._lock:
            device = self.get_device(household_id, device_id)
            if not device:
                raise NotFoundError("기기를 찾을 수 없습니다.")
            if device.device_type != "alert_node":
                raise ConflictError(
                    "LED 알림 설정은 ESP32 알림 노드에서만 변경할 수 있습니다.",
                    code="DEVICE_LED_CONTROL_UNSUPPORTED",
                )
            if led_alert_enabled is not None:
                device.led_alert_enabled = led_alert_enabled
            device.config_version += 1
            device.desired_updated_at = iso_utc()
            return device

    def update_device_reported(
        self,
        household_id: str,
        device_id: str,
        mqtt_connected: bool,
        config_version: int,
        firmware_version: str | None,
        seen_at: str | None = None,
        network_online: bool = True,
        audio_streaming: bool | None = None,
        microphone_ok: bool | None = None,
        audio_packets_sent: int | None = None,
        audio_packets_dropped: int | None = None,
        audio_clipped_samples: int | None = None,
    ) -> Device:
        with self._lock:
            device = self.get_device(household_id, device_id)
            if not device:
                raise NotFoundError("기기를 찾을 수 없습니다.")
            if config_version >= device.reported_config_version:
                device.reported_mqtt_connected = mqtt_connected
                device.reported_config_version = config_version
            device.last_seen_at = seen_at or iso_utc()
            device.reported_network_online = network_online
            if firmware_version:
                device.firmware_version = firmware_version
            if audio_streaming is not None:
                device.audio_streaming = audio_streaming
            if microphone_ok is not None:
                device.microphone_ok = microphone_ok
            if audio_packets_sent is not None:
                device.audio_packets_sent = audio_packets_sent
            if audio_packets_dropped is not None:
                device.audio_packets_dropped = audio_packets_dropped
            if audio_clipped_samples is not None:
                device.audio_clipped_samples = audio_clipped_samples
            return device

    def list_contacts(self, household_id: str) -> list[dict[str, Any]]:
        return sorted(self.contacts.get(household_id, {}).values(), key=lambda item: item["name"])

    def create_contact(self, household_id: str, contact: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            house = self.households.get(household_id)
            if house is None or house.status != "active":
                raise NotFoundError("가구를 찾을 수 없습니다.")
            house.registration_version += 1
            self.contacts.setdefault(household_id, {})[contact["contact_id"]] = contact
            return contact

    def update_contact(self, household_id: str, contact_id: str, contact: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            current = self.contacts.get(household_id, {}).get(contact_id)
            house = self.households.get(household_id)
            if not current or house is None or house.status != "active":
                raise NotFoundError("연락처를 찾을 수 없습니다.")
            house.registration_version += 1
            current.update(contact)
            return dict(current)

    def delete_contact(self, household_id: str, contact_id: str) -> None:
        with self._lock:
            if not self.contacts.get(household_id, {}).pop(contact_id, None):
                raise NotFoundError("연락처를 찾을 수 없습니다.")

    def put_alert(self, alert: Alert) -> bool:
        try:
            expires_at_epoch = alert_expiry_epoch(alert.timestamp)
        except AlertTimestampError as exc:
            raise StoreError(
                "알림 발생 시각이 올바르지 않습니다.",
                code="INVALID_ALERT_TIMESTAMP",
                field_errors={"timestamp": "유효한 ISO 8601 시각을 입력해 주세요."},
            ) from exc
        alert.expires_at_epoch = expires_at_epoch
        if is_expired_timestamp(alert.timestamp, alert_retention.utc_now()):
            return False
        with self._lock:
            house = self.households.get(alert.household_id)
            if house is None or house.status != "active":
                return False
            values = self.alerts.setdefault(alert.household_id, [])
            if any(existing.event_id == alert.event_id for existing in values):
                return False
            values.append(alert)
            return True

    def query_alerts(self, household_id: str, start: datetime, end_exclusive: datetime) -> list[Alert]:
        now = alert_retention.utc_now()
        return sorted(
            [
                alert
                for alert in self.alerts.get(household_id, [])
                if is_visible_alert(alert, now)
                and start <= parse_timestamp(alert.timestamp) < end_exclusive
            ],
            key=lambda item: parse_timestamp(item.timestamp),
            reverse=True,
        )

    def get_alert(self, household_id: str, event_id: str) -> Alert | None:
        now = alert_retention.utc_now()
        return next(
            (
                alert
                for alert in self.alerts.get(household_id, [])
                if alert.event_id == event_id
                and is_visible_alert(alert, now)
            ),
            None,
        )

    def latest_alerts(self, household_id: str, limit: int) -> list[Alert]:
        now = alert_retention.utc_now()
        return sorted(
            [
                alert
                for alert in self.alerts.get(household_id, [])
                if is_visible_alert(alert, now)
            ],
            key=lambda item: parse_timestamp(item.timestamp),
            reverse=True,
        )[:limit]


class DynamoRepository:
    """DynamoDB implementation using a core single-table plus an alerts table."""

    def __init__(self, settings: Settings):
        import boto3

        self.settings = settings
        self.resource = boto3.resource("dynamodb", region_name=settings.region)
        self.client = boto3.client("dynamodb", region_name=settings.region)
        self.core = self.resource.Table(settings.core_table)
        self.alerts = self.resource.Table(settings.alerts_table)

    @staticmethod
    def _ddb(item: dict[str, Any]) -> dict[str, Any]:
        from boto3.dynamodb.types import TypeSerializer

        serializer = TypeSerializer()
        return {key: serializer.serialize(value) for key, value in item.items()}

    @staticmethod
    def _user(item: dict[str, Any] | None) -> User | None:
        if not item:
            return None
        data = _plain(item)
        values: dict[str, Any] = {}
        for item_field in fields(User):
            if item_field.name in data:
                values[item_field.name] = data[item_field.name]
            elif item_field.default is not MISSING:
                values[item_field.name] = item_field.default
            elif item_field.default_factory is not MISSING:
                values[item_field.name] = item_field.default_factory()
        return User(**values)

    @staticmethod
    def _device(item: dict[str, Any] | None) -> Device | None:
        if not item:
            return None
        data = _plain(item)
        values: dict[str, Any] = {}
        for item_field in fields(Device):
            if item_field.name in data:
                values[item_field.name] = data[item_field.name]
            elif item_field.default is not MISSING:
                values[item_field.name] = item_field.default
            elif item_field.default_factory is not MISSING:
                values[item_field.name] = item_field.default_factory()
        return Device(**values)

    @staticmethod
    def _household(item: dict[str, Any] | None) -> Household | None:
        if not item:
            return None
        data = _plain(item)
        raw_address = data.get("emergency_address")
        address = EmergencyAddress(**raw_address) if isinstance(raw_address, dict) else None
        values = {
            key: data[key]
            for key in Household.__dataclass_fields__
            if key in data and key != "emergency_address"
        }
        values["emergency_address"] = address
        return Household(**values)

    def create_owner(self, user: User, household: Household, devices: list[Device]) -> None:
        puts = [
            {
                "Item": self._ddb(
                    {"pk": f"LOGINID#{user.login_id}", "sk": "USER", "user_id": user.user_id}
                ),
                "ConditionExpression": "attribute_not_exists(pk)",
            },
            {
                "Item": self._ddb(
                    {"pk": f"PHONE#{user.phone_number}", "sk": "USER", "user_id": user.user_id}
                ),
                "ConditionExpression": "attribute_not_exists(pk)",
            },
            {"Item": self._ddb({"pk": f"USER#{user.user_id}", "sk": "PROFILE", **asdict(user)})},
            {
                "Item": self._ddb(
                    {"pk": f"HOUSE#{household.household_id}", "sk": "META", **asdict(household)}
                )
            },
            {
                "Item": self._ddb(
                    {
                        "pk": f"HOUSE#{household.household_id}",
                        "sk": f"MEMBER#{user.user_id}",
                        "user_id": user.user_id,
                        "linked_at": user.linked_at,
                        "alarms_last_seen_at": user.linked_at or user.created_at,
                    }
                )
            },
        ]
        receipt = consent_receipt(user)
        if receipt is not None:
            puts.append({
                "Item": self._ddb(receipt),
                "ConditionExpression": "attribute_not_exists(pk)",
            })
        if household.invite_hash and household.invite_expires_at:
            puts.append(
                {
                    "Item": self._ddb(
                        {
                            "pk": f"INVITE#{household.invite_hash}",
                            "sk": "INVITE",
                            "household_id": household.household_id,
                            "expires_at": household.invite_expires_at,
                            "expires_at_epoch": int(
                                parse_timestamp(household.invite_expires_at).timestamp()
                            ),
                        }
                    ),
                    "ConditionExpression": "attribute_not_exists(pk)",
                }
            )
        for device in devices:
            puts.append(
                {
                    "Item": self._ddb(
                        {
                            "pk": f"HOUSE#{device.household_id}",
                            "sk": f"DEVICE#{device.device_id}",
                            **asdict(device),
                        }
                    )
                }
            )
            if device.credential_hash:
                puts.append(
                    {
                        "Item": self._ddb(
                            {
                                "pk": f"DEVICECRED#{device.credential_hash}",
                                "sk": "DEVICE",
                                "household_id": device.household_id,
                                "device_id": device.device_id,
                            }
                        ),
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                )
        try:
            self.client.transact_write_items(
                TransactItems=[
                    {"Put": {"TableName": self.settings.core_table, **put}}
                    for put in puts
                ]
            )
        except Exception as exc:
            self._raise_identity_conflict(user, exc)

    def _raise_identity_conflict(self, user: User, exc: Exception) -> None:
        if self.get_user_by_login_id(user.login_id):
            raise ConflictError(
                "이미 사용 중인 아이디입니다.",
                code="LOGIN_ID_ALREADY_EXISTS",
                field_errors={"login_id": "다른 아이디를 입력해 주세요."},
            ) from exc
        phone_alias = self.core.get_item(
            Key={"pk": f"PHONE#{user.phone_number}", "sk": "USER"}
        ).get("Item")
        if phone_alias:
            raise ConflictError(
                "이미 가입된 휴대폰 번호입니다.",
                code="PHONE_NUMBER_ALREADY_EXISTS",
                field_errors={"phone_number": "다른 휴대폰 번호를 입력해 주세요."},
            ) from exc
        raise ConflictError("사용자 생성 중 충돌이 발생했습니다.") from exc

    def create_unlinked_user(self, user: User) -> None:
        try:
            self.client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self.settings.core_table,
                            "Item": self._ddb(
                                {"pk": f"LOGINID#{user.login_id}", "sk": "USER", "user_id": user.user_id}
                            ),
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.settings.core_table,
                            "Item": self._ddb(
                                {"pk": f"PHONE#{user.phone_number}", "sk": "USER", "user_id": user.user_id}
                            ),
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.settings.core_table,
                            "Item": self._ddb(
                                {"pk": f"USER#{user.user_id}", "sk": "PROFILE", **asdict(user)}
                            ),
                        }
                    },
                ] + self._initial_consent_operations(user)
            )
        except Exception as exc:
            self._raise_identity_conflict(user, exc)

    def _initial_consent_operations(self, user: User) -> list[dict[str, Any]]:
        receipt = consent_receipt(user)
        if receipt is None:
            return []
        return [{"Put": {
            "TableName": self.settings.core_table,
            "Item": self._ddb(receipt),
            "ConditionExpression": "attribute_not_exists(pk)",
        }}]

    def get_user(self, user_id: str) -> User | None:
        result = self.core.get_item(Key={"pk": f"USER#{user_id}", "sk": "PROFILE"})
        return self._user(result.get("Item"))

    def get_user_consistent(self, user_id: str) -> User | None:
        result = self.core.get_item(
            Key={"pk": f"USER#{user_id}", "sk": "PROFILE"},
            ConsistentRead=True,
        )
        return self._user(result.get("Item"))

    def get_user_by_login_id(self, login_id: str) -> User | None:
        result = self.core.get_item(Key={"pk": f"LOGINID#{login_id}", "sk": "USER"})
        alias = result.get("Item")
        return self.get_user(alias["user_id"]) if alias else None

    def update_user_password(self, user_id: str, password_hash: str) -> User:
        try:
            result = self.core.update_item(
                Key={"pk": f"USER#{user_id}", "sk": "PROFILE"},
                UpdateExpression="SET password_hash=:hash ADD token_version :one",
                ConditionExpression="attribute_exists(pk)",
                ExpressionAttributeValues={":hash": password_hash, ":one": 1},
                ReturnValues="ALL_NEW",
            )
        except Exception as exc:
            if getattr(exc, "response", {}).get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise NotFoundError("사용자를 찾을 수 없습니다.") from exc
            raise
        return self._user(result["Attributes"])

    def record_legal_consent(
        self, user_id: str, terms_version: str, privacy_version: str, consented_at: str,
    ) -> User:
        at = iso_utc(parse_timestamp(consented_at))
        for _ in range(3):
            current = self._user(self.core.get_item(
                Key={"pk": f"USER#{user_id}", "sk": "PROFILE"}, ConsistentRead=True,
            ).get("Item"))
            if current is None:
                raise NotFoundError("사용자를 찾을 수 없습니다.")
            if (current.terms_version == terms_version and current.privacy_version == privacy_version
                    and current.terms_service_agreed is True and current.privacy_agreed is True
                    and valid_consent_time(current.consented_at) is not None):
                return current
            # Preserve monotonic actual consent times even during concurrent requests.
            previous_at = valid_consent_time(current.consented_at)
            if previous_at and parse_timestamp(at) < previous_at:
                raise ConflictError("동의 시각의 일관성을 확인할 수 없습니다.", code="LEGAL_CONSENT_CONFLICT")
            updated = replace(
                current, terms_version=terms_version, privacy_version=privacy_version,
                terms_service_agreed=True, privacy_agreed=True, consented_at=at,
            )
            receipt = consent_receipt(updated)
            values = {
                ":terms": terms_version, ":privacy": privacy_version, ":at": at,
                ":yes": True, ":old_terms": current.terms_version,
                ":old_privacy": current.privacy_version, ":old_at": current.consented_at,
                ":token": current.token_version,
                ":old_age": current.age_over_14_agreed,
                ":old_age_at": current.age_over_14_agreed_at,
            }
            token_condition = "token_version = :token"
            if current.token_version == 0:
                token_condition = "(attribute_not_exists(token_version) OR token_version = :token)"
            try:
                self.client.transact_write_items(TransactItems=[
                    {"Update": {
                        "TableName": self.settings.core_table,
                        "Key": self._ddb({"pk": f"USER#{user_id}", "sk": "PROFILE"}),
                        "UpdateExpression": "SET terms_version=:terms, privacy_version=:privacy, "
                            "consented_at=:at, terms_service_agreed=:yes, privacy_agreed=:yes",
                        "ConditionExpression": "attribute_exists(pk) AND " + token_condition
                            + " AND (attribute_not_exists(terms_version) OR terms_version=:old_terms)"
                            + " AND (attribute_not_exists(privacy_version) OR privacy_version=:old_privacy)"
                            + " AND (attribute_not_exists(consented_at) OR consented_at=:old_at)"
                            + " AND " + self._withdrawal_snapshot_clause(
                                "age_over_14_agreed", ":old_age", current.age_over_14_agreed,
                            )
                            + " AND " + self._withdrawal_snapshot_clause(
                                "age_over_14_agreed_at", ":old_age_at", current.age_over_14_agreed_at,
                            ),
                        "ExpressionAttributeValues": self._ddb(values),
                    }},
                    {"Put": {
                        "TableName": self.settings.core_table, "Item": self._ddb(receipt),
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }},
                ])
                return updated
            except Exception as exc:
                response = getattr(exc, "response", {})
                reasons = response.get("CancellationReasons", [])
                if (response.get("Error", {}).get("Code") != "TransactionCanceledException"
                        or not reasons
                        or any(reason.get("Code") not in {"None", "ConditionalCheckFailed", "TransactionConflict"}
                               for reason in reasons)):
                    raise
        raise ConflictError("다른 요청으로 동의 상태가 변경되었습니다. 다시 시도해 주세요.",
                            code="LEGAL_CONSENT_CONFLICT")

    def delete_user_account(self, user_id: str) -> None:
        from boto3.dynamodb.conditions import Attr, Key

        user = self._user(self.core.get_item(
            Key={"pk": f"USER#{user_id}", "sk": "PROFILE"}, ConsistentRead=True,
        ).get("Item"))
        if not user:
            raise NotFoundError("사용자를 찾을 수 없습니다.")
        if user.household_link_status == "linked":
            raise ConflictError(
                "가구 연결을 먼저 해제해야 합니다.",
                code="ACCOUNT_STILL_LINKED",
            )

        reference_filter = (
            (Attr("pk").begins_with("TOKEN#") & Attr("user_id").eq(user_id))
            | Attr("viewer_user_id").eq(user_id)
            | Attr("member_user_id").eq(user_id)
        )
        scan_arguments: dict[str, Any] = {
            "FilterExpression": reference_filter,
            "ProjectionExpression": "pk, sk",
            "ConsistentRead": True,
        }
        reference_keys: list[dict[str, str]] = []
        while True:
            response = self.core.scan(**scan_arguments)
            reference_keys.extend(
                {"pk": item["pk"], "sk": item["sk"]}
                for item in response.get("Items", [])
            )
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            scan_arguments["ExclusiveStartKey"] = last_key

        # Read receipts before removing the identity. A failed cleanup must leave
        # PROFILE and its login aliases alive so the user can retry withdrawal.
        consent_query: dict[str, Any] = {
            "KeyConditionExpression": Key("pk").eq(f"USER#{user_id}")
                & Key("sk").begins_with("CONSENT#"),
            "ProjectionExpression": "pk, sk", "ConsistentRead": True,
        }
        while True:
            response = self.core.query(**consent_query)
            reference_keys.extend(
                {"pk": item["pk"], "sk": item["sk"]} for item in response.get("Items", [])
            )
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            consent_query["ExclusiveStartKey"] = last_key

        # These conditions also reject a concurrent relink or new consent. Both
        # writers update PROFILE atomically; a successful final deletion therefore
        # cannot overlook a receipt written after the consistent query above.
        snapshot_fields = {
            "household_link_status": "unlinked", "household_id": user.household_id,
            "token_version": user.token_version, "terms_version": user.terms_version,
            "privacy_version": user.privacy_version, "consented_at": user.consented_at,
            "terms_service_agreed": user.terms_service_agreed, "privacy_agreed": user.privacy_agreed,
            "age_over_14_agreed": user.age_over_14_agreed,
            "age_over_14_agreed_at": user.age_over_14_agreed_at,
            "reference_version": user.reference_version,
            "login_id": user.login_id, "phone_number": user.phone_number,
        }
        conditions = ["attribute_exists(pk)"]
        snapshot_values = {}
        for field, value in snapshot_fields.items():
            placeholder = f":snapshot_{field}"
            snapshot_values[placeholder] = value
            if (value is None
                    or (field in {"token_version", "reference_version"} and value == 0)
                    or (field in {"terms_service_agreed", "privacy_agreed"} and value is False)
                    or field == "household_link_status"):
                conditions.append(f"(attribute_not_exists({field}) OR {field}={placeholder})")
            else:
                conditions.append(f"{field}={placeholder}")
        guard = {
            "TableName": self.settings.core_table,
            "Key": self._ddb({"pk": f"USER#{user_id}", "sk": "PROFILE"}),
            "ConditionExpression": " AND ".join(conditions),
            "ExpressionAttributeValues": self._ddb(snapshot_values),
        }
        identity_keys = {
            (f"LOGINID#{user.login_id}", "USER"), (f"PHONE#{user.phone_number}", "USER"),
            (f"USER#{user_id}", "PROFILE"),
        }
        all_cleanup = {(key["pk"], key["sk"]) for key in reference_keys} - identity_keys
        receipt_keys = sorted(key for key in all_cleanup
                              if key[0] == f"USER#{user_id}" and key[1].startswith("CONSENT#"))
        cleanup_keys = sorted(all_cleanup - set(receipt_keys))
        if len(receipt_keys) > 97:
            # Keep every receipt until the final atomic identity deletion.
            # Very large document-version histories need an operator workflow.
            raise ConflictError(
                "관련 기록이 많아 자동 탈퇴를 완료할 수 없습니다. 관리자에게 문의해 주세요.",
                code="ACCOUNT_DELETION_REVIEW_REQUIRED",
            )

        def deletes(keys):
            return [{"Delete": {
                "TableName": self.settings.core_table,
                "Key": self._ddb({"pk": pk, "sk": sk}),
            }} for pk, sk in keys]

        try:
            # Token/display-name cleanup may be resumed if a later step fails;
            # PROFILE, aliases and all consent receipts remain intact meanwhile.
            # New reference writes update the snapshot version and force a retry.
            while len(cleanup_keys) + len(receipt_keys) > 97:
                batch = cleanup_keys[:99]
                self.client.transact_write_items(TransactItems=[
                    {"ConditionCheck": guard}, *deletes(batch),
                ])
                cleanup_keys = cleanup_keys[99:]
            self.client.transact_write_items(
                TransactItems=[
                    {
                        "Delete": {
                            "TableName": self.settings.core_table,
                            "Key": self._ddb(
                                {"pk": f"LOGINID#{user.login_id}", "sk": "USER"}
                            ),
                            "ConditionExpression": "user_id=:user",
                            "ExpressionAttributeValues": self._ddb({":user": user_id}),
                        }
                    },
                    {
                        "Delete": {
                            "TableName": self.settings.core_table,
                            "Key": self._ddb(
                                {"pk": f"PHONE#{user.phone_number}", "sk": "USER"}
                            ),
                            "ConditionExpression": "user_id=:user",
                            "ExpressionAttributeValues": self._ddb({":user": user_id}),
                        }
                    },
                    {
                        "Delete": {
                            **guard,
                        }
                    },
                    *deletes(cleanup_keys + receipt_keys),
                ],
            )
        except Exception as exc:
            raise ConflictError(
                "계정 상태가 변경되어 삭제를 완료하지 못했습니다.",
                code="ACCOUNT_DELETION_CONFLICT",
            ) from exc

    @staticmethod
    def _raise_withdrawal_review_required() -> None:
        raise ConflictError(
            "관련 기록을 자동으로 검증할 수 없어 관리자 확인이 필요합니다.",
            code="ACCOUNT_DELETION_REVIEW_REQUIRED",
        )

    @staticmethod
    def _withdrawal_snapshot_clause(
        field: str,
        placeholder: str,
        value: Any,
        *,
        legacy_zero: bool = False,
        legacy_false: bool = False,
        legacy_unlinked: bool = False,
    ) -> str:
        if (
            value is None
            or (legacy_zero and value == 0)
            or (legacy_false and value is False)
            or (legacy_unlinked and value == "unlinked")
        ):
            return f"(attribute_not_exists({field}) OR {field}={placeholder})"
        return f"{field}={placeholder}"

    def _withdrawal_reference_keys(
        self, user_id: str
    ) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
        from boto3.dynamodb.conditions import Attr, Key

        reference_filter = (
            (Attr("pk").begins_with("TOKEN#") & Attr("user_id").eq(user_id))
            | Attr("viewer_user_id").eq(user_id)
            | Attr("member_user_id").eq(user_id)
            | Attr("sk").eq(f"MEMBER#{user_id}")
        )
        scan_arguments: dict[str, Any] = {
            "FilterExpression": reference_filter,
            "ProjectionExpression": "pk, sk",
            "ConsistentRead": True,
        }
        keys: set[tuple[str, str]] = set()
        membership_keys: set[tuple[str, str]] = set()
        while True:
            response = self.core.scan(**scan_arguments)
            for item in response.get("Items", []):
                pk, sk = item.get("pk"), item.get("sk")
                if not isinstance(pk, str) or not pk or not isinstance(sk, str) or not sk:
                    self._raise_withdrawal_review_required()
                if pk.startswith("HOUSE#") and sk == f"MEMBER#{user_id}":
                    membership_keys.add((pk, sk))
                elif not (
                    pk.startswith("TOKEN#")
                    or (pk.startswith("HOUSE#") and sk.startswith("ALIAS#"))
                ):
                    self._raise_withdrawal_review_required()
                else:
                    keys.add((pk, sk))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            scan_arguments["ExclusiveStartKey"] = last_key

        consent_query: dict[str, Any] = {
            "KeyConditionExpression": Key("pk").eq(f"USER#{user_id}")
            & Key("sk").begins_with("CONSENT#"),
            "ProjectionExpression": "pk, sk",
            "ConsistentRead": True,
        }
        while True:
            response = self.core.query(**consent_query)
            for item in response.get("Items", []):
                pk, sk = item.get("pk"), item.get("sk")
                if (
                    pk != f"USER#{user_id}"
                    or not isinstance(sk, str)
                    or not sk.startswith("CONSENT#")
                ):
                    self._raise_withdrawal_review_required()
                keys.add((pk, sk))
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            consent_query["ExclusiveStartKey"] = last_key
        return keys, membership_keys

    def _consistent_household_members(
        self, household_id: str
    ) -> list[tuple[dict[str, Any], User]]:
        from boto3.dynamodb.conditions import Key

        query_arguments: dict[str, Any] = {
            "KeyConditionExpression": Key("pk").eq(f"HOUSE#{household_id}")
            & Key("sk").begins_with("MEMBER#"),
            "ProjectionExpression": "pk, sk, user_id, linked_at",
            "ConsistentRead": True,
        }
        membership_items: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        while True:
            response = self.core.query(**query_arguments)
            for raw_item in response.get("Items", []):
                item = _plain(raw_item)
                pk, sk, member_id = item.get("pk"), item.get("sk"), item.get("user_id")
                if (
                    pk != f"HOUSE#{household_id}"
                    or not isinstance(member_id, str)
                    or not member_id
                    or sk != f"MEMBER#{member_id}"
                    or (pk, sk) in seen
                    or not isinstance(item.get("linked_at"), str)
                ):
                    self._raise_withdrawal_review_required()
                seen.add((pk, sk))
                membership_items.append(item)
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            query_arguments["ExclusiveStartKey"] = last_key

        members: list[tuple[dict[str, Any], User]] = []
        for item in sorted(membership_items, key=lambda value: value["user_id"]):
            member = self.get_user_consistent(item["user_id"])
            if (
                member is None
                or member.user_id != item["user_id"]
                or member.household_id != household_id
                or member.household_link_status != "linked"
                or member.role not in {"owner", "member"}
                or member.linked_at != item["linked_at"]
                or (member.role == "owner")
                != (member.account_type == "household_owner")
            ):
                self._raise_withdrawal_review_required()
            members.append((item, member))
        return members

    def _registration_write_operation(self, household_id: str) -> dict[str, Any]:
        """Fence new child records against a concurrent household destruction."""
        return {"Update": {
            "TableName": self.settings.core_table,
            "Key": self._ddb({"pk": f"HOUSE#{household_id}", "sk": "META"}),
            "UpdateExpression": "ADD registration_version :one",
            "ConditionExpression": "attribute_exists(pk) AND #status=:active",
            "ExpressionAttributeNames": {"#status": "status"},
            "ExpressionAttributeValues": self._ddb({":one": 1, ":active": "active"}),
        }}

    def _owned_households(self, user: User) -> list[Household]:
        """Find legacy inactive houses too; never silently delete another owner."""
        from boto3.dynamodb.conditions import Attr

        arguments: dict[str, Any] = {
            "FilterExpression": (
                Attr("pk").begins_with("HOUSE#") & Attr("sk").eq("META")
                & Attr("owner_user_id").eq(user.user_id)
            ),
            "ConsistentRead": True,
        }
        houses: list[Household] = []
        seen: set[str] = set()
        while True:
            response = self.core.scan(**arguments)
            for item in response.get("Items", []):
                try:
                    house = self._household(item)
                except (TypeError, ValueError, KeyError):
                    self._raise_withdrawal_review_required()
                if (
                    house is None or item.get("pk") != f"HOUSE#{house.household_id}"
                    or item.get("sk") != "META" or house.owner_user_id != user.user_id
                    or user.account_type != "household_owner"
                    or house.household_id in seen
                    or house.status not in {"active", "inactive"}
                    or (house.status == "active" and (
                        user.household_id != house.household_id or user.role != "owner"
                        or user.household_link_status != "linked"
                    ))
                ):
                    self._raise_withdrawal_review_required()
                seen.add(house.household_id)
                houses.append(house)
                if len(houses) > 100:
                    self._raise_withdrawal_review_required()
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                return sorted(houses, key=lambda value: value.household_id)
            arguments["ExclusiveStartKey"] = last_key

    def _household_destruction_operations(
        self, house: Household, membership_rows: list[tuple[dict[str, Any], User]],
    ) -> list[dict[str, Any]]:
        """Discover a bounded, conditioned plan. No writes and no alert deletion."""
        from boto3.dynamodb.conditions import Attr, Key

        for version in (house.membership_version, house.registration_version):
            if not isinstance(version, int) or isinstance(version, bool) or version < 0:
                self._raise_withdrawal_review_required()
        expected_members = {f"MEMBER#{member.user_id}" for _, member in membership_rows}
        if house.status == "inactive" and expected_members:
            self._raise_withdrawal_review_required()
        rows: dict[tuple[str, str], dict[str, Any]] = {}
        arguments: dict[str, Any] = {
            "KeyConditionExpression": Key("pk").eq(f"HOUSE#{house.household_id}"),
            "ConsistentRead": True,
        }
        observed_members: set[str] = set()
        metadata = None
        while True:
            response = self.core.query(**arguments)
            for item in response.get("Items", []):
                pk, sk = item.get("pk"), item.get("sk")
                if pk != f"HOUSE#{house.household_id}" or not isinstance(sk, str):
                    self._raise_withdrawal_review_required()
                if sk == "META":
                    metadata = item
                elif sk.startswith("MEMBER#"):
                    if sk in observed_members:
                        self._raise_withdrawal_review_required()
                    observed_members.add(sk)
                    continue  # Added with PROFILE unlink operations by the caller.
                elif not sk.startswith(("DEVICE#", "ALIAS#", "CONTACT#")):
                    self._raise_withdrawal_review_required()
                if (pk, sk) in rows:
                    self._raise_withdrawal_review_required()
                rows[(pk, sk)] = item
            if len(rows) + len(observed_members) > 100:
                self._raise_withdrawal_review_required()
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            arguments["ExclusiveStartKey"] = last_key
        if (
            metadata is None or observed_members != expected_members
            or metadata.get("owner_user_id") != house.owner_user_id
            or metadata.get("status") != house.status
            or metadata.get("household_id") != house.household_id
            or metadata.get("membership_version", 0) != house.membership_version
            or metadata.get("registration_version", 0) != house.registration_version
        ):
            self._raise_withdrawal_review_required()

        aliases: dict[tuple[str, str], dict[str, Any]] = {}
        scan_arguments: dict[str, Any] = {
            "FilterExpression": (
                Attr("household_id").eq(house.household_id)
                & (Attr("pk").begins_with("DEVICECRED#") | Attr("pk").begins_with("INVITE#"))
            ),
            "ConsistentRead": True,
        }
        while True:
            response = self.core.scan(**scan_arguments)
            for item in response.get("Items", []):
                pk, sk = item.get("pk"), item.get("sk")
                if (
                    not isinstance(pk, str) or item.get("household_id") != house.household_id
                    or not ((pk.startswith("DEVICECRED#") and sk == "DEVICE")
                            or (pk.startswith("INVITE#") and sk == "INVITE"))
                    or (pk, sk) in aliases
                ):
                    self._raise_withdrawal_review_required()
                aliases[(pk, sk)] = item
            if len(rows) + len(aliases) + len(observed_members) > 100:
                self._raise_withdrawal_review_required()
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            scan_arguments["ExclusiveStartKey"] = last_key
        if house.invite_hash:
            invite_key = (f"INVITE#{house.invite_hash}", "INVITE")
            invite = aliases.get(invite_key)
            if invite is None:
                # An expired alias may already be gone. A rebound alias belongs
                # to another household and must not be deleted or ignored.
                invite = self.core.get_item(
                    Key={"pk": invite_key[0], "sk": invite_key[1]},
                    ConsistentRead=True,
                ).get("Item")
            if invite is not None and (
                invite.get("pk") != invite_key[0] or invite.get("sk") != invite_key[1]
                or invite.get("household_id") != house.household_id
            ):
                self._raise_withdrawal_review_required()
        for item in rows.values():
            if not item["sk"].startswith("DEVICE#"):
                continue
            if item.get("household_id") != house.household_id or item["sk"] != f"DEVICE#{item.get('device_id')}":
                self._raise_withdrawal_review_required()
            credential_hash = item.get("credential_hash")
            if credential_hash:
                alias = aliases.get((f"DEVICECRED#{credential_hash}", "DEVICE"))
                if alias is not None and alias.get("device_id") != item["device_id"]:
                    self._raise_withdrawal_review_required()
                if alias is None:
                    # Missing aliases may already have been cleaned; a different
                    # household's credential must never be removed as a fallback.
                    existing = self.core.get_item(
                        Key={"pk": f"DEVICECRED#{credential_hash}", "sk": "DEVICE"},
                        ConsistentRead=True,
                    ).get("Item")
                    if existing is not None:
                        self._raise_withdrawal_review_required()

        operations: list[dict[str, Any]] = []
        for (pk, sk), item in sorted({**rows, **aliases}.items()):
            # Compare discovered fields, not volatile heartbeat counters. Every
            # child-creating writer fences META; device rotation also fences it.
            values: dict[str, Any] = {}
            names: dict[str, str] = {}
            conditions = ["attribute_exists(pk)"]
            selected_fields = (
                [key for key in item if key not in {"pk", "sk"}]
                if sk == "META" else
                [key for key in ("household_id", "device_id", "credential_hash",
                                 "viewer_user_id", "member_user_id") if key in item]
            )
            for index, field in enumerate(selected_fields):
                name, value = f"#destroy{index}", f":destroy{index}"
                names[name] = field
                values[value] = item[field]
                conditions.append(f"{name}={value}")
            if sk == "META":
                for field in ("membership_version", "registration_version"):
                    if field not in item:
                        conditions.append(f"attribute_not_exists({field})")
            delete: dict[str, Any] = {
                "TableName": self.settings.core_table,
                "Key": self._ddb({"pk": pk, "sk": sk}),
                "ConditionExpression": " AND ".join(conditions),
            }
            if names:
                delete["ExpressionAttributeNames"] = names
                delete["ExpressionAttributeValues"] = self._ddb(values)
            operations.append({"Delete": delete})
        return operations

    def withdraw_user_account(
        self,
        user_id: str,
        now: datetime,
        *,
        expected_password_hash: str,
        _unlink_only: bool = False,
    ) -> dict[str, Any]:
        """Delete an identity and any membership effects in one DynamoDB transaction."""
        user = self.get_user_consistent(user_id)
        if user is None:
            raise NotFoundError("사용자를 찾을 수 없습니다.")
        if user.user_id != user_id:
            self._raise_withdrawal_review_required()
        if user.password_hash != expected_password_hash:
            raise ConflictError(
                "계정 상태가 변경되어 삭제를 완료하지 못했습니다.",
                code="ACCOUNT_DELETION_CONFLICT",
            )

        previous_household_id = user.household_id
        household_status: str | None = None
        household: Household | None = None
        membership_rows: list[tuple[dict[str, Any], User]] = []
        if user.household_link_status == "linked":
            if not previous_household_id or user.role not in {"owner", "member"}:
                self._raise_withdrawal_review_required()
            household = self.get_household_consistent(previous_household_id)
            if (
                household is None
                or household.status != "active"
                or not isinstance(household.membership_version, int)
                or isinstance(household.membership_version, bool)
                or household.membership_version < 0
            ):
                self._raise_withdrawal_review_required()
            membership_rows = self._consistent_household_members(previous_household_id)
            member_by_id = {member.user_id: member for _, member in membership_rows}
            if (
                user_id not in member_by_id
                or household.owner_user_id not in member_by_id
                or member_by_id[household.owner_user_id].role != "owner"
                or any(
                    (member.user_id == household.owner_user_id) != (member.role == "owner")
                    for _, member in membership_rows
                )
                or (user.role == "owner") != (household.owner_user_id == user_id)
            ):
                self._raise_withdrawal_review_required()
            household_status = "inactive" if user.role == "owner" else "active"
        elif user.household_link_status != "unlinked":
            self._raise_withdrawal_review_required()

        # All discovery is complete before constructing or issuing a write.
        reference_keys, membership_reference_keys = self._withdrawal_reference_keys(user_id)
        if household is None:
            if (
                user.household_id is not None
                or user.role is not None
                or user.linked_at is not None
                or membership_reference_keys
            ):
                self._raise_withdrawal_review_required()
        elif membership_reference_keys != {
            (f"HOUSE#{previous_household_id}", f"MEMBER#{user_id}")
        }:
            self._raise_withdrawal_review_required()
        if _unlink_only and (household is None or user.role != "owner"):
            self._raise_withdrawal_review_required()
        owned_houses = self._owned_households(user)
        if household is not None and user.role == "owner" and not any(
            value.household_id == previous_household_id for value in owned_houses
        ):
            self._raise_withdrawal_review_required()
        destruction_operations: list[dict[str, Any]] = []
        for owned in owned_houses:
            current = owned.household_id == previous_household_id and user.role == "owner"
            # Use the metadata version observed BEFORE the membership snapshot.
            plan_house = household if current else owned
            old_members = membership_rows if current else self._consistent_household_members(owned.household_id)
            destruction_operations.extend(self._household_destruction_operations(plan_house, old_members))
        destruction_keys = {
            (self._decode_operation_key(value)["pk"], self._decode_operation_key(value)["sk"])
            for value in destruction_operations
        }
        if previous_household_id is None and owned_houses:
            previous_household_id = owned_houses[0].household_id
            household_status = "inactive"
        operations: list[dict[str, Any]] = []
        operation_keys: set[tuple[str, str]] = set()

        def add_operation(pk: str, sk: str, operation: dict[str, Any]) -> None:
            identity = (pk, sk)
            if identity in operation_keys:
                self._raise_withdrawal_review_required()
            operation_keys.add(identity)
            operations.append(operation)

        identity_values = self._ddb({":deleting_user": user_id})
        for pk in (() if _unlink_only else (f"LOGINID#{user.login_id}", f"PHONE#{user.phone_number}")):
            add_operation(pk, "USER", {"Delete": {
                "TableName": self.settings.core_table,
                "Key": self._ddb({"pk": pk, "sk": "USER"}),
                "ConditionExpression": "user_id=:deleting_user",
                "ExpressionAttributeValues": identity_values,
            }})

        profile_fields = {
            "user_id": user_id,
            "password_hash": expected_password_hash,
            "token_version": user.token_version,
            "reference_version": user.reference_version,
            "household_link_status": user.household_link_status,
            "household_id": user.household_id,
            "#role": user.role,
            "linked_at": user.linked_at,
            "terms_version": user.terms_version,
            "privacy_version": user.privacy_version,
            "consented_at": user.consented_at,
            "terms_service_agreed": user.terms_service_agreed,
            "privacy_agreed": user.privacy_agreed,
            "age_over_14_agreed": user.age_over_14_agreed,
            "age_over_14_agreed_at": user.age_over_14_agreed_at,
            "login_id": user.login_id,
            "phone_number": user.phone_number,
            "account_type": user.account_type,
        }
        profile_values: dict[str, Any] = {}
        profile_conditions = ["attribute_exists(pk)"]
        for field, value in profile_fields.items():
            field_name = "role" if field == "#role" else field
            placeholder = f":profile_{field_name}"
            profile_values[placeholder] = value
            profile_conditions.append(self._withdrawal_snapshot_clause(
                field,
                placeholder,
                value,
                legacy_zero=field_name in {"token_version", "reference_version"},
                legacy_false=field_name in {"terms_service_agreed", "privacy_agreed"},
                legacy_unlinked=field_name == "household_link_status",
            ))
        profile_operation = {
            "TableName": self.settings.core_table,
            "Key": self._ddb({"pk": f"USER#{user_id}", "sk": "PROFILE"}),
            "ConditionExpression": " AND ".join(profile_conditions),
            "ExpressionAttributeNames": {"#role": "role"},
            "ExpressionAttributeValues": self._ddb(profile_values),
        }
        if _unlink_only:
            profile_values[":unlink_status"] = "unlinked"
            profile_operation["UpdateExpression"] = (
                "SET household_link_status=:unlink_status REMOVE household_id, #role, linked_at"
            )
            profile_operation["ExpressionAttributeValues"] = self._ddb(profile_values)
        add_operation(f"USER#{user_id}", "PROFILE", {
            "Update" if _unlink_only else "Delete": profile_operation,
        })

        for pk, sk in sorted(set() if _unlink_only else reference_keys):
            if (pk, sk) in destruction_keys:
                continue
            delete: dict[str, Any] = {
                "TableName": self.settings.core_table,
                "Key": self._ddb({"pk": pk, "sk": sk}),
            }
            if pk.startswith("TOKEN#"):
                delete.update(
                    ConditionExpression="user_id=:deleting_user",
                    ExpressionAttributeValues=identity_values,
                )
            elif sk.startswith("ALIAS#"):
                delete.update(
                    ConditionExpression=(
                        "(viewer_user_id=:deleting_user OR member_user_id=:deleting_user)"
                    ),
                    ExpressionAttributeValues=identity_values,
                )
            add_operation(pk, sk, {"Delete": delete})

        if household is not None and previous_household_id is not None:
            house_values: dict[str, Any] = {
                ":active": "active",
                ":owner": household.owner_user_id,
                ":current_membership_version": household.membership_version,
                ":next_membership_version": household.membership_version + 1,
            }
            house_conditions = [
                "#status=:active",
                "owner_user_id=:owner",
                self._withdrawal_snapshot_clause(
                    "membership_version",
                    ":current_membership_version",
                    household.membership_version,
                    legacy_zero=True,
                ),
            ]
            if user.role != "owner":
                add_operation(f"HOUSE#{previous_household_id}", "META", {"Update": {
                    "TableName": self.settings.core_table,
                    "Key": self._ddb({"pk": f"HOUSE#{previous_household_id}", "sk": "META"}),
                    "UpdateExpression": "SET membership_version=:next_membership_version",
                    "ConditionExpression": " AND ".join(house_conditions),
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": self._ddb(house_values),
                }})

            for membership_item, member in membership_rows:
                if user.role != "owner" and member.user_id != user_id:
                    continue
                member_values = {
                    ":member_id": member.user_id,
                    ":member_linked_at": membership_item["linked_at"],
                }
                add_operation(
                    f"HOUSE#{previous_household_id}",
                    f"MEMBER#{member.user_id}",
                    {"Delete": {
                        "TableName": self.settings.core_table,
                        "Key": self._ddb({
                            "pk": f"HOUSE#{previous_household_id}",
                            "sk": f"MEMBER#{member.user_id}",
                        }),
                        "ConditionExpression": "user_id=:member_id AND linked_at=:member_linked_at",
                        "ExpressionAttributeValues": self._ddb(member_values),
                    }},
                )
                if user.role == "owner" and member.user_id != user_id:
                    linked_values = self._ddb({
                        ":household_id": previous_household_id,
                        ":linked": "linked",
                        ":member_role": "member",
                        ":member_linked_at": member.linked_at,
                        ":unlinked": "unlinked",
                        ":member_reference_version": member.reference_version,
                    })
                    add_operation(f"USER#{member.user_id}", "PROFILE", {"Update": {
                        "TableName": self.settings.core_table,
                        "Key": self._ddb({
                            "pk": f"USER#{member.user_id}", "sk": "PROFILE",
                        }),
                        "UpdateExpression": (
                            "SET household_link_status=:unlinked "
                            "REMOVE household_id, #role, linked_at"
                        ),
                        "ConditionExpression": (
                            "attribute_exists(pk) AND household_id=:household_id AND "
                            "household_link_status=:linked AND #role=:member_role AND "
                            "linked_at=:member_linked_at AND "
                            + self._withdrawal_snapshot_clause(
                                "reference_version", ":member_reference_version",
                                member.reference_version, legacy_zero=True,
                            )
                        ),
                        "ExpressionAttributeNames": {"#role": "role"},
                        "ExpressionAttributeValues": linked_values,
                    }})

        for operation in destruction_operations:
            key = self._decode_operation_key(operation)
            add_operation(key["pk"], key["sk"], operation)

        if len(operations) > 100:
            self._raise_withdrawal_review_required()
        try:
            self.client.transact_write_items(TransactItems=operations)
        except Exception as exc:
            raise ConflictError(
                "계정 상태가 변경되어 삭제를 완료하지 못했습니다.",
                code="ACCOUNT_DELETION_CONFLICT",
            ) from exc
        return {
            "previous_household_id": previous_household_id,
            "household_status": household_status,
        }

    @staticmethod
    def _decode_operation_key(operation: dict[str, Any]) -> dict[str, str]:
        from boto3.dynamodb.types import TypeDeserializer

        deserializer = TypeDeserializer()
        return {
            key: deserializer.deserialize(value)
            for key, value in next(iter(operation.values()))["Key"].items()
        }

    def get_household(self, household_id: str) -> Household | None:
        item = self.core.get_item(Key={"pk": f"HOUSE#{household_id}", "sk": "META"}).get("Item")
        return self._household(item)

    def get_household_consistent(self, household_id: str) -> Household | None:
        item = self.core.get_item(
            Key={"pk": f"HOUSE#{household_id}", "sk": "META"},
            ConsistentRead=True,
        ).get("Item")
        return self._household(item)

    def update_household_emergency_address(
        self, household_id: str, address: EmergencyAddress
    ) -> EmergencyAddress:
        try:
            self.core.update_item(
                Key={"pk": f"HOUSE#{household_id}", "sk": "META"},
                UpdateExpression="SET emergency_address=:address",
                ConditionExpression="attribute_exists(pk) AND #status=:active",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":address": asdict(address), ":active": "active"},
            )
        except Exception as exc:
            raise NotFoundError("가구를 찾을 수 없습니다.") from exc
        return address

    def list_members(self, household_id: str) -> list[User]:
        from boto3.dynamodb.conditions import Key

        response = self.core.query(
            KeyConditionExpression=Key("pk").eq(f"HOUSE#{household_id}") & Key("sk").begins_with("MEMBER#")
        )
        users = [self.get_user(item["user_id"]) for item in response.get("Items", [])]
        return [user for user in users if user]

    def get_display_name(
        self, household_id: str, viewer_user_id: str, member_user_id: str
    ) -> str | None:
        item = self.core.get_item(
            Key={
                "pk": f"HOUSE#{household_id}",
                "sk": f"ALIAS#{viewer_user_id}#{member_user_id}",
            }
        ).get("Item")
        return item.get("display_name") if item else None

    def set_display_name(
        self,
        household_id: str,
        viewer_user_id: str,
        member_user_id: str,
        display_name: str,
    ) -> None:
        values = self._ddb({":household": household_id, ":linked": "linked", ":one": 1})
        operations = [self._registration_write_operation(household_id)]
        for user_id in dict.fromkeys((viewer_user_id, member_user_id)):
            operations.append({"Update": {
                "TableName": self.settings.core_table,
                "Key": self._ddb({"pk": f"USER#{user_id}", "sk": "PROFILE"}),
                "UpdateExpression": "ADD reference_version :one",
                "ConditionExpression": (
                    "attribute_exists(pk) AND household_id=:household "
                    "AND household_link_status=:linked"
                ),
                "ExpressionAttributeValues": values,
            }})
        operations.append({"Put": {
            "TableName": self.settings.core_table,
            "Item": self._ddb({
                "pk": f"HOUSE#{household_id}",
                "sk": f"ALIAS#{viewer_user_id}#{member_user_id}",
                "viewer_user_id": viewer_user_id,
                "member_user_id": member_user_id,
                "display_name": display_name,
                "updated_at": iso_utc(),
            }),
        }})
        try:
            self.client.transact_write_items(TransactItems=operations)
        except Exception as exc:
            response = getattr(exc, "response", {})
            if response.get("Error", {}).get("Code") == "TransactionCanceledException":
                raise NotFoundError("가족 구성원을 찾을 수 없습니다.") from exc
            raise

    def delete_display_name(
        self, household_id: str, viewer_user_id: str, member_user_id: str
    ) -> None:
        member = self.get_user(member_user_id)
        if not member or member.household_id != household_id:
            raise NotFoundError("가족 구성원을 찾을 수 없습니다.")
        self.core.delete_item(
            Key={
                "pk": f"HOUSE#{household_id}",
                "sk": f"ALIAS#{viewer_user_id}#{member_user_id}",
            }
        )

    def member_count(self, household_id: str) -> int:
        from boto3.dynamodb.conditions import Key

        response = self.core.query(
            KeyConditionExpression=Key("pk").eq(f"HOUSE#{household_id}")
            & Key("sk").begins_with("MEMBER#"),
            Select="COUNT",
        )
        return int(response.get("Count", 0))

    def link_member(self, user_id: str, invite_hash: str, now: datetime) -> User:
        user = self.get_user(user_id)
        if not user:
            raise NotFoundError("사용자를 찾을 수 없습니다.")
        if user.account_type != "family_member":
            raise ConflictError("가족 계정만 가구에 연동할 수 있습니다.", code="ACCOUNT_TYPE_NOT_LINKABLE")
        if user.household_link_status == "linked":
            raise ConflictError("이미 가구에 연동되어 있습니다.", code="HOUSEHOLD_ALREADY_LINKED")
        invite = self.get_invite(invite_hash)
        if not invite:
            raise InvalidInviteError("초대 코드를 찾을 수 없습니다.", code="INVITE_CODE_NOT_FOUND")
        if parse_timestamp(invite["expires_at"]) <= now:
            raise InvalidInviteError("초대 코드가 만료되었습니다.", code="INVITE_CODE_EXPIRED")
        household_id = invite["household_id"]
        linked_at = iso_utc(now)
        try:
            self.client.transact_write_items(
                TransactItems=[
                    {
                        "ConditionCheck": {
                            "TableName": self.settings.core_table,
                            "Key": self._ddb({"pk": f"INVITE#{invite_hash}", "sk": "INVITE"}),
                            "ConditionExpression": "attribute_exists(pk) AND expires_at > :now",
                            "ExpressionAttributeValues": self._ddb({":now": linked_at}),
                        }
                    },
                    {
                        "Update": {
                            "TableName": self.settings.core_table,
                            "Key": self._ddb({"pk": f"HOUSE#{household_id}", "sk": "META"}),
                            "UpdateExpression": "ADD membership_version :one",
                            "ConditionExpression": "#status=:active AND invite_hash=:hash",
                            "ExpressionAttributeNames": {"#status": "status"},
                            "ExpressionAttributeValues": self._ddb(
                                {":active": "active", ":hash": invite_hash, ":one": 1}
                            ),
                        }
                    },
                    {
                        "Update": {
                            "TableName": self.settings.core_table,
                            "Key": self._ddb({"pk": f"USER#{user_id}", "sk": "PROFILE"}),
                            "UpdateExpression": (
                                "SET household_id=:house, #role=:role, "
                                "household_link_status=:linked, linked_at=:at"
                            ),
                            "ConditionExpression": (
                                "household_link_status=:unlinked AND account_type=:family"
                            ),
                            "ExpressionAttributeNames": {"#role": "role"},
                            "ExpressionAttributeValues": self._ddb(
                                {
                                    ":house": household_id,
                                    ":role": "member",
                                    ":linked": "linked",
                                    ":unlinked": "unlinked",
                                    ":family": "family_member",
                                    ":at": linked_at,
                                }
                            ),
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.settings.core_table,
                            "Item": self._ddb(
                                {
                                    "pk": f"HOUSE#{household_id}",
                                    "sk": f"MEMBER#{user_id}",
                                    "user_id": user_id,
                                    "linked_at": linked_at,
                                    "alarms_last_seen_at": linked_at,
                                }
                            ),
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                ]
            )
        except Exception as exc:
            raise ConflictError("가구 연동 상태가 변경되어 다시 시도해야 합니다.", code="HOUSEHOLD_LINK_CONFLICT") from exc
        return self.get_user(user_id)

    def get_or_initialize_alarm_last_seen(
        self,
        household_id: str,
        user_id: str,
        baseline_at: str,
    ) -> str:
        key = {
            "pk": f"HOUSE#{household_id}",
            "sk": f"MEMBER#{user_id}",
        }
        try:
            result = self.core.update_item(
                Key=key,
                UpdateExpression=(
                    "SET alarms_last_seen_at="
                    "if_not_exists(alarms_last_seen_at, :baseline)"
                ),
                ConditionExpression="attribute_exists(pk)",
                ExpressionAttributeValues={":baseline": baseline_at},
                ReturnValues="ALL_NEW",
            )
        except self.client.exceptions.ConditionalCheckFailedException as exc:
            raise NotFoundError("가구 구성원을 찾을 수 없습니다.") from exc
        return result["Attributes"]["alarms_last_seen_at"]

    def mark_alarms_seen(
        self,
        household_id: str,
        user_id: str,
        seen_at: str,
    ) -> str:
        key = {
            "pk": f"HOUSE#{household_id}",
            "sk": f"MEMBER#{user_id}",
        }
        try:
            result = self.core.update_item(
                Key=key,
                UpdateExpression="SET alarms_last_seen_at=:seen",
                ConditionExpression=(
                    "attribute_exists(pk) AND "
                    "(attribute_not_exists(alarms_last_seen_at) "
                    "OR alarms_last_seen_at <= :seen)"
                ),
                ExpressionAttributeValues={":seen": seen_at},
                ReturnValues="ALL_NEW",
            )
            return result["Attributes"]["alarms_last_seen_at"]
        except self.client.exceptions.ConditionalCheckFailedException as exc:
            current = self.core.get_item(Key=key).get("Item")
            if not current:
                raise NotFoundError("가구 구성원을 찾을 수 없습니다.") from exc
            value = current.get("alarms_last_seen_at")
            if not value:
                raise StoreError("알림 확인 시각을 갱신하지 못했습니다.") from exc
            return str(value)

    def rotate_invite(
        self,
        household_id: str,
        invite_hash: str,
        invite_nonce: str,
        expires_at: str,
    ) -> Household:
        household = self.get_household(household_id)
        if not household or household.status != "active":
            raise NotFoundError("가구를 찾을 수 없습니다.")
        operations: list[dict[str, Any]] = [
            {
                "Update": {
                    "TableName": self.settings.core_table,
                    "Key": self._ddb({"pk": f"HOUSE#{household_id}", "sk": "META"}),
                    "UpdateExpression": (
                        "SET invite_hash=:hash, invite_nonce=:nonce, invite_expires_at=:expires "
                        "ADD registration_version :one"
                    ),
                    "ConditionExpression": (
                        "#status=:active AND "
                        + self._withdrawal_snapshot_clause(
                            "invite_hash", ":previous_hash", household.invite_hash,
                        )
                    ),
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": self._ddb(
                        {
                            ":hash": invite_hash,
                            ":nonce": invite_nonce,
                            ":expires": expires_at,
                            ":active": "active",
                            ":one": 1,
                            ":previous_hash": household.invite_hash,
                        }
                    ),
                }
            },
            {
                "Put": {
                    "TableName": self.settings.core_table,
                    "Item": self._ddb(
                        {
                            "pk": f"INVITE#{invite_hash}",
                            "sk": "INVITE",
                            "household_id": household_id,
                            "expires_at": expires_at,
                            "expires_at_epoch": int(parse_timestamp(expires_at).timestamp()),
                        }
                    ),
                    "ConditionExpression": "attribute_not_exists(pk)",
                }
            },
        ]
        if household.invite_hash:
            operations.append(
                {
                    "Delete": {
                        "TableName": self.settings.core_table,
                        "Key": self._ddb(
                            {"pk": f"INVITE#{household.invite_hash}", "sk": "INVITE"}
                        ),
                        "ConditionExpression": "attribute_not_exists(pk) OR household_id=:house",
                        "ExpressionAttributeValues": self._ddb({":house": household_id}),
                    }
                }
            )
        self.client.transact_write_items(TransactItems=operations)
        return self.get_household(household_id)

    def get_invite(self, invite_hash: str) -> dict[str, Any] | None:
        return self.core.get_item(Key={"pk": f"INVITE#{invite_hash}", "sk": "INVITE"}).get("Item")

    def unlink_user(self, user_id: str, now: datetime) -> dict[str, Any]:
        user = self.get_user_consistent(user_id)
        if not user or user.household_link_status != "linked" or not user.household_id:
            raise ConflictError("가구에 연동되어 있지 않습니다.", code="HOUSEHOLD_LINK_REQUIRED")
        household_id = user.household_id
        household = self.get_household_consistent(household_id)
        if not household or household.status != "active":
            raise ConflictError("활성 가구를 찾을 수 없습니다.", code="HOUSEHOLD_INACTIVE")
        if (
            not isinstance(household.membership_version, int)
            or isinstance(household.membership_version, bool)
            or household.membership_version < 0
        ):
            self._raise_withdrawal_review_required()
        if user.role != "owner":
            self.client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self.settings.core_table,
                            "Key": self._ddb({"pk": f"HOUSE#{household_id}", "sk": "META"}),
                            "UpdateExpression": "ADD membership_version :one",
                            "ConditionExpression": "#status=:active AND owner_user_id=:owner",
                            "ExpressionAttributeNames": {"#status": "status"},
                            "ExpressionAttributeValues": self._ddb({
                                ":one": 1,
                                ":active": "active",
                                ":owner": household.owner_user_id,
                            }),
                        }
                    },
                    {
                        "Update": {
                            "TableName": self.settings.core_table,
                            "Key": self._ddb({"pk": f"USER#{user_id}", "sk": "PROFILE"}),
                            "UpdateExpression": (
                                "SET household_link_status=:unlinked "
                                "REMOVE household_id, #role, linked_at"
                            ),
                            "ConditionExpression": "household_id=:house AND #role=:member",
                            "ExpressionAttributeNames": {"#role": "role"},
                            "ExpressionAttributeValues": self._ddb(
                                {
                                    ":unlinked": "unlinked",
                                    ":house": household_id,
                                    ":member": "member",
                                }
                            ),
                        }
                    },
                    {
                        "Delete": {
                            "TableName": self.settings.core_table,
                            "Key": self._ddb(
                                {"pk": f"HOUSE#{household_id}", "sk": f"MEMBER#{user_id}"}
                            ),
                            "ConditionExpression": "user_id=:user",
                            "ExpressionAttributeValues": self._ddb({":user": user_id}),
                        }
                    },
                ]
            )
            return {"household_link_status": "unlinked", "household_status": "active"}

        self.withdraw_user_account(
            user_id, now, expected_password_hash=user.password_hash, _unlink_only=True,
        )
        return {"household_link_status": "unlinked", "household_status": "inactive"}

    def _save_token(self, kind: str, token_hash: str, user_id: str, expires_at: str) -> None:
        try:
            self.client.transact_write_items(TransactItems=[
                {"Update": {
                    "TableName": self.settings.core_table,
                    "Key": self._ddb({"pk": f"USER#{user_id}", "sk": "PROFILE"}),
                    "UpdateExpression": "ADD reference_version :one",
                    "ConditionExpression": "attribute_exists(pk)",
                    "ExpressionAttributeValues": self._ddb({":one": 1}),
                }},
                {"Put": {
                    "TableName": self.settings.core_table,
                    "Item": self._ddb({
                        "pk": f"TOKEN#{token_hash}", "sk": kind,
                        "user_id": user_id,
                        "expires_at": expires_at,
                        "expires_at_epoch": int(parse_timestamp(expires_at).timestamp()),
                    }),
                }},
            ])
        except Exception as exc:
            response = getattr(exc, "response", {})
            if response.get("Error", {}).get("Code") == "TransactionCanceledException":
                raise NotFoundError("사용자를 찾을 수 없습니다.") from exc
            raise

    def save_refresh_token(self, token_hash: str, user_id: str, expires_at: str) -> None:
        self._save_token("REFRESH", token_hash, user_id, expires_at)

    def _consume_token(self, kind: str, token_hash: str, now: datetime) -> str:
        key = {"pk": f"TOKEN#{token_hash}", "sk": kind}
        item = self.core.get_item(Key=key).get("Item")
        if not item or item.get("used_at") or parse_timestamp(item["expires_at"]) <= now:
            raise NotFoundError("토큰이 폐기되었거나 만료되었습니다.")
        try:
            self.core.update_item(
                Key=key,
                UpdateExpression="SET used_at=:used",
                ConditionExpression="attribute_exists(pk) AND attribute_not_exists(used_at)",
                ExpressionAttributeValues={":used": iso_utc(now)},
            )
        except Exception as exc:
            raise NotFoundError("이미 사용된 토큰입니다.") from exc
        return item["user_id"]

    def consume_refresh_token(self, token_hash: str, now: datetime) -> str:
        return self._consume_token("REFRESH", token_hash, now)

    def list_devices(self, household_id: str) -> list[Device]:
        from boto3.dynamodb.conditions import Key

        response = self.core.query(
            KeyConditionExpression=Key("pk").eq(f"HOUSE#{household_id}") & Key("sk").begins_with("DEVICE#")
        )
        return sorted([self._device(item) for item in response.get("Items", [])], key=lambda item: item.device_id)

    def get_device(self, household_id: str, device_id: str) -> Device | None:
        item = self.core.get_item(
            Key={"pk": f"HOUSE#{household_id}", "sk": f"DEVICE#{device_id}"}
        ).get("Item")
        return self._device(item)

    def get_device_by_credential(self, credential_hash: str) -> Device | None:
        # Authentication must not read a stale alias/device after revocation.
        alias = self.core.get_item(
            Key={"pk": f"DEVICECRED#{credential_hash}", "sk": "DEVICE"},
            ConsistentRead=True,
        ).get("Item")
        if not alias or any(
            not isinstance(alias.get(field), str) or not alias[field]
            for field in ("household_id", "device_id")
        ):
            return None
        item = self.core.get_item(
            Key={"pk": f"HOUSE#{alias['household_id']}", "sk": f"DEVICE#{alias['device_id']}"},
            ConsistentRead=True,
        ).get("Item")
        try:
            device = self._device(item)
        except (TypeError, ValueError):
            return None
        if (
            device is None or device.household_id != alias["household_id"]
            or device.device_id != alias["device_id"]
            or device.credential_hash != credential_hash
        ):
            return None
        return device

    def rotate_device_credential(self, household_id: str, device_id: str, credential_hash: str) -> Device:
        device = self.get_device(household_id, device_id)
        if not device:
            raise NotFoundError("기기를 찾을 수 없습니다.")
        if credential_hash == device.credential_hash:
            raise ConflictError("현재 기기 인증정보와 다른 값을 사용해야 합니다.")
        operations = [self._registration_write_operation(household_id)]
        if device.credential_hash:
            operations.append(
                {
                    "Delete": {
                        "TableName": self.settings.core_table,
                        "Key": self._ddb(
                            {"pk": f"DEVICECRED#{device.credential_hash}", "sk": "DEVICE"}
                        ),
                        "ConditionExpression": "household_id=:house AND device_id=:device",
                        "ExpressionAttributeValues": self._ddb({
                            ":house": household_id, ":device": device_id,
                        }),
                    }
                }
            )
        operations.extend(
            [
                {
                    "Update": {
                        "TableName": self.settings.core_table,
                        "Key": self._ddb(
                            {"pk": f"HOUSE#{household_id}", "sk": f"DEVICE#{device_id}"}
                        ),
                        "UpdateExpression": "SET credential_hash=:hash",
                        "ConditionExpression": (
                            "attribute_exists(pk) AND "
                            + self._withdrawal_snapshot_clause(
                                "credential_hash", ":previous_hash", device.credential_hash,
                            )
                        ),
                        "ExpressionAttributeValues": self._ddb({
                            ":hash": credential_hash, ":previous_hash": device.credential_hash,
                        }),
                    }
                },
                {
                    "Put": {
                        "TableName": self.settings.core_table,
                        "Item": self._ddb(
                            {
                                "pk": f"DEVICECRED#{credential_hash}",
                                "sk": "DEVICE",
                                "household_id": household_id,
                                "device_id": device_id,
                            }
                        ),
                        "ConditionExpression": "attribute_not_exists(pk)",
                    }
                },
            ]
        )
        self.client.transact_write_items(TransactItems=operations)
        return self.get_device(household_id, device_id)

    def update_device_desired(self, household_id: str, device_id: str, enabled: bool) -> Device:
        try:
            result = self.core.update_item(
                Key={"pk": f"HOUSE#{household_id}", "sk": f"DEVICE#{device_id}"},
                UpdateExpression=(
                    "SET desired_mqtt_connected=:enabled, desired_updated_at=:now "
                    "ADD config_version :one"
                ),
                ConditionExpression="attribute_exists(pk)",
                ExpressionAttributeValues={":enabled": enabled, ":now": iso_utc(), ":one": 1},
                ReturnValues="ALL_NEW",
            )
        except Exception as exc:
            raise NotFoundError("기기를 찾을 수 없습니다.") from exc
        return self._device(result["Attributes"])

    def update_device_settings(
        self,
        household_id: str,
        device_id: str,
        led_alert_enabled: bool | None,
    ) -> Device:
        device = self.get_device(household_id, device_id)
        if not device:
            raise NotFoundError("기기를 찾을 수 없습니다.")
        if device.device_type != "alert_node":
            raise ConflictError(
                "LED 알림 설정은 ESP32 알림 노드에서만 변경할 수 있습니다.",
                code="DEVICE_LED_CONTROL_UNSUPPORTED",
            )
        sets = ["desired_updated_at=:now"]
        values: dict[str, Any] = {":now": iso_utc(), ":one": 1}
        if led_alert_enabled is not None:
            sets.append("led_alert_enabled=:led")
            values[":led"] = led_alert_enabled
        try:
            result = self.core.update_item(
                Key={"pk": f"HOUSE#{household_id}", "sk": f"DEVICE#{device_id}"},
                UpdateExpression="SET " + ", ".join(sets) + " ADD config_version :one",
                ConditionExpression="attribute_exists(pk)",
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
        except Exception as exc:
            raise NotFoundError("기기를 찾을 수 없습니다.") from exc
        return self._device(result["Attributes"])

    def update_device_reported(
        self, household_id: str, device_id: str, mqtt_connected: bool,
        config_version: int, firmware_version: str | None, seen_at: str | None = None,
        network_online: bool = True,
        audio_streaming: bool | None = None, microphone_ok: bool | None = None,
        audio_packets_sent: int | None = None, audio_packets_dropped: int | None = None,
        audio_clipped_samples: int | None = None,
    ) -> Device:
        key = {"pk": f"HOUSE#{household_id}", "sk": f"DEVICE#{device_id}"}
        values: dict[str, Any] = {
            ":mqtt": mqtt_connected,
            ":version": config_version,
            ":seen": seen_at or iso_utc(),
            ":online": network_online,
        }
        sets = [
            "reported_mqtt_connected=:mqtt",
            "reported_config_version=:version",
            "last_seen_at=:seen",
            "reported_network_online=:online",
        ]
        if firmware_version:
            sets.append("firmware_version=:firmware")
            values[":firmware"] = firmware_version
        audio_values = {
            "audio_streaming": audio_streaming,
            "microphone_ok": microphone_ok,
            "audio_packets_sent": audio_packets_sent,
            "audio_packets_dropped": audio_packets_dropped,
            "audio_clipped_samples": audio_clipped_samples,
        }
        for field_name, field_value in audio_values.items():
            if field_value is not None:
                token = f":{field_name}"
                sets.append(f"{field_name}={token}")
                values[token] = field_value
        try:
            result = self.core.update_item(
                Key=key,
                UpdateExpression="SET " + ", ".join(sets),
                ConditionExpression=(
                    "attribute_exists(pk) AND "
                    "(attribute_not_exists(reported_config_version) OR reported_config_version <= :version)"
                ),
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
            return self._device(result["Attributes"])
        except self.client.exceptions.ConditionalCheckFailedException:
            stale_values: dict[str, Any] = {
                ":seen": seen_at or iso_utc(),
                ":online": network_online,
            }
            stale_sets = ["last_seen_at=:seen", "reported_network_online=:online"]
            if firmware_version:
                stale_sets.append("firmware_version=:firmware")
                stale_values[":firmware"] = firmware_version
            for field_name, field_value in audio_values.items():
                if field_value is not None:
                    token = f":{field_name}"
                    stale_sets.append(f"{field_name}={token}")
                    stale_values[token] = field_value
            try:
                result = self.core.update_item(
                    Key=key,
                    UpdateExpression="SET " + ", ".join(stale_sets),
                    ConditionExpression="attribute_exists(pk)",
                    ExpressionAttributeValues=stale_values,
                    ReturnValues="ALL_NEW",
                )
                return self._device(result["Attributes"])
            except self.client.exceptions.ConditionalCheckFailedException as exc:
                raise NotFoundError("기기를 찾을 수 없습니다.") from exc

    def list_contacts(self, household_id: str) -> list[dict[str, Any]]:
        from boto3.dynamodb.conditions import Key

        response = self.core.query(
            KeyConditionExpression=Key("pk").eq(f"HOUSE#{household_id}") & Key("sk").begins_with("CONTACT#")
        )
        return sorted([_plain(item) for item in response.get("Items", [])], key=lambda item: item["name"])

    def create_contact(self, household_id: str, contact: dict[str, Any]) -> dict[str, Any]:
        try:
            self.client.transact_write_items(TransactItems=[
                self._registration_write_operation(household_id),
                {"Put": {
                    "TableName": self.settings.core_table,
                    "Item": self._ddb({
                        **contact, "pk": f"HOUSE#{household_id}",
                        "sk": f"CONTACT#{contact['contact_id']}",
                    }),
                    "ConditionExpression": "attribute_not_exists(pk)",
                }},
            ])
        except Exception as exc:
            if getattr(exc, "response", {}).get("Error", {}).get("Code") == "TransactionCanceledException":
                raise NotFoundError("활성 가구 또는 연락처 상태를 확인할 수 없습니다.") from exc
            raise
        return contact

    def update_contact(self, household_id: str, contact_id: str, contact: dict[str, Any]) -> dict[str, Any]:
        key = {"pk": f"HOUSE#{household_id}", "sk": f"CONTACT#{contact_id}"}
        current = self.core.get_item(Key=key).get("Item")
        if not current:
            raise NotFoundError("연락처를 찾을 수 없습니다.")
        updated = {**_plain(current), **contact}
        updated.update(key)
        try:
            self.client.transact_write_items(TransactItems=[
                self._registration_write_operation(household_id),
                {"Put": {
                    "TableName": self.settings.core_table,
                    "Item": self._ddb(updated),
                    "ConditionExpression": "attribute_exists(pk)",
                }},
            ])
        except Exception as exc:
            if getattr(exc, "response", {}).get("Error", {}).get("Code") == "TransactionCanceledException":
                raise NotFoundError("활성 가구 또는 연락처를 찾을 수 없습니다.") from exc
            raise
        return updated

    def delete_contact(self, household_id: str, contact_id: str) -> None:
        result = self.core.delete_item(
            Key={"pk": f"HOUSE#{household_id}", "sk": f"CONTACT#{contact_id}"},
            ReturnValues="ALL_OLD",
        )
        if not result.get("Attributes"):
            raise NotFoundError("연락처를 찾을 수 없습니다.")

    @staticmethod
    def _alert(item: dict[str, Any]) -> Alert:
        data = _plain(item)
        return Alert(**{key: data.get(key) for key in Alert.__dataclass_fields__})

    def put_alert(self, alert: Alert) -> bool:
        try:
            expires_at_epoch = alert_expiry_epoch(alert.timestamp)
        except AlertTimestampError as exc:
            raise StoreError(
                "알림 발생 시각이 올바르지 않습니다.",
                code="INVALID_ALERT_TIMESTAMP",
                field_errors={"timestamp": "유효한 ISO 8601 시각을 입력해 주세요."},
            ) from exc
        alert.expires_at_epoch = expires_at_epoch
        if is_expired_timestamp(alert.timestamp, alert_retention.utc_now()):
            return False
        item = asdict(alert)
        item["event_key"] = alert.event_key
        item["alarm_lookup_key"] = f"{alert.household_id}#{alert.event_id}"
        for field_name in (
            "confidence",
            "yamnet_score",
            "hearo_confidence",
            "applied_threshold",
        ):
            value = item.get(field_name)
            item[field_name] = Decimal(str(value)) if value is not None else None
        try:
            self.client.transact_write_items(
                TransactItems=[
                    {"ConditionCheck": {
                        "TableName": self.settings.core_table,
                        "Key": self._ddb({"pk": f"HOUSE#{alert.household_id}", "sk": "META"}),
                        "ConditionExpression": "attribute_exists(pk) AND #status=:active",
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": self._ddb({":active": "active"}),
                    }},
                    {
                        "Put": {
                            "TableName": self.settings.alerts_table,
                            "Item": self._ddb(item),
                            "ConditionExpression": "attribute_not_exists(household_id)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.settings.core_table,
                            "Item": self._ddb(
                                {
                                    "pk": f"ALERTID#{alert.household_id}#{alert.event_id}",
                                    "sk": "ALERT",
                                    "event_key": alert.event_key,
                                    "expires_at_epoch": expires_at_epoch,
                                }
                            ),
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                ]
            )
            return True
        except self.client.exceptions.TransactionCanceledException as exc:
            reasons = exc.response.get("CancellationReasons", [])
            if any(reason.get("Code") == "ConditionalCheckFailed" for reason in reasons):
                return False
            raise

    def get_alert(self, household_id: str, event_id: str) -> Alert | None:
        from boto3.dynamodb.conditions import Attr, Key
        from botocore.exceptions import ClientError

        lookup_key = f"{household_id}#{event_id}"
        now = alert_retention.utc_now()
        try:
            kwargs: dict[str, Any] = {
                "IndexName": "alarm-lookup-index",
                "KeyConditionExpression": Key("alarm_lookup_key").eq(lookup_key),
                "Limit": 25,
            }
            while True:
                response = self.alerts.query(**kwargs)
                for item in response.get("Items", []):
                    alert = self._alert(item)
                    if is_visible_alert(alert, now):
                        return alert
                last_key = response.get("LastEvaluatedKey")
                if not last_key:
                    break
                kwargs["ExclusiveStartKey"] = last_key
        except ClientError as exc:
            # 기존 배포 테이블에 인덱스가 아직 없을 때만 호환 조회를 사용합니다.
            code = exc.response.get("Error", {}).get("Code")
            if code not in {
                "AccessDeniedException",
                "ResourceNotFoundException",
                "ValidationException",
            }:
                raise

        # 기존 알림에는 alarm_lookup_key가 없으므로 가구 파티션 안에서 찾습니다.
        # 인덱스 적용 후 새 알림은 위 쿼리에서 바로 반환됩니다.
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("household_id").eq(household_id),
            "FilterExpression": Attr("event_id").eq(event_id),
            "ScanIndexForward": False,
        }
        while True:
            response = self.alerts.query(**kwargs)
            for item in response.get("Items", []):
                alert = self._alert(item)
                if is_visible_alert(alert, now):
                    return alert
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                return None
            kwargs["ExclusiveStartKey"] = last_key

    def query_alerts(self, household_id: str, start: datetime, end_exclusive: datetime) -> list[Alert]:
        from boto3.dynamodb.conditions import Key

        start_key = iso_utc(start)
        end_key = iso_utc(end_exclusive)
        now = alert_retention.utc_now()
        result: list[Alert] = []
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("household_id").eq(household_id)
            & Key("event_key").between(start_key, end_key),
            "ScanIndexForward": False,
        }
        while True:
            response = self.alerts.query(**kwargs)
            result.extend(
                alert
                for item in response.get("Items", [])
                if is_visible_alert((alert := self._alert(item)), now)
            )
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key
        return result

    def latest_alerts(self, household_id: str, limit: int) -> list[Alert]:
        from boto3.dynamodb.conditions import Key

        now = alert_retention.utc_now()
        result: list[Alert] = []
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("household_id").eq(household_id),
            "ScanIndexForward": False,
            "Limit": limit,
        }
        while len(result) < limit:
            response = self.alerts.query(**kwargs)
            result.extend(
                alert
                for item in response.get("Items", [])
                if is_visible_alert((alert := self._alert(item)), now)
            )
            if len(result) >= limit:
                break
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            kwargs["ExclusiveStartKey"] = last_key
            kwargs["Limit"] = limit - len(result)
        return result[:limit]


def create_repository(settings: Settings):
    if settings.store_backend == "dynamodb":
        return DynamoRepository(settings)
    return MemoryRepository()
