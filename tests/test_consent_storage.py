from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from boto3.dynamodb.types import TypeDeserializer

from hearo_backend.domain import Household, User
from hearo_backend.legal_storage import consent_receipt
from hearo_backend.store import ConflictError, DynamoRepository, MemoryRepository, NotFoundError


def user(**kwargs):
    base = User(
        user_id="user-1", login_id="demo01", name="테스트",
        phone_number="+821012345678", password_hash="test-hash",
        account_type="family_member", terms_service_agreed=True, privacy_agreed=True,
        consented_at="2026-10-07T00:00:00Z",
    )
    return replace(base, **kwargs)


def decode(item):
    serializer = TypeDeserializer()
    return {key: serializer.deserialize(value) for key, value in item.items()}


class Table:
    def __init__(self, values):
        self.values = list(values)
        self.reads = []

    def get_item(self, **kwargs):
        self.reads.append(kwargs)
        value = self.values.pop(0) if len(self.values) > 1 else self.values[0]
        return {"Item": asdict(value)} if value else {}


class Client:
    def __init__(self, failure=None):
        self.transactions = []
        self.failure = failure

    def transact_write_items(self, **kwargs):
        self.transactions.append(kwargs["TransactItems"])
        if self.failure:
            failure, self.failure = self.failure, None
            raise failure


def dynamo(values, failure=None):
    repo = DynamoRepository.__new__(DynamoRepository)
    repo.settings = SimpleNamespace(core_table="core")
    repo.core = Table(values)
    repo.client = Client(failure)
    return repo


def test_legacy_has_no_invented_receipt_and_new_receipt_is_minimal():
    assert consent_receipt(user()) is None
    receipt = consent_receipt(user(terms_version="v1", privacy_version="p1"))
    assert receipt["sk"].startswith("CONSENT#")
    assert receipt["consented_at"] == "2026-10-07T00:00:00Z"
    assert not {"name", "phone_number", "password_hash", "household_id", "expires_at_epoch"} & receipt.keys()


def test_memory_initial_consent_and_idempotent_reconsent_then_delete():
    repo = MemoryRepository()
    value = user(terms_version="v1", privacy_version="p1")
    repo.create_unlinked_user(value)
    assert len(repo.legal_consents[value.user_id]) == 1
    same = repo.record_legal_consent(value.user_id, "v1", "p1", "2026-10-08T00:00:00Z")
    assert same.consented_at == value.consented_at
    assert len(repo.legal_consents[value.user_id]) == 1
    updated = repo.record_legal_consent(value.user_id, "v2", "p1", "2026-10-08T00:00:00Z")
    assert updated.terms_version == "v2"
    assert len(repo.legal_consents[value.user_id]) == 2
    repo.delete_user_account(value.user_id)
    assert value.user_id not in repo.legal_consents
    with pytest.raises(NotFoundError):
        repo.record_legal_consent(value.user_id, "v2", "p1", "2026-10-08T01:00:00Z")


def test_owner_initial_receipt_is_in_the_same_creation_transaction():
    value = user(terms_version="v1", privacy_version="p1", role="owner")
    repo = dynamo([value])
    repo.create_owner(value, Household("home-1", "집", value.user_id), [])
    items = [decode(operation["Put"]["Item"]) for operation in repo.client.transactions[0]]
    assert len([item for item in items if item["sk"] == "PROFILE"]) == 1
    assert len([item for item in items if item["sk"].startswith("CONSENT#")]) == 1


def test_family_initial_receipt_is_in_the_same_creation_transaction():
    value = user(terms_version="v1", privacy_version="p1")
    repo = dynamo([value])
    repo.create_unlinked_user(value)
    items = [decode(operation["Put"]["Item"]) for operation in repo.client.transactions[0]]
    assert len(items) == 4
    assert items[-1]["sk"].startswith("CONSENT#")


def test_dynamo_updates_profile_and_receipt_atomically_without_profile_recreation():
    repo = dynamo([user()])
    result = repo.record_legal_consent("user-1", "v1", "p1", "2026-10-08T00:00:00Z")
    operations = repo.client.transactions[0]
    assert len(operations) == 2
    update = operations[0]["Update"]
    assert "attribute_exists(pk)" in update["ConditionExpression"]
    assert "token_version" in update["ConditionExpression"]
    assert "privacy_version=:old_privacy" in update["ConditionExpression"]
    assert "consented_at=:old_at" in update["ConditionExpression"]
    assert repo.core.reads[0]["ConsistentRead"] is True
    receipt = decode(operations[1]["Put"]["Item"])
    assert receipt["terms_version"] == result.terms_version == "v1"
    assert receipt["privacy_version"] == result.privacy_version == "p1"
    assert receipt["consented_at"] == result.consented_at


def test_dynamo_retry_after_same_version_race_preserves_the_first_consent_time():
    failure = RuntimeError("condition failed")
    failure.response = {"Error": {"Code": "TransactionCanceledException"},
                        "CancellationReasons": [{"Code": "ConditionalCheckFailed"}]}
    first = user(terms_version="v1", privacy_version="p1", consented_at="2026-10-08T00:00:00Z")
    repo = dynamo([user(), first], failure)
    result = repo.record_legal_consent("user-1", "v1", "p1", "2026-10-08T00:00:01Z")
    assert result.consented_at == first.consented_at
    assert len(repo.client.transactions) == 1


def test_dynamo_delete_race_cannot_recreate_profile():
    failure = RuntimeError("condition failed")
    failure.response = {"Error": {"Code": "TransactionCanceledException"},
                        "CancellationReasons": [{"Code": "ConditionalCheckFailed"}]}
    repo = dynamo([user(), None], failure)
    with pytest.raises(NotFoundError):
        repo.record_legal_consent("user-1", "v1", "p1", "2026-10-08T00:00:00Z")


def test_earlier_consent_timestamp_cannot_overwrite_newer_receipt():
    repo = dynamo([user(terms_version="v1", privacy_version="p1")])
    with pytest.raises(ConflictError):
        repo.record_legal_consent("user-1", "v2", "p1", "2026-10-06T00:00:00Z")
    assert repo.client.transactions == []


def test_non_conditional_aws_error_is_not_hidden_as_consent_success():
    failure = RuntimeError("storage unavailable")
    repo = dynamo([user()], failure)
    with pytest.raises(RuntimeError, match="storage unavailable"):
        repo.record_legal_consent("user-1", "v1", "p1", "2026-10-08T00:00:00Z")


@pytest.mark.parametrize("old_at", [None, "bad-date", "2026-10-07T00:00:00"])
def test_malformed_existing_consent_can_be_repaired_in_storage(old_at):
    value = user(terms_version="v1", privacy_version="p1", consented_at=old_at)
    repo = dynamo([value])
    result = repo.record_legal_consent("user-1", "v1", "p1", "2026-10-08T00:00:00Z")
    assert result.consented_at == "2026-10-08T00:00:00Z"
    assert len(repo.client.transactions) == 1
    memory = MemoryRepository()
    memory.users[value.user_id] = value
    result = memory.record_legal_consent("user-1", "v1", "p1", "2026-10-08T00:00:00Z")
    assert len(memory.legal_consents["user-1"]) == 1


class DeletionTable(Table):
    def __init__(self, value, receipts=1, query_failure=False):
        super().__init__([value])
        self.receipts = receipts
        self.query_failure = query_failure

    def scan(self, **kwargs):
        assert kwargs["ConsistentRead"] is True
        return {"Items": []}

    def query(self, **kwargs):
        assert kwargs["ConsistentRead"] is True
        if self.query_failure:
            raise RuntimeError("query unavailable")
        return {"Items": [
            {"pk": "USER#user-1", "sk": f"CONSENT#{index:03d}"}
            for index in range(self.receipts)
        ]}


def test_withdrawal_deletes_receipts_and_identity_in_one_transaction():
    repo = dynamo([user()])
    repo.core = DeletionTable(user(), receipts=2)
    repo.delete_user_account("user-1")
    operations = repo.client.transactions[0]
    profile = operations[2]["Delete"]
    assert "household_link_status" in profile["ConditionExpression"]
    assert "consented_at" in profile["ConditionExpression"]
    assert "terms_version" in profile["ConditionExpression"]
    assert "token_version" in profile["ConditionExpression"]
    assert len(operations) == 5
    assert all(decode(item["Delete"]["Key"])["sk"].startswith("CONSENT#") for item in operations[3:])


def test_withdrawal_query_failure_does_not_remove_profile_or_aliases():
    repo = dynamo([user()])
    repo.core = DeletionTable(user(), query_failure=True)
    with pytest.raises(RuntimeError, match="query unavailable"):
        repo.delete_user_account("user-1")
    assert repo.client.transactions == []


def test_withdrawal_transaction_failure_leaves_retryable_identity():
    repo = dynamo([user()], RuntimeError("storage unavailable"))
    repo.core = DeletionTable(user())
    with pytest.raises(ConflictError):
        repo.delete_user_account("user-1")
    operations = repo.client.transactions[0]
    assert len(operations) == 4  # AWS applies all or none; no post-PROFILE cleanup.
    assert any(decode(item["Delete"]["Key"])["sk"].startswith("CONSENT#") for item in operations)


def test_oversized_withdrawal_fails_closed_before_destroying_consent_history():
    repo = dynamo([user()])
    repo.core = DeletionTable(user(), receipts=205)
    with pytest.raises(ConflictError) as exc:
        repo.delete_user_account("user-1")
    assert exc.value.code == "ACCOUNT_DELETION_REVIEW_REQUIRED"
    assert repo.client.transactions == []


def test_many_refresh_tokens_are_cleaned_without_partially_deleting_consent_history():
    class ManyTokensTable(DeletionTable):
        def scan(self, **kwargs):
            return {"Items": [{"pk": f"TOKEN#{index:03d}", "sk": "REFRESH"} for index in range(205)]}

    repo = dynamo([user()])
    repo.core = ManyTokensTable(user(), receipts=2)
    repo.delete_user_account("user-1")
    first, second, final = repo.client.transactions
    assert len(first) == len(second) == 100
    for operations in (first, second):
        assert "ConditionCheck" in operations[0]
        assert "reference_version" in operations[0]["ConditionCheck"]["ConditionExpression"]
        assert all(decode(item["Delete"]["Key"])["pk"].startswith("TOKEN#") for item in operations[1:])
    final_keys = [decode(item["Delete"]["Key"]) for item in final]
    assert {key["sk"] for key in final_keys} >= {"PROFILE", "CONSENT#000", "CONSENT#001"}
    assert len(final) == 12  # Identity 3 + token remainder 7 + receipts 2.
