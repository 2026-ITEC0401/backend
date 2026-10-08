from pathlib import Path

from fastapi.testclient import TestClient

from hearo_backend.config import Settings
from hearo_backend import __version__
from hearo_backend.main import create_app


ROOT = Path(__file__).resolve().parents[1]


def test_health_and_unpublished_policy_release_contract():
    settings = Settings(environment="test", store_backend="memory", mqtt_enabled=False)
    with TestClient(create_app(settings=settings)) as client:
        assert client.get("/health").json() == {"status": "ok", "version": __version__}
        response = client.get("/legal/policies")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["configured"] is False
        paths = client.get("/openapi.json").json()["paths"]
        assert "get" in paths["/me/consents"] and "patch" in paths["/me/consents"]


def test_r6_service_templates_keep_production_and_staging_isolated():
    files = {
        "hearo-api-v2-r6.service": ("--port 8001", "/etc/hearo/hearo-api-v2.env"),
        "hearo-api-v2-r6-staging.service": ("--port 8002", "/etc/hearo/hearo-api-v2-r6-staging.env"),
        "hearo-mqtt-bridge-v2-r6.service": ("hearo_backend.mqtt_bridge", "/etc/hearo/hearo-mqtt-bridge-v2.env"),
    }
    for name, (entry, env_path) in files.items():
        text = (ROOT / "infra/systemd" / name).read_text(encoding="utf-8")
        assert "WorkingDirectory=/opt/hearo-backend-v2-r6" in text
        assert "ExecStart=/opt/hearo-backend-v2-r6/.venv/bin/python" in text
        assert entry in text and f"EnvironmentFile={env_path}" in text


def test_legal_copies_are_utf8_90_day_unpublished_drafts():
    privacy = (ROOT / "docs/legal/PRIVACY_POLICY_DRAFT.md").read_text(encoding="utf-8")
    terms = (ROOT / "docs/legal/TERMS_OF_SERVICE_DRAFT.md").read_text(encoding="utf-8")
    assert "발생 시각부터 90일" in privacy
    for text in (privacy, terms):
        assert "시행일과 문서 버전은 공개 전에 확정" in text
        assert "2026년 10월 5일" not in text
        assert "\ufffd" not in text
        assert "\\##" not in text


def test_public_release_docs_do_not_publish_unapproved_legal_contacts():
    privacy = (ROOT / "docs/legal/PRIVACY_POLICY_DRAFT.md").read_text(
        encoding="utf-8"
    )
    terms = (ROOT / "docs/legal/TERMS_OF_SERVICE_DRAFT.md").read_text(
        encoding="utf-8"
    )
    privacy_contact_section = privacy.split(
        "## 제12조 개인정보 보호책임자", maxsplit=1
    )[1]
    terms_contact_section = terms.split(
        "## 제16조 이용자의 의견 및 문의", maxsplit=1
    )[1]

    assert "[공개 전 확정]" in privacy_contact_section
    assert "[공개 전 확정]" in terms_contact_section
    assert "@" not in privacy_contact_section
    assert "@" not in terms_contact_section


def test_device_kit_spec_distinguishes_source_from_production_deployment():
    spec = (ROOT / "docs/DEVICE_KIT_API_SPEC.md").read_text(encoding="utf-8")
    header = "\n".join(spec.splitlines()[:12])

    for marker in ("v2.5.0", "미구현", "미배포", "v2.4.0", "OpenAPI", "소스 구현과 운영 배포는 별개"):
        assert marker in header


def test_readme_points_to_v240_r6_and_labels_future_kit_spec():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "docs/DEPLOYMENT_V2_4.md" in readme
    assert "infra/systemd/hearo-api-v2-r6.service" in readme
    assert "infra/systemd/hearo-mqtt-bridge-v2-r6.service" in readme
    assert "docs/DEVICE_KIT_API_SPEC.md" in readme
    assert "미구현" in readme
