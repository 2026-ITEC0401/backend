from .conftest import auth_header, create_family, create_owner


def address_payload(**overrides):
    value = {
        "postal_code": "41566",
        "road_address": "대구광역시 북구 대학로 80",
        "detail_address": "101동 902호",
        "address_provider": "kakao_postcode",
    }
    value.update(overrides)
    return value


def owner_signup_payload(**overrides):
    value = {
        "login_id": "address01",
        "phone_number": "010-1111-2222",
        "password": "StrongPassword123",
        "name": "주소 사용자",
        "signup_type": "new_household",
        "household_name": "주소 가구",
        "emergency_address": address_payload(),
        "terms_service_agreed": True,
        "privacy_agreed": True,
        "age_over_14_agreed": True,
    }
    value.update(overrides)
    return value


def test_new_household_allows_missing_address_but_validates_address_when_present(api):
    missing_value = owner_signup_payload()
    missing_value.pop("emergency_address")
    missing = api.client.post("/auth/signup", json=missing_value)
    assert missing.status_code == 201
    missing_household_id = missing.json()["user"]["household_id"]
    stored_household = api.repository.get_household(missing_household_id)
    assert stored_household.emergency_address is None
    current = api.client.get(
        "/households/current", headers=auth_header(missing.json())
    )
    assert current.json()["onboarding"] == {
        "required": True,
        "missing_steps": ["emergency_address"],
        "next_action": "register_emergency_address",
        "can_edit_emergency_address": True,
    }

    invalid = api.client.post(
        "/auth/signup",
        json=owner_signup_payload(
            login_id="address02",
            phone_number="010-1111-2223",
            emergency_address=address_payload(postal_code="1234"),
        ),
    )
    assert invalid.status_code == 422

    control_character = api.client.post(
        "/auth/signup",
        json=owner_signup_payload(
            login_id="address03",
            phone_number="010-1111-2224",
            emergency_address=address_payload(
                road_address="대구광역시\n북구 대학로 80"
            ),
        ),
    )
    assert control_character.status_code == 422

    timestamp = api.client.post(
        "/auth/signup",
        json=owner_signup_payload(
            login_id="address04",
            phone_number="010-1111-2225",
            emergency_address=address_payload(updated_at="2000-01-01T00:00:00Z"),
        ),
    )
    assert timestamp.status_code == 422


def test_emergency_address_read_update_permissions_and_non_disclosure(api):
    owner = create_owner(api)
    household_id = owner["user"]["household_id"]
    owner_headers = auth_header(owner)
    current = api.client.get("/households/current", headers=owner_headers)
    assert current.status_code == 200
    assert "postal_code" not in str(current.json())
    assert "road_address" not in str(current.json())

    initial = api.client.get(
        f"/households/{household_id}/emergency-address", headers=owner_headers
    )
    assert initial.status_code == 200
    assert initial.json()["postal_code"] == "41566"
    assert "updated_at" in initial.json()
    assert initial.json()["verified"] is False

    member = create_family(api)
    invite = api.client.get(
        f"/households/{household_id}/invite-code", headers=owner_headers
    ).json()
    linked = api.client.post(
        "/households/link",
        headers=auth_header(member),
        json={"invite_code": invite["invite_code"]},
    )
    assert linked.status_code == 200
    member_headers = auth_header(member)
    member_read = api.client.get(
        f"/households/{household_id}/emergency-address", headers=member_headers
    )
    assert member_read.status_code == 200

    member_write = api.client.patch(
        f"/households/{household_id}/emergency-address",
        headers=member_headers,
        json=address_payload(detail_address="변경 시도"),
    )
    assert member_write.status_code == 403

    updated = api.client.patch(
        f"/households/{household_id}/emergency-address",
        headers=owner_headers,
        json=address_payload(
            postal_code="03027",
            road_address="서울특별시 종로구 청와대로 1",
            detail_address="본관",
            address_provider="juso_go_kr",
        ),
    )
    assert updated.status_code == 200
    assert updated.json()["postal_code"] == "03027"
    assert updated.json()["verified"] is False

    outsider = create_owner(
        api,
        login_id="outside01",
        phone_number="010-3333-4444",
        name="외부인",
    )
    blocked = api.client.get(
        f"/households/{household_id}/emergency-address",
        headers=auth_header(outsider),
    )
    assert blocked.status_code == 403

    members = api.client.get(
        f"/households/{household_id}/members", headers=owner_headers
    ).json()["members"]
    assert "address" not in str(members).casefold()


def test_family_signup_rejects_household_address(api):
    payload = {
        "login_id": "family99",
        "phone_number": "010-4444-5555",
        "password": "MemberPassword123",
        "name": "가족",
        "signup_type": "family_member",
        "emergency_address": address_payload(),
        "terms_service_agreed": True,
        "privacy_agreed": True,
        "age_over_14_agreed": True,
    }
    response = api.client.post("/auth/signup", json=payload)
    assert response.status_code == 422


def test_addressless_household_member_waits_for_owner_and_cannot_write(api):
    owner_payload = owner_signup_payload(
        login_id="noaddress01", phone_number="010-7777-1001"
    )
    owner_payload.pop("emergency_address")
    owner_response = api.client.post("/auth/signup", json=owner_payload)
    assert owner_response.status_code == 201
    owner = owner_response.json()
    household_id = owner["user"]["household_id"]

    member = create_family(
        api,
        login_id="waitmember01",
        phone_number="010-7777-1002",
    )
    invite = api.client.get(
        f"/households/{household_id}/invite-code", headers=auth_header(owner)
    ).json()
    assert api.client.post(
        "/households/link",
        headers=auth_header(member),
        json={"invite_code": invite["invite_code"]},
    ).status_code == 200

    current = api.client.get(
        "/households/current", headers=auth_header(member)
    )
    assert current.json()["onboarding"] == {
        "required": True,
        "missing_steps": ["emergency_address"],
        "next_action": "wait_for_owner",
        "can_edit_emergency_address": False,
    }
    missing = api.client.get(
        f"/households/{household_id}/emergency-address",
        headers=auth_header(member),
    )
    assert missing.status_code == 404
    assert missing.json()["code"] == "EMERGENCY_ADDRESS_NOT_FOUND"
    forbidden = api.client.patch(
        f"/households/{household_id}/emergency-address",
        headers=auth_header(member),
        json=address_payload(),
    )
    assert forbidden.status_code == 403
