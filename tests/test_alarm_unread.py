from datetime import UTC, datetime, timedelta

from hearo_backend.domain import Alert, iso_utc, parse_timestamp
from hearo_backend.history import history_utc_range

from .conftest import auth_header, create_family, create_owner


def make_alert(
    household_id: str,
    event_id: str,
    timestamp: datetime,
) -> Alert:
    return Alert(
        household_id=household_id,
        event_id=event_id,
        timestamp=iso_utc(timestamp),
        source_device_id="rpi-001",
        location="거실",
        sound="도어락소리",
        raw_label="도어락_입력음",
        type="Visitor",
        confidence=0.8,
    )


def link_member(api, owner: dict, member: dict) -> None:
    household_id = owner["user"]["household_id"]
    invite = api.client.get(
        f"/households/{household_id}/invite-code",
        headers=auth_header(owner),
    )
    assert invite.status_code == 200
    response = api.client.post(
        "/households/link",
        headers=auth_header(member),
        json={"invite_code": invite.json()["invite_code"]},
    )
    assert response.status_code == 200, response.text


def test_unread_count_uses_kst_seven_day_window_and_strict_last_seen(api):
    owner = create_owner(api)
    household_id = owner["user"]["household_id"]
    user_id = owner["user"]["user_id"]
    current = datetime.now(UTC)
    _, _, window_start, _ = history_utc_range(current)
    api.repository.alarm_last_seen[(household_id, user_id)] = iso_utc(
        window_start - timedelta(days=1)
    )

    at_start = make_alert(household_id, "at-window-start", window_start)
    recent = make_alert(household_id, "recent", current - timedelta(seconds=1))
    before_window = make_alert(
        household_id,
        "before-window",
        window_start - timedelta(microseconds=1),
    )
    future = make_alert(household_id, "future", current + timedelta(hours=1))
    for item in (at_start, recent, before_window, future):
        api.repository.put_alert(item)

    response = api.client.get(
        f"/households/{household_id}/alarms/unread-count",
        headers=auth_header(owner),
    )
    assert response.status_code == 200
    assert response.json()["unread_count"] == 2
    assert response.json()["window_days"] == 7

    api.repository.alarm_last_seen[(household_id, user_id)] = recent.timestamp
    strict_response = api.client.get(
        f"/households/{household_id}/alarms/unread-count",
        headers=auth_header(owner),
    )
    assert strict_response.status_code == 200
    assert strict_response.json()["unread_count"] == 0


def test_seen_state_is_independent_for_owner_and_member(api):
    owner = create_owner(api)
    member = create_family(api)
    link_member(api, owner, member)
    household_id = owner["user"]["household_id"]
    owner_id = owner["user"]["user_id"]
    member_id = member["user"]["user_id"]
    current = datetime.now(UTC)
    previous = iso_utc(current - timedelta(hours=1))
    api.repository.alarm_last_seen[(household_id, owner_id)] = previous
    api.repository.alarm_last_seen[(household_id, member_id)] = previous

    for index in range(3):
        api.repository.put_alert(
            make_alert(
                household_id,
                f"shared-{index}",
                current - timedelta(minutes=30 - index),
            )
        )

    owner_count = api.client.get(
        f"/households/{household_id}/alarms/unread-count",
        headers=auth_header(owner),
    )
    member_count = api.client.get(
        f"/households/{household_id}/alarms/unread-count",
        headers=auth_header(member),
    )
    assert owner_count.json()["unread_count"] == 3
    assert member_count.json()["unread_count"] == 3

    seen = api.client.patch(
        f"/households/{household_id}/alarms/seen",
        headers=auth_header(owner),
    )
    assert seen.status_code == 200
    assert seen.json()["unread_count"] == 0
    assert parse_timestamp(seen.json()["last_seen_at"]) >= current

    owner_after = api.client.get(
        f"/households/{household_id}/alarms/unread-count",
        headers=auth_header(owner),
    )
    member_after = api.client.get(
        f"/households/{household_id}/alarms/unread-count",
        headers=auth_header(member),
    )
    assert owner_after.json()["unread_count"] == 0
    assert member_after.json()["unread_count"] == 3


def test_existing_membership_is_lazily_initialized_from_deployment_baseline(api):
    owner = create_owner(api)
    household_id = owner["user"]["household_id"]
    user_id = owner["user"]["user_id"]
    api.repository.alarm_last_seen.pop((household_id, user_id))

    response = api.client.get(
        f"/households/{household_id}/alarms/unread-count",
        headers=auth_header(owner),
    )
    assert response.status_code == 200
    assert response.json() == {
        "unread_count": 0,
        "last_seen_at": api.client.app.state.alarm_unread_baseline_at,
        "window_days": 7,
    }
    assert api.repository.alarm_last_seen[(household_id, user_id)] == (
        api.client.app.state.alarm_unread_baseline_at
    )


def test_unread_endpoints_enforce_household_permissions(api):
    owner = create_owner(api)
    other_owner = create_owner(
        api,
        login_id="owner02",
        phone_number="010-2222-3333",
        name="다른 보호자",
    )
    unlinked = create_family(api)
    household_id = owner["user"]["household_id"]

    assert api.client.get(
        f"/households/{household_id}/alarms/unread-count"
    ).status_code == 401
    assert api.client.get(
        f"/households/{household_id}/alarms/unread-count",
        headers=auth_header(other_owner),
    ).status_code == 403
    assert api.client.patch(
        f"/households/{household_id}/alarms/seen",
        headers=auth_header(unlinked),
    ).status_code == 409


def test_unlink_and_relink_resets_member_seen_state(api):
    owner = create_owner(api)
    member = create_family(api)
    link_member(api, owner, member)
    household_id = owner["user"]["household_id"]
    member_id = member["user"]["user_id"]
    old_value = "2026-01-01T00:00:00Z"
    api.repository.alarm_last_seen[(household_id, member_id)] = old_value

    unlinked = api.client.delete(
        "/households/current/link",
        headers=auth_header(member),
    )
    assert unlinked.status_code == 200
    assert (household_id, member_id) not in api.repository.alarm_last_seen

    link_member(api, owner, member)
    reset_value = api.repository.alarm_last_seen[(household_id, member_id)]
    assert reset_value != old_value
    assert reset_value == api.repository.get_user(member_id).linked_at
