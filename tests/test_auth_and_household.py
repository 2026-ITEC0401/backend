from datetime import UTC, datetime, timedelta

from hearo_backend.domain import iso_utc
from hearo_backend.security import hash_secret

from .conftest import auth_header, create_family, create_owner


def link_family(api, owner: dict, family: dict) -> tuple[str, dict]:
    household_id = owner["user"]["household_id"]
    invite = api.client.get(
        f"/households/{household_id}/invite-code", headers=auth_header(owner)
    )
    assert invite.status_code == 200
    linked = api.client.post(
        "/households/link",
        headers=auth_header(family),
        json={"invite_code": invite.json()["invite_code"]},
    )
    assert linked.status_code == 200, linked.text
    return invite.json()["invite_code"], linked.json()


def test_signup_identity_uniqueness_and_unlinked_family_state(api):
    owner = create_owner(api)
    assert owner["user"]["login_id"] == "owner01"
    assert owner["user"]["phone_number"] == "+821012345678"
    assert owner["user"]["household_link_status"] == "linked"
    assert set(owner["device_credentials"]) == {
        "rpi-001",
        "esp32_1",
        "esp32_2",
        "esp32_3",
    }

    duplicate_login = api.client.post(
        "/auth/signup",
        json={
            "login_id": "OWNER01",
            "phone_number": "010-2222-3333",
            "password": "StrongPassword123",
            "name": "중복 아이디",
            "signup_type": "family_member",
            "terms_service_agreed": True,
            "privacy_agreed": True,
        },
    )
    assert duplicate_login.status_code == 409
    assert duplicate_login.json()["code"] == "LOGIN_ID_ALREADY_EXISTS"

    duplicate_phone = api.client.post(
        "/auth/signup",
        json={
            "login_id": "another01",
            "phone_number": "01012345678",
            "password": "StrongPassword123",
            "name": "중복 번호",
            "signup_type": "family_member",
            "terms_service_agreed": True,
            "privacy_agreed": True,
        },
    )
    assert duplicate_phone.status_code == 409
    assert duplicate_phone.json()["code"] == "PHONE_NUMBER_ALREADY_EXISTS"

    family = create_family(api)
    assert family["user"]["household_id"] is None
    assert family["user"]["role"] is None
    current = api.client.get("/households/current", headers=auth_header(family))
    assert current.json() == {
        "household_link_status": "unlinked",
        "household": None,
        "membership": None,
    }
    blocked = api.client.get(
        f"/households/{owner['user']['household_id']}/devices",
        headers=auth_header(family),
    )
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "HOUSEHOLD_LINK_REQUIRED"


def test_signup_and_password_change_require_letter_and_number(api):
    invalid_signup = api.client.post(
        "/auth/signup",
        json={
            "login_id": "weakpass01",
            "phone_number": "010-2222-1000",
            "password": "1234567890",
            "name": "약한 비밀번호",
            "signup_type": "family_member",
            "terms_service_agreed": True,
            "privacy_agreed": True,
        },
    )
    assert invalid_signup.status_code == 422
    assert "password" in invalid_signup.json()["field_errors"]

    owner = create_owner(api)
    invalid_change = api.client.patch(
        "/me/password",
        headers=auth_header(owner),
        json={
            "current_password": "StrongPassword123",
            "new_password": "onlyletterslong",
        },
    )
    assert invalid_change.status_code == 422
    assert "new_password" in invalid_change.json()["field_errors"]


def test_signup_invalid_phone_has_field_specific_error(api):
    response = api.client.post(
        "/auth/signup",
        json={
            "login_id": "badphone01",
            "phone_number": "12345",
            "password": "StrongPassword123",
            "name": "잘못된 번호",
            "signup_type": "family_member",
            "terms_service_agreed": True,
            "privacy_agreed": True,
        },
    )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_PHONE_NUMBER"
    assert "phone_number" in response.json()["field_errors"]


def test_reusable_invite_preview_link_rotation_and_permissions(api):
    owner = create_owner(api)
    household_id = owner["user"]["household_id"]
    owner_headers = auth_header(owner)
    current = api.client.get(
        f"/households/{household_id}/invite-code", headers=owner_headers
    )
    assert current.status_code == 200
    code = current.json()["invite_code"]
    assert len(code) == 6

    first = create_family(api)
    preview = api.client.post(
        "/households/link/preview",
        headers=auth_header(first),
        json={"invite_code": code.lower()},
    )
    assert preview.status_code == 200
    assert preview.json()["household"]["member_count"] == 1
    assert "address" not in str(preview.json()).casefold()
    linked = api.client.post(
        "/households/link",
        headers=auth_header(first),
        json={"invite_code": code},
    )
    assert linked.status_code == 200
    assert linked.json()["household_id"] == household_id

    second = create_family(
        api, login_id="family02", phone_number="010-5555-6666", name="둘째"
    )
    reused = api.client.post(
        "/households/link",
        headers=auth_header(second),
        json={"invite_code": code},
    )
    assert reused.status_code == 200

    forbidden = api.client.patch(
        f"/households/{household_id}/devices/esp32_2/connection",
        headers=auth_header(first),
        json={"enabled": False},
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "OWNER_REQUIRED"

    rotated = api.client.post(
        f"/households/{household_id}/invite-code/rotate", headers=owner_headers
    )
    assert rotated.status_code == 200
    assert rotated.json()["invite_code"] != code
    third = create_family(
        api, login_id="family03", phone_number="010-7777-8888", name="셋째"
    )
    old_code = api.client.post(
        "/households/link",
        headers=auth_header(third),
        json={"invite_code": code},
    )
    assert old_code.status_code == 400
    assert old_code.json()["code"] in {"INVITE_CODE_NOT_FOUND", "INVITE_CODE_INACTIVE"}


def test_per_viewer_display_names_and_reset(api):
    owner = create_owner(api)
    family = create_family(api)
    _, linked = link_family(api, owner, family)
    household_id = linked["household_id"]
    owner_headers = auth_header(owner)
    family_headers = auth_header(family)
    member_id = family["user"]["user_id"]

    changed = api.client.patch(
        f"/households/{household_id}/members/{member_id}/display-name",
        headers=owner_headers,
        json={"display_name": "우리 가족"},
    )
    assert changed.status_code == 200
    owner_view = api.client.get(
        f"/households/{household_id}/members", headers=owner_headers
    ).json()["members"]
    owner_member = next(item for item in owner_view if item["user_id"] == member_id)
    assert owner_member["display_name"] == "우리 가족"
    assert owner_member["display_name_is_custom"] is True

    family_view = api.client.get(
        f"/households/{household_id}/members", headers=family_headers
    ).json()["members"]
    family_member = next(item for item in family_view if item["user_id"] == member_id)
    assert family_member["display_name"] == "가족"

    reset = api.client.delete(
        f"/households/{household_id}/members/{member_id}/display-name",
        headers=owner_headers,
    )
    assert reset.status_code == 200
    assert reset.json()["display_name"] == "가족"


def test_member_unlink_keeps_account_and_owner_unlink_deactivates_household(api):
    owner = create_owner(api)
    family = create_family(api)
    code, linked = link_family(api, owner, family)
    household_id = linked["household_id"]
    family_headers = auth_header(family)

    member_unlink = api.client.delete(
        "/households/current/link", headers=family_headers
    )
    assert member_unlink.status_code == 200
    assert member_unlink.json()["household_status"] == "active"
    assert api.client.get("/me", headers=family_headers).json()["household_id"] is None
    relink = api.client.post(
        "/households/link",
        headers=family_headers,
        json={"invite_code": code},
    )
    assert relink.status_code == 200

    owner_unlink = api.client.delete(
        "/households/current/link", headers=auth_header(owner)
    )
    assert owner_unlink.status_code == 200
    assert owner_unlink.json()["household_status"] == "inactive"
    household = api.repository.get_household(household_id)
    assert household.status == "inactive"
    assert household.emergency_address is None
    assert api.client.get("/households/current", headers=family_headers).json()[
        "household_link_status"
    ] == "unlinked"
    device_blocked = api.client.get(
        "/device/v1/config",
        headers={"X-Device-Credential": owner["device_credentials"]["rpi-001"]},
    )
    assert device_blocked.status_code == 403


def test_refresh_rotation_logout_and_logged_in_password_change(api):
    owner = create_owner(api)
    original_refresh = owner["tokens"]["refresh_token"]
    rotated = api.client.post("/auth/refresh", json={"refresh_token": original_refresh})
    assert rotated.status_code == 200
    assert api.client.post(
        "/auth/refresh", json={"refresh_token": original_refresh}
    ).status_code == 401

    new_refresh = rotated.json()["refresh_token"]
    assert api.client.post(
        "/auth/logout", json={"refresh_token": new_refresh}
    ).status_code == 204
    assert api.client.post(
        "/auth/refresh", json={"refresh_token": new_refresh}
    ).status_code == 401

    changed = api.client.patch(
        "/me/password",
        headers=auth_header(owner),
        json={
            "current_password": "StrongPassword123",
            "new_password": "NewStrongPassword456",
        },
    )
    assert changed.status_code == 204
    assert api.client.get("/me", headers=auth_header(owner)).status_code == 401
    assert api.client.post(
        "/auth/login",
        json={"login_id": "owner01", "password": "StrongPassword123"},
    ).status_code == 401
    assert api.client.post(
        "/auth/login",
        json={"login_id": "OWNER01", "password": "NewStrongPassword456"},
    ).status_code == 200


def test_expired_invite_rate_limits_and_error_contract(api):
    owner = create_owner(api)
    household_id = owner["user"]["household_id"]
    invite = api.client.get(
        f"/households/{household_id}/invite-code", headers=auth_header(owner)
    ).json()
    invite_hash = hash_secret(invite["invite_code"])
    api.repository.invites[invite_hash]["expires_at"] = iso_utc(
        datetime.now(UTC) - timedelta(seconds=1)
    )
    family = create_family(api)
    expired = api.client.post(
        "/households/link/preview",
        headers=auth_header(family),
        json={"invite_code": invite["invite_code"]},
    )
    assert expired.status_code == 400
    assert expired.json()["code"] == "INVITE_CODE_EXPIRED"
    assert expired.json()["request_id"].startswith("req-")

    for _ in range(10):
        response = api.client.post(
            "/auth/login",
            json={"login_id": "owner01", "password": "wrong-password"},
        )
        assert response.status_code == 401
    limited = api.client.post(
        "/auth/login",
        json={"login_id": "owner01", "password": "wrong-password"},
    )
    assert limited.status_code == 429
    assert set(limited.json()) == {"code", "message", "field_errors", "request_id"}

    invalid = api.client.post("/auth/login", json={"login_id": "x", "password": ""})
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "VALIDATION_ERROR"
