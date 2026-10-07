"""Static checks for retention infrastructure templates.

CloudFormation uses intrinsic tags such as ``!Ref`` and ``!GetAtt``. These
tests intentionally inspect the small, named resource sections as text rather
than pretending that a generic YAML loader validates CloudFormation semantics.
"""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def without_comments(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def assert_directive(text: str, name: str, value: str) -> None:
    if value:
        pattern = rf"(?m)^\s*{re.escape(name)}\s+{re.escape(value)}\s*$"
    else:
        pattern = rf"(?m)^\s*{re.escape(name)}\s*$"
    assert re.search(pattern, text)


def test_alerts_table_enables_the_existing_expiry_attribute_without_runtime_admin():
    template = read("infra/aws-v2.yaml")
    alerts = template[
        template.index("  AlertsTable:") : template.index("  HearoEc2Role:")
    ]

    assert "TimeToLiveSpecification:" in alerts
    assert re.search(
        r"TimeToLiveSpecification:\s+AttributeName: expires_at_epoch"
        r"\s+Enabled: true",
        alerts,
    )
    assert template.count("TimeToLiveSpecification:") == 2
    assert template.count("AttributeName: expires_at_epoch") == 2

    # The application role writes expiry attributes, but table-level TTL and
    # CloudFormation administration remain deployment/operator privileges.
    runtime_role = without_comments(
        template[
            template.index("  HearoEc2Role:") : template.index(
                "  HearoInstanceProfile:"
            )
        ]
    )
    assert "dynamodb:UpdateTimeToLive" not in runtime_role
    assert not re.search(r"(?mi)^\s*-\s*cloudformation:", runtime_role)


def test_legal_policy_environment_defaults_are_fail_closed_until_launch():
    lines = without_comments(read("infra/hearo-api.env.example")).splitlines()
    values = {
        key: value
        for line in lines
        if "=" in line
        for key, value in [line.split("=", 1)]
    }

    assert values["HEARO_TERMS_VERSION"] == ""
    assert values["HEARO_PRIVACY_VERSION"] == ""
    assert values["HEARO_LEGAL_EFFECTIVE_AT"] == ""
    assert values["HEARO_TERMS_URL"] == ""
    assert values["HEARO_PRIVACY_URL"] == ""
    assert values["HEARO_LEGAL_CONSENT_REQUIRED"] == "false"


def test_nginx_rotation_keeps_90_daily_archives_and_standard_hooks():
    config = without_comments(read("infra/logrotate/hearo-nginx"))

    assert "/var/log/nginx/*.log" in config
    assert_directive(config, "daily", "")
    assert_directive(config, "rotate", "90")
    assert_directive(config, "maxage", "90")
    assert_directive(config, "compress", "")
    assert_directive(config, "delaycompress", "")
    assert "run-parts /etc/logrotate.d/httpd-prerotate" in config
    assert "invoke-rc.d nginx rotate" in config


def test_rsyslog_rotation_excludes_authentication_logs():
    raw = read("infra/logrotate/hearo-rsyslog-service")
    config = without_comments(raw)

    for path in (
        "/var/log/syslog",
        "/var/log/mail.log",
        "/var/log/kern.log",
        "/var/log/user.log",
        "/var/log/cron.log",
    ):
        assert path in config
    assert "/var/log/auth.log" not in config
    assert "authpriv" not in config.casefold()
    assert_directive(config, "daily", "")
    assert_directive(config, "rotate", "90")
    assert_directive(config, "maxage", "90")
    assert_directive(config, "compress", "")
    assert "/usr/lib/rsyslog/rsyslog-rotate" in config
    assert "at least one" in raw.casefold()
    assert "calendar year" in raw.casefold()


def test_journald_90_day_template_has_an_explicit_audit_separation_gate():
    raw = read("infra/journald/90-hearo-service-retention.conf")
    config = without_comments(raw)

    assert "DO NOT INSTALL" in raw
    assert "system-wide" in raw
    assert "at least one calendar year" in raw
    assert re.search(r"(?m)^MaxRetentionSec=90day$", config)
    assert re.search(r"(?m)^MaxFileSec=1day$", config)


def test_retention_target_environment_requires_independent_confirmation():
    values = {
        key: value
        for line in without_comments(
            read("infra/hearo-alert-retention.env.example")
        ).splitlines()
        if "=" in line
        for key, value in [line.split("=", 1)]
    }
    expected = {
        "HEARO_RETENTION_REGION",
        "HEARO_RETENTION_ALERTS_TABLE",
        "HEARO_RETENTION_CORE_TABLE",
        "HEARO_RETENTION_CONFIRM_REGION",
        "HEARO_RETENTION_CONFIRM_ALERTS_TABLE",
        "HEARO_RETENTION_CONFIRM_CORE_TABLE",
        "HEARO_RETENTION_CONFIRM_ALERTS_TABLE_ARN",
        "HEARO_RETENTION_CONFIRM_CORE_TABLE_ARN",
    }

    assert set(values) == expected
    assert all(value == "" for value in values.values())


def test_scheduled_retention_unit_is_narrow_guarded_and_targets_r6():
    raw = read("infra/systemd/hearo-alert-retention.service")
    active = without_comments(raw)
    exec_start = next(
        line for line in active.splitlines() if line.startswith("ExecStart=")
    )

    assert "DEPLOYMENT GATE" in raw
    assert "WorkingDirectory=/opt/hearo-backend-v2-r6" in active
    assert (
        "EnvironmentFile=/etc/hearo/hearo-alert-retention.env" in active
    )
    assert "--mode purge" in exec_start
    assert "--scheduled" in exec_start
    assert "--apply" in exec_start
    assert "--mode migrate" not in exec_start
    assert "--plan-file" not in exec_start
    for option in (
        "--region",
        "--alerts-table",
        "--core-table",
        "--confirm-region",
        "--confirm-alerts-table",
        "--confirm-core-table",
        "--confirm-alerts-table-arn",
        "--confirm-core-table-arn",
    ):
        assert option in exec_start
    assert active.count("ExecStartPre=/usr/bin/test ") == 8
    assert "NoNewPrivileges=true" in active
    assert "ProtectSystem=strict" in active
    assert "ProtectHome=true" in active


def test_retention_timer_is_hourly_but_explicitly_deployment_gated():
    raw = read("infra/systemd/hearo-alert-retention.timer")
    active = without_comments(raw)

    assert "DEPLOYMENT GATE" in raw
    assert "OnCalendar=hourly" in active
    assert "Persistent=true" in active
    assert "Unit=hearo-alert-retention.service" in active
    assert "WantedBy=timers.target" in active
