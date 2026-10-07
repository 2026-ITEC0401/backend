import asyncio
import threading

import pytest
from starlette.websockets import WebSocketDisconnect

from hearo_backend.store import ConflictError, DynamoRepository

from .conftest import auth_header, create_family, create_owner


def _link_family(api, owner):
    household_id = owner["user"]["household_id"]
    invite = api.client.get(
        f"/households/{household_id}/invite-code",
        headers=auth_header(owner),
    )
    assert invite.status_code == 200
    family = create_family(api)
    linked = api.client.post(
        "/households/link",
        headers=auth_header(family),
        json={"invite_code": invite.json()["invite_code"]},
    )
    assert linked.status_code == 200
    return family


def _authenticate(websocket, response):
    websocket.send_json(
        {"type": "auth", "access_token": response["tokens"]["access_token"]}
    )
    ready = websocket.receive_json()
    assert ready["type"] == "connection.ready"


def _assert_policy_close(websocket):
    with pytest.raises(WebSocketDisconnect) as exc_info:
        websocket.receive_json()
    assert exc_info.value.code == 1008


def test_member_unlink_notifies_household_and_closes_existing_websocket(api):
    owner = create_owner(api)
    family = _link_family(api, owner)
    household_id = owner["user"]["household_id"]

    with api.client.websocket_connect(f"/ws/households/{household_id}") as websocket:
        _authenticate(websocket, family)

        response = api.client.delete(
            "/households/current/link", headers=auth_header(family)
        )

        assert response.status_code == 200
        assert response.json() == {
            "household_link_status": "unlinked",
            "household_status": "active",
        }
        assert websocket.receive_json() == {
            "type": "household.member_removed",
            "user_id": family["user"]["user_id"],
        }
        _assert_policy_close(websocket)


def test_password_change_closes_websocket_authenticated_by_revoked_token(api):
    owner = create_owner(api)
    household_id = owner["user"]["household_id"]

    with api.client.websocket_connect(f"/ws/households/{household_id}") as websocket:
        _authenticate(websocket, owner)

        response = api.client.patch(
            "/me/password",
            headers=auth_header(owner),
            json={
                "current_password": "StrongPassword123",
                "new_password": "ChangedPassword456",
            },
        )

        assert response.status_code == 204
        _assert_policy_close(websocket)
        assert api.client.get("/me", headers=auth_header(owner)).status_code == 401


def test_member_delete_failure_preserves_error_and_closes_existing_websocket(
    api, monkeypatch
):
    owner = create_owner(api)
    family = _link_family(api, owner)
    household_id = owner["user"]["household_id"]

    def fail_final_delete(_user_id):
        raise ConflictError(
            "계정 상태가 변경되어 삭제를 완료하지 못했습니다.",
            code="ACCOUNT_DELETION_CONFLICT",
        )

    monkeypatch.setattr(api.repository, "delete_user_account", fail_final_delete)

    with api.client.websocket_connect(f"/ws/households/{household_id}") as websocket:
        _authenticate(websocket, family)

        response = api.client.request(
            "DELETE",
            "/me",
            headers=auth_header(family),
            json={"current_password": "MemberPassword123"},
        )

        assert response.status_code == 409
        assert response.json()["code"] == "ACCOUNT_DELETION_CONFLICT"
        _assert_policy_close(websocket)
        current = api.client.get("/households/current", headers=auth_header(family))
        assert current.status_code == 200
        assert current.json()["household_link_status"] == "unlinked"


def test_owner_delete_failure_closes_every_household_websocket(api, monkeypatch):
    owner = create_owner(api)
    family = _link_family(api, owner)
    household_id = owner["user"]["household_id"]

    def fail_final_delete(_user_id):
        raise ConflictError(
            "계정 상태가 변경되어 삭제를 완료하지 못했습니다.",
            code="ACCOUNT_DELETION_CONFLICT",
        )

    monkeypatch.setattr(api.repository, "delete_user_account", fail_final_delete)

    with api.client.websocket_connect(f"/ws/households/{household_id}") as owner_ws:
        with api.client.websocket_connect(f"/ws/households/{household_id}") as member_ws:
            _authenticate(owner_ws, owner)
            _authenticate(member_ws, family)

            response = api.client.request(
                "DELETE",
                "/me",
                headers=auth_header(owner),
                json={"current_password": "StrongPassword123"},
            )

            assert response.status_code == 409
            assert response.json()["code"] == "ACCOUNT_DELETION_CONFLICT"
            _assert_policy_close(owner_ws)
            _assert_policy_close(member_ws)
            assert api.repository.get_household(household_id).status == "inactive"


def test_member_unlink_unknown_outcome_preserves_error_and_closes_websocket(
    api, monkeypatch
):
    owner = create_owner(api)
    family = _link_family(api, owner)
    household_id = owner["user"]["household_id"]
    original_unlink = api.repository.unlink_user

    def commit_then_fail(user_id, now):
        original_unlink(user_id, now)
        raise ConflictError(
            "가구 연동 해제 결과를 확인할 수 없습니다.",
            code="HOUSEHOLD_UNLINK_CONFLICT",
        )

    monkeypatch.setattr(api.repository, "unlink_user", commit_then_fail)

    with api.client.websocket_connect(f"/ws/households/{household_id}") as websocket:
        _authenticate(websocket, family)
        response = api.client.delete(
            "/households/current/link", headers=auth_header(family)
        )

        assert response.status_code == 409
        assert response.json()["code"] == "HOUSEHOLD_UNLINK_CONFLICT"
        _assert_policy_close(websocket)
        current = api.repository.get_user(family["user"]["user_id"])
        assert current.household_link_status == "unlinked"


def test_owner_unlink_unknown_outcome_closes_every_household_websocket(
    api, monkeypatch
):
    owner = create_owner(api)
    family = _link_family(api, owner)
    household_id = owner["user"]["household_id"]
    original_unlink = api.repository.unlink_user

    def commit_then_fail(user_id, now):
        original_unlink(user_id, now)
        raise ConflictError(
            "가구 연동 해제 결과를 확인할 수 없습니다.",
            code="HOUSEHOLD_UNLINK_CONFLICT",
        )

    monkeypatch.setattr(api.repository, "unlink_user", commit_then_fail)

    with api.client.websocket_connect(f"/ws/households/{household_id}") as owner_ws:
        with api.client.websocket_connect(f"/ws/households/{household_id}") as member_ws:
            _authenticate(owner_ws, owner)
            _authenticate(member_ws, family)
            response = api.client.delete(
                "/households/current/link", headers=auth_header(owner)
            )

            assert response.status_code == 409
            assert response.json()["code"] == "HOUSEHOLD_UNLINK_CONFLICT"
            _assert_policy_close(owner_ws)
            _assert_policy_close(member_ws)
            assert api.repository.get_household(household_id).status == "inactive"


def test_password_change_during_websocket_registration_is_rechecked(api, monkeypatch):
    owner = create_owner(api)
    household_id = owner["user"]["household_id"]
    manager = api.client.app.state.realtime
    original_add = manager.add
    add_started = threading.Event()
    allow_add = threading.Event()

    async def delayed_add(*args, **kwargs):
        add_started.set()
        await asyncio.to_thread(allow_add.wait)
        await original_add(*args, **kwargs)

    monkeypatch.setattr(manager, "add", delayed_add)

    with api.client.websocket_connect(f"/ws/households/{household_id}") as websocket:
        websocket.send_json(
            {"type": "auth", "access_token": owner["tokens"]["access_token"]}
        )
        try:
            assert add_started.wait(timeout=2)
            response = api.client.patch(
                "/me/password",
                headers=auth_header(owner),
                json={
                    "current_password": "StrongPassword123",
                    "new_password": "ChangedPassword456",
                },
            )
            assert response.status_code == 204
        finally:
            allow_add.set()

        _assert_policy_close(websocket)


def test_dynamo_realtime_reads_are_strongly_consistent():
    class CoreTable:
        def __init__(self):
            self.calls = []

        def get_item(self, **kwargs):
            self.calls.append(kwargs)
            return {}

    repository = object.__new__(DynamoRepository)
    repository.core = CoreTable()

    assert repository.get_user_consistent("user-1") is None
    assert repository.get_household_consistent("home-1") is None
    assert repository.core.calls == [
        {
            "Key": {"pk": "USER#user-1", "sk": "PROFILE"},
            "ConsistentRead": True,
        },
        {
            "Key": {"pk": "HOUSE#home-1", "sk": "META"},
            "ConsistentRead": True,
        },
    ]
