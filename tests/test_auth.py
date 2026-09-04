"""The operator gate, and the locks that depend on it."""

from pathlib import Path

import pytest

from app.settings import (
    AdminSettings,
    AnalyticsSettings,
    AppSettings,
    CalendarSettings,
    ConsoleSettings,
    DataSettings,
    PostgresSettings,
    ProjectionSettings,
    Settings,
    SnapshotSettings,
    TargetSettings,
)
from app.views import auth
from app.views.database import _writes_permitted


@pytest.fixture
def gated(monkeypatch) -> AdminSettings:
    monkeypatch.setenv("TEST_OPERATOR_PASSWORD", "s3cret")
    return AdminSettings(password_env="TEST_OPERATOR_PASSWORD")


def test_no_password_env_means_no_gate():
    settings = AdminSettings(password_env=None)
    assert auth.is_configured(settings) is False
    assert auth.is_unlocked(settings) is True
    assert auth.lock_notice(settings) is not None


def test_an_unset_variable_means_no_gate(monkeypatch):
    """A named variable that nobody set must not be treated as a password."""
    monkeypatch.delenv("TEST_OPERATOR_PASSWORD", raising=False)
    settings = AdminSettings(password_env="TEST_OPERATOR_PASSWORD")
    assert auth.is_configured(settings) is False
    assert settings.required_password() is None


def test_a_set_variable_configures_the_gate(gated):
    assert auth.is_configured(gated) is True
    assert gated.required_password() == "s3cret"
    assert auth.lock_notice(gated) is None


def test_a_blank_password_does_not_count(monkeypatch):
    monkeypatch.setenv("TEST_OPERATOR_PASSWORD", "   ")
    settings = AdminSettings(password_env="TEST_OPERATOR_PASSWORD")
    assert settings.required_password() is None


def test_the_lock_notice_names_the_setting():
    notice = auth.lock_notice(AdminSettings(password_env=None))
    assert "admin.password_env" in notice


# --------------------------------------------------------------------------- #
# The rule the gate exists to enforce
# --------------------------------------------------------------------------- #


def build_settings(admin: AdminSettings, console: ConsoleSettings) -> Settings:
    """A Settings object carrying just the two things the rule looks at."""
    return Settings(
        app=AppSettings(),
        data=DataSettings(postgres=PostgresSettings(console=console)),
        calendar=CalendarSettings(),
        projection=ProjectionSettings(),
        analytics=AnalyticsSettings(),
        snapshots=SnapshotSettings(),
        targets=TargetSettings(),
        admin=admin,
        source_file=Path("config.yaml"),
    )


def test_writes_need_the_config_flag(gated):
    off, _ = _writes_permitted(build_settings(gated, ConsoleSettings()))
    on, _ = _writes_permitted(
        build_settings(gated, ConsoleSettings(allow_writes=True))
    )
    assert off is False
    assert on is True


def test_writes_are_refused_without_an_operator_password():
    """The whole point: allow_writes on an ungated page is not honoured."""
    settings = build_settings(
        AdminSettings(password_env=None), ConsoleSettings(allow_writes=True)
    )
    allowed, reason = _writes_permitted(settings)
    assert allowed is False
    assert reason and "admin.password_env" in reason


def test_no_reason_is_given_when_writes_are_simply_off(gated):
    """"Off on purpose" and "blocked" must read differently in the UI."""
    allowed, reason = _writes_permitted(build_settings(gated, ConsoleSettings()))
    assert allowed is False
    assert reason is None
