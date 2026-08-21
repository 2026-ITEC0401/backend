from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from hearo_backend.config import Settings
from hearo_backend.integrations import NullMqttPublisher
from hearo_backend.main import create_app
from hearo_backend.store import MemoryRepository


@dataclass
class ApiContext:
    client: TestClient
    repository: MemoryRepository
    mqtt: NullMqttPublisher
    settings: Settings


@pytest.fixture
def api() -> ApiContext:
    settings = Settings(
        environment="test",
        store_backend="memory",
        jwt_secret="test-jwt-secret-that-is-at-least-thirty-two-characters",
        internal_token="test-internal-secret-that-is-at-least-thirty-two-characters",
    )
    repository = MemoryRepository()
    mqtt = NullMqttPublisher()
    app = create_app(settings, repository, mqtt)
    with TestClient(app) as client:
        yield ApiContext(client, repository, mqtt, settings)


def create_owner(
    api: ApiContext,
    *,
    login_id: str = "owner01",
    phone_number: str = "010-1234-5678",
    name: str = "보호자",
):
    response = api.client.post(
        "/auth/signup",
        json={
            "login_id": login_id,
            "phone_number": phone_number,
            "password": "StrongPassword123",
            "name": name,
            "signup_type": "new_household",
            "household_name": f"{name} 가구",
            "emergency_address": {
                "postal_code": "41566",
                "road_address": "대구광역시 북구 대학로 80",
                "detail_address": "101동 902호",
                "address_provider": "kakao_postcode",
            },
            "terms_service_agreed": True,
            "privacy_agreed": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_family(
    api: ApiContext,
    *,
    login_id: str = "family01",
    phone_number: str = "010-9876-5432",
    name: str = "가족",
):
    response = api.client.post(
        "/auth/signup",
        json={
            "login_id": login_id,
            "phone_number": phone_number,
            "password": "MemberPassword123",
            "name": name,
            "signup_type": "family_member",
            "terms_service_agreed": True,
            "privacy_agreed": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def auth_header(signup_response: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {signup_response['tokens']['access_token']}"}
