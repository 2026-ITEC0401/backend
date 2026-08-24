from __future__ import annotations

from hearo_backend.config import Settings
from hearo_backend.juso import JusoClient, JusoError

from .conftest import auth_header, create_family, create_owner


REFERENCE = {
    "adm_cd": "2723011100",
    "road_name_code": "272303145001",
    "underground": "0",
    "building_main_no": 80,
    "building_sub_no": 0,
    "apartment": True,
}


class FakeJusoClient:
    def __init__(self):
        self.road_verified = True
        self.detail_verified = "101동 9층 902호"
        self.error: JusoError | None = None

    def _raise_if_needed(self):
        if self.error:
            raise self.error

    def search_roads(self, keyword, *, page, page_size):
        self._raise_if_needed()
        return {
            "page": page,
            "page_size": page_size,
            "total_count": 1,
            "items": [
                {
                    "postal_code": "41566",
                    "road_address": "대구광역시 북구 대학로 80",
                    "building_name": "경북대학교",
                    "detail_supported": True,
                    "provider_reference": REFERENCE,
                }
            ],
        }

    def search_details(self, reference, *, search_type, dong_name=None):
        self._raise_if_needed()
        assert reference == REFERENCE
        if search_type == "dong":
            items = [
                {
                    "dong_name": "101",
                    "floor_name": None,
                    "ho_name": None,
                    "formatted_detail_address": "101동",
                }
            ]
        else:
            assert dong_name == "101"
            items = [
                {
                    "dong_name": "101",
                    "floor_name": "9",
                    "ho_name": "902",
                    "formatted_detail_address": "101동 9층 902호",
                }
            ]
        return {
            "search_type": search_type,
            "items": items,
            "manual_input_allowed": True,
        }

    def verify_road(self, *, postal_code, road_address, reference):
        self._raise_if_needed()
        assert postal_code == "41566"
        assert road_address == "대구광역시 북구 대학로 80"
        assert reference == REFERENCE
        return self.road_verified

    def verify_detail(self, reference, selection):
        self._raise_if_needed()
        assert reference == REFERENCE
        assert selection == {
            "dong_name": "101",
            "floor_name": "9",
            "ho_name": "902",
        }
        return self.detail_verified


def juso_address_payload(**overrides):
    value = {
        "postal_code": "41566",
        "road_address": "대구광역시 북구 대학로 80",
        "detail_address": "클라이언트가 보낸 값은 저장하지 않음",
        "address_provider": "juso_go_kr",
        "provider_reference": REFERENCE,
        "detail_source": "juso",
        "juso_detail": {
            "dong_name": "101",
            "floor_name": "9",
            "ho_name": "902",
        },
    }
    value.update(overrides)
    return value


def test_owner_can_proxy_road_and_detail_search_but_member_cannot(api):
    fake = FakeJusoClient()
    api.client.app.state.juso = fake
    owner = create_owner(api)
    household_id = owner["user"]["household_id"]

    roads = api.client.post(
        f"/households/{household_id}/address-search/roads",
        headers=auth_header(owner),
        json={"keyword": "대구 북구 대학로 80"},
    )
    assert roads.status_code == 200
    assert roads.json()["items"][0]["provider_reference"] == REFERENCE

    dongs = api.client.post(
        f"/households/{household_id}/address-search/details",
        headers=auth_header(owner),
        json={"provider_reference": REFERENCE, "search_type": "dong"},
    )
    assert dongs.status_code == 200
    assert dongs.json()["items"][0]["formatted_detail_address"] == "101동"

    floor_and_ho = api.client.post(
        f"/households/{household_id}/address-search/details",
        headers=auth_header(owner),
        json={
            "provider_reference": REFERENCE,
            "search_type": "floorho",
            "dong_name": "101",
        },
    )
    assert floor_and_ho.status_code == 200

    member = create_family(api)
    invite = api.client.get(
        f"/households/{household_id}/invite-code", headers=auth_header(owner)
    ).json()
    assert api.client.post(
        "/households/link",
        headers=auth_header(member),
        json={"invite_code": invite["invite_code"]},
    ).status_code == 200
    forbidden = api.client.post(
        f"/households/{household_id}/address-search/roads",
        headers=auth_header(member),
        json={"keyword": "대구 북구 대학로 80"},
    )
    assert forbidden.status_code == 403


def test_juso_verified_value_is_server_derived_and_manual_fallback_is_false(api):
    fake = FakeJusoClient()
    api.client.app.state.juso = fake
    owner = create_owner(api)
    household_id = owner["user"]["household_id"]

    verified = api.client.patch(
        f"/households/{household_id}/emergency-address",
        headers=auth_header(owner),
        json=juso_address_payload(),
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["verified"] is True
    assert verified.json()["detail_address"] == "101동 9층 902호"
    current = api.client.get("/households/current", headers=auth_header(owner))
    assert current.json()["onboarding"] == {
        "required": False,
        "missing_steps": [],
        "next_action": None,
        "can_edit_emergency_address": True,
    }

    client_cannot_force = api.client.patch(
        f"/households/{household_id}/emergency-address",
        headers=auth_header(owner),
        json={**juso_address_payload(), "verified": True},
    )
    assert client_cannot_force.status_code == 422

    manual = api.client.patch(
        f"/households/{household_id}/emergency-address",
        headers=auth_header(owner),
        json={
            "postal_code": "41566",
            "road_address": "대구광역시 북구 대학로 80",
            "detail_address": "101동 902호",
            "address_provider": "manual",
            "detail_source": "manual",
        },
    )
    assert manual.status_code == 200
    assert manual.json()["verified"] is False


def test_juso_mismatch_and_provider_outage_are_explicit(api):
    fake = FakeJusoClient()
    api.client.app.state.juso = fake
    owner = create_owner(api)
    household_id = owner["user"]["household_id"]

    fake.road_verified = False
    mismatch = api.client.patch(
        f"/households/{household_id}/emergency-address",
        headers=auth_header(owner),
        json=juso_address_payload(),
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["code"] == "ADDRESS_SELECTION_MISMATCH"

    fake.error = JusoError(
        "주소 검색 서비스를 일시적으로 사용할 수 없습니다.",
        code="ADDRESS_PROVIDER_UNAVAILABLE",
        status_code=503,
    )
    unavailable = api.client.post(
        f"/households/{household_id}/address-search/roads",
        headers=auth_header(owner),
        json={"keyword": "대구 북구 대학로 80"},
    )
    assert unavailable.status_code == 503
    assert unavailable.json()["code"] == "ADDRESS_PROVIDER_UNAVAILABLE"
    assert "confmKey" not in unavailable.text


def test_invalid_detail_search_contract_is_rejected_before_provider_call(api):
    fake = FakeJusoClient()
    api.client.app.state.juso = fake
    owner = create_owner(api)
    household_id = owner["user"]["household_id"]
    invalid = api.client.post(
        f"/households/{household_id}/address-search/details",
        headers=auth_header(owner),
        json={"provider_reference": REFERENCE, "search_type": "floorho"},
    )
    assert invalid.status_code == 422
    assert "dong_name" in invalid.json()["field_errors"] or "body" in invalid.json()["field_errors"]


def test_address_search_rate_limit_is_enforced(api):
    api.client.app.state.juso = FakeJusoClient()
    owner = create_owner(api)
    household_id = owner["user"]["household_id"]
    for _ in range(30):
        response = api.client.post(
            f"/households/{household_id}/address-search/roads",
            headers=auth_header(owner),
            json={"keyword": "대구 북구 대학로 80"},
        )
        assert response.status_code == 200
    limited = api.client.post(
        f"/households/{household_id}/address-search/roads",
        headers=auth_header(owner),
        json={"keyword": "대구 북구 대학로 80"},
    )
    assert limited.status_code == 429
    assert limited.json()["code"] == "ADDRESS_SEARCH_RATE_LIMITED"


def test_real_juso_client_maps_provider_response_without_exposing_key(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "results": {
                    "common": {
                        "errorCode": "0",
                        "errorMessage": "정상",
                        "totalCount": "1",
                        "currentPage": "1",
                        "countPerPage": "10",
                    },
                    "juso": [
                        {
                            "zipNo": "41566",
                            "roadAddrPart1": "대구광역시 북구 대학로 80",
                            "bdNm": "경북대학교",
                            "bdKdcd": "1",
                            "admCd": "2723011100",
                            "rnMgtSn": "272303145001",
                            "udrtYn": "0",
                            "buldMnnm": "80",
                            "buldSlno": "0",
                        }
                    ],
                }
            }

    def fake_get(url, *, params, timeout, follow_redirects):
        captured.update({"url": url, "params": params, "timeout": timeout})
        assert follow_redirects is False
        return Response()

    monkeypatch.setattr("hearo_backend.juso.httpx.get", fake_get)
    client = JusoClient(
        Settings(juso_confirm_key="server-side-search-key", juso_timeout_seconds=4)
    )
    result = client.search_roads("대구 북구 대학로 80")
    assert result["items"][0]["provider_reference"] == REFERENCE
    assert captured["params"]["confmKey"] == "server-side-search-key"
    assert "server-side-search-key" not in str(result)
