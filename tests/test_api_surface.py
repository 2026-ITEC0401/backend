import dashboard_api_v2

from hearo_backend.main import app as package_app


def test_compatibility_entrypoint_exports_the_package_application():
    assert dashboard_api_v2.app is package_app


def test_openapi_matches_the_final_frontend_contract(api):
    document = api.client.get("/openapi.json").json()
    paths = document["paths"]
    expected = {
        ("/auth/signup", "post"),
        ("/auth/login", "post"),
        ("/auth/refresh", "post"),
        ("/auth/logout", "post"),
        ("/me", "get"),
        ("/me/password", "patch"),
        ("/households/current", "get"),
        ("/households/current/link", "delete"),
        ("/households/link/preview", "post"),
        ("/households/link", "post"),
        ("/households/{household_id}/invite-code", "get"),
        ("/households/{household_id}/invite-code/rotate", "post"),
        ("/households/{household_id}/members", "get"),
        (
            "/households/{household_id}/members/{member_user_id}/display-name",
            "patch",
        ),
        (
            "/households/{household_id}/members/{member_user_id}/display-name",
            "delete",
        ),
        ("/households/{household_id}/emergency-address", "get"),
        ("/households/{household_id}/emergency-address", "patch"),
        ("/households/{household_id}/address-search/roads", "post"),
        ("/households/{household_id}/address-search/details", "post"),
        ("/households/{household_id}/devices", "get"),
        (
            "/households/{household_id}/devices/{device_id}/connection",
            "patch",
        ),
        ("/households/{household_id}/devices/{device_id}/settings", "patch"),
        ("/households/{household_id}/alarms/latest", "get"),
        ("/households/{household_id}/alarms/history", "get"),
        ("/households/{household_id}/alarms/unread-count", "get"),
        ("/households/{household_id}/alarms/seen", "patch"),
        ("/households/{household_id}/alarms/{alarm_id}", "get"),
    }
    for path, method in expected:
        assert path in paths
        assert method in paths[path]

    assert "/auth/password/forgot" not in paths
    assert "/auth/password/reset" not in paths
    assert "/households/{household_id}/members/{user_id}" not in paths
    serialized = str(document).casefold()
    assert "email" not in serialized
    assert "sensitivity" not in serialized
    assert "vibration" not in serialized
