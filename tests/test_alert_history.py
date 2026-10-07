from datetime import UTC, datetime, timedelta

from hearo_backend.domain import Alert, iso_utc
from hearo_backend.history import group_history, history_utc_range

from .conftest import auth_header, create_family, create_owner


def alert(household_id: str, event_id: str, timestamp: datetime) -> Alert:
    return Alert(
        household_id=household_id,
        event_id=event_id,
        timestamp=iso_utc(timestamp),
        source_device_id="rpi-001",
        location="거실",
        sound="도어락소리",
        raw_label="도어락_개방음",
        type="Visitor",
        confidence=0.91,
    )


def test_history_has_seven_days_empty_days_and_exact_range(api):
    owner = create_owner(api)
    household_id = owner["user"]["household_id"]
    _, _, start, end_exclusive = history_utc_range()
    included = [
        alert(household_id, "at-start", start),
        alert(household_id, "same-time-a", end_exclusive - timedelta(seconds=1)),
        alert(household_id, "same-time-b", end_exclusive - timedelta(seconds=1)),
    ]
    excluded = [
        alert(household_id, "before-start", start - timedelta(microseconds=1)),
        alert(household_id, "at-end", end_exclusive),
    ]
    for item in included + excluded:
        api.repository.put_alert(item)
    api.repository.put_alert(
        alert("another-household", "not-this-household", end_exclusive - timedelta(minutes=1))
    )

    response = api.client.get(
        f"/households/{household_id}/alarms/history", headers=auth_header(owner)
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["timezone"] == "Asia/Seoul"
    assert len(payload["days"]) == 7
    assert payload["days"] == sorted(payload["days"], key=lambda day: day["date"], reverse=True)
    assert payload["total_count"] == 3
    ids = [alarm["id"] for day in payload["days"] for alarm in day["alarms"]]
    assert set(ids) == {"at-start", "same-time-a", "same-time-b"}
    assert any(not day["alarms"] for day in payload["days"])
    for day in payload["days"]:
        times = [item["time"] for item in day["alarms"]]
        assert times == sorted(times, reverse=True)


def test_kst_utc_1500_boundary_groups_into_correct_dates():
    household = "home-boundary"
    before = alert(household, "before", datetime(2026, 8, 12, 14, 59, 59, tzinfo=UTC))
    after = alert(household, "after", datetime(2026, 8, 12, 15, 0, 0, tzinfo=UTC))
    result = group_history(
        [before, after], now=datetime(2026, 8, 13, 3, 0, tzinfo=UTC)
    )
    grouped = {
        day["date"]: [item["id"] for item in day["alarms"]]
        for day in result["days"]
    }
    assert grouped["2026-08-12"] == ["before"]
    assert grouped["2026-08-13"] == ["after"]


def test_internal_mqtt_alert_is_idempotent_and_appears_in_latest(api):
    owner = create_owner(api)
    household_id = owner["user"]["household_id"]
    payload = {
        "household_id": household_id,
        "event_id": "alert-001",
        "timestamp": iso_utc(),
        "source_device_id": "rpi-001",
        "location": "거실",
        "sound": "도어락소리",
        "raw_label": "도어락_개방음",
        "type": "Visitor",
        "confidence": 0.91,
    }
    internal_headers = {"X-Internal-Token": api.settings.internal_token}
    broadcasts = []
    original_broadcast = api.client.app.state.realtime.broadcast

    async def recording_broadcast(household, event):
        broadcasts.append((household, event))
        await original_broadcast(household, event)

    api.client.app.state.realtime.broadcast = recording_broadcast
    assert api.client.post(
        "/internal/mqtt/alert", headers=internal_headers, json=payload
    ).status_code == 200
    assert api.client.post(
        "/internal/mqtt/alert", headers=internal_headers, json=payload
    ).status_code == 200

    latest = api.client.get(
        f"/households/{household_id}/alarms/latest", headers=auth_header(owner)
    )
    assert latest.json()["alarm"]["id"] == "alert-001"
    assert latest.json()["alarm"]["time"].endswith("Z")
    history = api.client.get(
        f"/households/{household_id}/alarms/history", headers=auth_header(owner)
    )
    assert history.json()["total_count"] == 1
    alarm_events = [event for _, event in broadcasts if event["type"] == "alarm.created"]
    assert len(alarm_events) == 1
    assert alarm_events[0]["alarm"]["raw_label"] == "도어락_개방음"
    assert alarm_events[0]["alarm"]["type"] == "Visitor"


def test_alarm_detail_is_household_scoped_and_returns_kst_fields(api, monkeypatch):
    owner = create_owner(api)
    household_id = owner["user"]["household_id"]
    occurred_at = datetime(2026, 8, 20, 23, 47, tzinfo=UTC)
    # The fixed timezone example must not expire merely because CI runs later.
    monkeypatch.setattr("hearo_backend.retention.utc_now", lambda: occurred_at + timedelta(days=1))
    api.repository.put_alert(alert(household_id, "detail-alert-001", occurred_at))

    response = api.client.get(
        f"/households/{household_id}/alarms/detail-alert-001",
        headers=auth_header(owner),
    )
    assert response.status_code == 200
    alarm_value = response.json()["alarm"]
    assert alarm_value["id"] == "detail-alert-001"
    assert alarm_value["date"] == "2026-08-21"
    assert alarm_value["local_time"] == "2026-08-21T08:47:00+09:00"
    assert alarm_value["raw_label"] == "도어락_개방음"
    assert alarm_value["type"] == "Visitor"
    assert set(alarm_value) == {
        "id",
        "date",
        "local_time",
        "location",
        "sound",
        "raw_label",
        "type",
    }

    invite = api.client.get(
        f"/households/{household_id}/invite-code", headers=auth_header(owner)
    ).json()
    member = create_family(api)
    assert api.client.post(
        "/households/link",
        headers=auth_header(member),
        json={"invite_code": invite["invite_code"]},
    ).status_code == 200
    member_read = api.client.get(
        f"/households/{household_id}/alarms/detail-alert-001",
        headers=auth_header(member),
    )
    assert member_read.status_code == 200

    missing = api.client.get(
        f"/households/{household_id}/alarms/not-found",
        headers=auth_header(owner),
    )
    assert missing.status_code == 404

    outsider = create_owner(
        api,
        login_id="alarmout01",
        phone_number="010-5555-4444",
        name="외부인",
    )
    blocked = api.client.get(
        f"/households/{household_id}/alarms/detail-alert-001",
        headers=auth_header(outsider),
    )
    assert blocked.status_code == 403


def test_alarm_detail_keeps_raw_label_key_when_legacy_value_is_missing(api):
    owner = create_owner(api)
    household_id = owner["user"]["household_id"]
    legacy_alert = alert(household_id, "legacy-alert-001", datetime.now(UTC))
    legacy_alert.raw_label = None
    api.repository.put_alert(legacy_alert)

    response = api.client.get(
        f"/households/{household_id}/alarms/legacy-alert-001",
        headers=auth_header(owner),
    )

    assert response.status_code == 200
    assert "raw_label" in response.json()["alarm"]
    assert response.json()["alarm"]["raw_label"] is None


def test_hub_publisher_can_store_remote_capture_location_and_hybrid_diagnostics(api):
    owner = create_owner(api)
    household_id = owner["user"]["household_id"]
    payload = {
        "household_id": household_id,
        "event_id": "remote-alert-001",
        "timestamp": iso_utc(),
        "source_device_id": "rpi-001",
        "publisher_device_id": "rpi-001",
        "capture_device_id": "esp32_2",
        "location": "클라이언트 값은 신뢰하지 않음",
        "sound": "비상벨소리",
        "raw_label": "yamnet_critical_siren",
        "type": "Urgent",
        "confidence": 0.93,
        "model_version": "hearo_classifier_v2",
        "decision_source": "yamnet_safety_override",
        "confidence_kind": "yamnet_family_score",
        "yamnet_family": "critical_siren",
        "yamnet_score": 0.93,
        "hearo_confidence": 0.12,
        "applied_threshold": 0.8,
        "policy_version": "hearo-hybrid-v3-test",
    }
    response = api.client.post(
        "/internal/mqtt/alert",
        headers={"X-Internal-Token": api.settings.internal_token},
        json=payload,
    )
    assert response.status_code == 200, response.text
    alarm_value = response.json()["alarm"]
    assert set(alarm_value) == {
        "id",
        "sound",
        "raw_label",
        "type",
        "location",
        "time",
    }
    assert alarm_value["location"] == "현관"
    assert alarm_value["raw_label"] == "yamnet_critical_siren"
    assert alarm_value["type"] == "Urgent"
    stored = api.repository.get_alert(household_id, "remote-alert-001")
    assert stored.publisher_device_id == "rpi-001"
    assert stored.source_device_id == "esp32_2"
    assert stored.model_version == "hearo_classifier_v2"
    assert stored.decision_source == "yamnet_safety_override"
    assert stored.yamnet_family == "critical_siren"


def test_empty_history_still_returns_all_seven_dates(api):
    owner = create_owner(api)
    household_id = owner["user"]["household_id"]
    response = api.client.get(
        f"/households/{household_id}/alarms/history", headers=auth_header(owner)
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["total_count"] == 0
    assert len(payload["days"]) == 7
    assert all(day["alarms"] == [] for day in payload["days"])


def test_alert_older_than_history_window_is_hidden_but_detail_remains_available(api):
    owner = create_owner(api)
    household_id = owner["user"]["household_id"]
    _, _, start, _ = history_utc_range()
    old_alert = alert(household_id, "older-detail", start - timedelta(seconds=1))
    api.repository.put_alert(old_alert)

    history = api.client.get(
        f"/households/{household_id}/alarms/history", headers=auth_header(owner)
    )
    assert history.status_code == 200
    assert history.json()["total_count"] == 0

    detail = api.client.get(
        f"/households/{household_id}/alarms/older-detail", headers=auth_header(owner)
    )
    assert detail.status_code == 200
    assert detail.json()["alarm"]["id"] == "older-detail"
