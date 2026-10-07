from __future__ import annotations

import threading
from dataclasses import asdict
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from boto3.dynamodb.types import TypeDeserializer

from hearo_backend.domain import Household, User
from hearo_backend.store import ConflictError, DynamoRepository, MemoryRepository, NotFoundError

from .conftest import auth_header, create_family


def user(user_id: str, *, linked: bool = False) -> User:
    return User(
        user_id=user_id,
        login_id=f"login-{user_id}",
        name=user_id,
        phone_number=f"+8210{len(user_id):08d}",
        password_hash="hash",
        account_type="family_member",
        household_id="home-1" if linked else None,
        role="member" if linked else None,
        household_link_status="linked" if linked else "unlinked",
    )


def decode(item: dict) -> dict:
    deserializer = TypeDeserializer()
    return {key: deserializer.deserialize(value) for key, value in item.items()}


class RecordingClient:
    def __init__(self):
        self.transactions: list[list[dict]] = []

    def transact_write_items(self, *, TransactItems):
        self.transactions.append(TransactItems)


def dynamo() -> DynamoRepository:
    repository = DynamoRepository.__new__(DynamoRepository)
    repository.settings = SimpleNamespace(core_table="core")
    repository.client = RecordingClient()
    return repository


def test_reference_version_is_internal_and_does_not_revoke_access_tokens():
    value = user("member-1")
    assert value.reference_version == 0
    assert value.token_version == 0
    assert "reference_version" not in value.public()


def test_http_auth_uses_current_profile_not_a_stale_eventual_read(api, monkeypatch):
    family = create_family(api)
    stale = api.repository.get_user(family["user"]["user_id"])
    monkeypatch.setattr(api.repository, "get_user", lambda _user_id: stale)
    monkeypatch.setattr(api.repository, "get_user_consistent", lambda _user_id: None)

    response = api.client.get("/me", headers=auth_header(family))
    assert response.status_code == 401
    assert response.json()["code"] == "REVOKED_ACCESS_TOKEN"


def test_withdrawal_fences_the_hash_actually_verified(monkeypatch):
    from hearo_backend import services

    repository = MemoryRepository()
    value = user("password-race")
    repository.create_unlinked_user(value)

    def verify_then_change_password(_password, password_hash):
        assert password_hash == "hash"
        repository.update_user_password(value.user_id, "new-hash")
        return True

    monkeypatch.setattr(services, "verify_password", verify_then_change_password)
    with pytest.raises(ConflictError) as exc:
        services.delete_account(repository, value, "old-password")

    assert exc.value.code == "ACCOUNT_DELETION_CONFLICT"
    assert repository.get_user(value.user_id).password_hash == "new-hash"
    assert repository.users_by_login_id[value.login_id] == value.user_id


def test_memory_reader_cannot_observe_temporary_unlink_during_failed_withdrawal(monkeypatch):
    repository = MemoryRepository()
    owner = user("owner", linked=True)
    owner.role = "owner"
    owner.account_type = "household_owner"
    repository.create_owner(owner, Household("home-1", "합성 가구", owner.user_id), [])
    deletion_started = threading.Event()
    allow_failure = threading.Event()
    read_started = threading.Event()
    read_finished = threading.Event()
    outcomes = []
    observed = []

    def fail_delete(_user_id):
        deletion_started.set()
        assert allow_failure.wait(timeout=2)
        raise ConflictError("injected failure", code="ACCOUNT_DELETION_CONFLICT")

    def withdraw():
        try:
            repository.withdraw_user_account(
                owner.user_id, datetime.now(UTC), expected_password_hash="hash",
            )
        except Exception as exc:
            outcomes.append(exc)

    def read():
        read_started.set()
        observed.append(repository.get_user(owner.user_id).household_link_status)
        read_finished.set()

    monkeypatch.setattr(repository, "delete_user_account", fail_delete)
    writer = threading.Thread(target=withdraw)
    reader = threading.Thread(target=read)
    writer.start()
    try:
        assert deletion_started.wait(timeout=2)
        reader.start()
        assert read_started.wait(timeout=2)
        assert not read_finished.wait(timeout=0.05)
    finally:
        allow_failure.set()
        writer.join(timeout=2)
        if reader.ident is not None:
            reader.join(timeout=2)
    assert not writer.is_alive() and not reader.is_alive()
    assert len(outcomes) == 1 and isinstance(outcomes[0], ConflictError)
    assert observed == ["linked"]


def test_memory_refresh_token_write_is_fenced_against_account_deletion():
    # Whichever operation gets the repository lock first, deletion must not
    # leave a token that references a missing profile.
    for index in range(20):
        repository = MemoryRepository()
        value = user(f"member-{index}")
        repository.create_unlinked_user(value)
        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def save_token():
            barrier.wait()
            try:
                repository.save_refresh_token(
                    f"token-{index}", value.user_id, "2026-10-08T00:00:00Z"
                )
            except Exception as exc:  # The delete-first outcome is expected.
                errors.append(exc)

        def delete_user():
            barrier.wait()
            repository.delete_user_account(value.user_id)

        writer = threading.Thread(target=save_token)
        deleter = threading.Thread(target=delete_user)
        writer.start()
        deleter.start()
        writer.join()
        deleter.join()

        assert value.user_id not in repository.users
        assert f"token-{index}" not in repository.refresh_tokens
        assert all(isinstance(exc, NotFoundError) for exc in errors)


def test_memory_refresh_token_increments_only_the_reference_fence():
    repository = MemoryRepository()
    value = user("member")
    repository.create_unlinked_user(value)
    repository.save_refresh_token(
        "refresh-hash", value.user_id, "2026-10-08T00:00:00Z"
    )
    assert value.reference_version == 1
    assert value.token_version == 0


def test_memory_display_name_write_fences_each_distinct_profile_once():
    repository = MemoryRepository()
    viewer = user("viewer", linked=True)
    member = user("member", linked=True)
    repository.users = {viewer.user_id: viewer, member.user_id: member}
    repository.members = {"home-1": {viewer.user_id, member.user_id}}
    repository.households = {"home-1": Household("home-1", "가구", "owner")}

    repository.set_display_name("home-1", viewer.user_id, member.user_id, "가족")
    assert viewer.reference_version == member.reference_version == 1
    assert viewer.token_version == member.token_version == 0
    assert repository.households["home-1"].registration_version == 1

    repository.set_display_name("home-1", viewer.user_id, viewer.user_id, "나")
    assert viewer.reference_version == 2
    assert member.reference_version == 1
    assert repository.households["home-1"].registration_version == 2


def test_memory_display_name_rejects_a_missing_or_unlinked_profile():
    repository = MemoryRepository()
    viewer = user("viewer", linked=True)
    member = user("member", linked=True)
    repository.users = {viewer.user_id: viewer, member.user_id: member}
    repository.members = {"home-1": {viewer.user_id, member.user_id}}
    repository.households = {"home-1": Household("home-1", "가구", "owner")}
    member.household_link_status = "unlinked"

    with pytest.raises(NotFoundError):
        repository.set_display_name("home-1", viewer.user_id, member.user_id, "가족")
    assert repository.display_names == {}
    assert viewer.reference_version == member.reference_version == 0


def test_dynamo_refresh_token_and_profile_fence_share_one_transaction():
    repository = dynamo()
    repository.save_refresh_token(
        "refresh-hash", "member-1", "2026-10-08T00:00:00Z"
    )

    update, put = repository.client.transactions[0]
    assert decode(update["Update"]["Key"]) == {
        "pk": "USER#member-1",
        "sk": "PROFILE",
    }
    assert update["Update"]["ConditionExpression"] == "attribute_exists(pk)"
    assert update["Update"]["UpdateExpression"] == "ADD reference_version :one"
    assert decode(update["Update"]["ExpressionAttributeValues"])[":one"] == 1
    token = decode(put["Put"]["Item"])
    assert token["pk"] == "TOKEN#refresh-hash"
    assert token["user_id"] == "member-1"


def test_dynamo_display_name_fences_both_profiles_and_deduplicates_self():
    repository = dynamo()
    repository.set_display_name("home-1", "viewer-1", "member-1", "가족")

    house, viewer, member, alias = repository.client.transactions[0]
    house_update = house["Update"]
    assert decode(house_update["Key"]) == {"pk": "HOUSE#home-1", "sk": "META"}
    assert house_update["UpdateExpression"] == "ADD registration_version :one"
    assert "#status=:active" in house_update["ConditionExpression"]
    assert decode(viewer["Update"]["Key"])["pk"] == "USER#viewer-1"
    assert decode(member["Update"]["Key"])["pk"] == "USER#member-1"
    for operation in (viewer, member):
        update = operation["Update"]
        assert update["UpdateExpression"] == "ADD reference_version :one"
        assert "household_id=:household" in update["ConditionExpression"]
        assert "household_link_status=:linked" in update["ConditionExpression"]
    assert decode(alias["Put"]["Item"])["sk"] == "ALIAS#viewer-1#member-1"

    repository.set_display_name("home-1", "viewer-1", "viewer-1", "나")
    assert len(repository.client.transactions[1]) == 3
    assert "Update" in repository.client.transactions[1][0]
    assert "Update" in repository.client.transactions[1][1]
    assert "Put" in repository.client.transactions[1][2]


def test_user_deserialization_defaults_legacy_reference_version_to_zero():
    stored = asdict(user("legacy"))
    stored.pop("reference_version")
    value = DynamoRepository._user(stored)
    assert value is not None
    assert value.reference_version == 0


def test_account_deletion_snapshots_reference_version_with_legacy_zero_support():
    class DeletionTable:
        def get_item(self, **_):
            return {"Item": asdict(user("member"))}

        def scan(self, **_):
            return {"Items": []}

        def query(self, **_):
            return {"Items": []}

    repository = dynamo()
    repository.core = DeletionTable()
    repository.delete_user_account("member")

    profile_delete = repository.client.transactions[0][2]["Delete"]
    assert "reference_version" in profile_delete["ConditionExpression"]
    values = decode(profile_delete["ExpressionAttributeValues"])
    assert values[":snapshot_reference_version"] == 0
    assert "attribute_not_exists(reference_version)" in profile_delete["ConditionExpression"]
    assert "attribute_not_exists(terms_service_agreed)" in profile_delete["ConditionExpression"]
    assert "attribute_not_exists(privacy_agreed)" in profile_delete["ConditionExpression"]


def test_password_and_token_updates_cannot_recreate_deleted_records():
    class UpdateTable:
        def __init__(self):
            self.updates = []

        def get_item(self, **kwargs):
            return {"Item": {**kwargs["Key"], "user_id": "member", "expires_at": "2026-10-08T00:00:00Z"}}

        def update_item(self, **kwargs):
            self.updates.append(kwargs)
            return {"Attributes": asdict(user("member"))}

    from datetime import UTC, datetime

    repository = dynamo()
    table = UpdateTable()
    repository.core = table
    repository.update_user_password("member", "new-test-hash")
    repository.consume_refresh_token("refresh-hash", datetime(2026, 10, 7, tzinfo=UTC))
    assert all("attribute_exists(pk)" in item["ConditionExpression"] for item in table.updates)


def test_memory_password_update_on_deleted_profile_is_not_found():
    repository = MemoryRepository()
    with pytest.raises(NotFoundError):
        repository.update_user_password("deleted-user", "test-hash")
