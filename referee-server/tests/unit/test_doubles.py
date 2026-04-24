"""Unit tests for the recording test doubles in ``tests/conftest.py``.

The doubles are test infrastructure; pinning their behavior directly
means a regression in a helper (e.g. ``assert_command_to`` stops raising
on a miss) surfaces as a red test here rather than silently turning other
tests into no-ops.
"""
from __future__ import annotations

import pytest

from tests.conftest import DummyScheduler, DummySSH


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# DummySSH
# ---------------------------------------------------------------------------
def test_dummy_ssh_records_every_command() -> None:
    ssh = DummySSH()
    ssh.exec("host-a", "uptime")
    ssh.exec("host-b", "whoami")

    assert ssh.commands == [("host-a", "uptime"), ("host-b", "whoami")]
    assert ssh.commands_to("host-a") == ["uptime"]
    assert ssh.last_command_to("host-b") == "whoami"


def test_dummy_ssh_default_response_applies_when_no_override() -> None:
    ssh = DummySSH(response=(42, "greetings", "nope"))

    code, out, err = ssh.exec("host-a", "whoami")

    assert (code, out, err) == (42, "greetings", "nope")


def test_dummy_ssh_reply_on_overrides_for_a_single_host() -> None:
    ssh = DummySSH(response=(0, "default", ""))
    ssh.reply_on("host-b", (1, "scripted", "boom"))

    assert ssh.exec("host-a", "uptime") == (0, "default", "")
    assert ssh.exec("host-b", "uptime") == (1, "scripted", "boom")


def test_dummy_ssh_assert_no_commands_raises_when_any_sent() -> None:
    ssh = DummySSH()
    ssh.assert_no_commands()
    ssh.exec("host-a", "hostname")
    with pytest.raises(AssertionError, match="expected zero SSH commands"):
        ssh.assert_no_commands()


def test_dummy_ssh_assert_command_count_checks_exact() -> None:
    ssh = DummySSH()
    ssh.exec("host-a", "one")
    ssh.exec("host-a", "two")

    ssh.assert_command_count(2)
    with pytest.raises(AssertionError, match="expected 3 SSH commands, got 2"):
        ssh.assert_command_count(3)


def test_dummy_ssh_assert_command_to_matches_and_returns_command() -> None:
    ssh = DummySSH()
    ssh.exec("host-a", "echo hello")
    ssh.exec("host-a", "echo world")

    latest = ssh.assert_command_to("host-a")
    assert latest == "echo world"

    match = ssh.assert_command_to("host-a", contains="hello")
    assert match == "echo hello"


def test_dummy_ssh_assert_command_to_raises_when_host_absent() -> None:
    ssh = DummySSH()
    ssh.exec("host-a", "one")

    with pytest.raises(AssertionError, match="no SSH commands went to host-b"):
        ssh.assert_command_to("host-b")


def test_dummy_ssh_assert_command_to_raises_when_contains_missing() -> None:
    ssh = DummySSH()
    ssh.exec("host-a", "echo hello")

    with pytest.raises(AssertionError, match="no command to host-a contained 'missing'"):
        ssh.assert_command_to("host-a", contains="missing")


def test_dummy_ssh_assert_command_contains_returns_host_and_command() -> None:
    ssh = DummySSH()
    ssh.exec("host-a", "ls /")
    ssh.exec("host-b", "systemctl status app")

    host, cmd = ssh.assert_command_contains("systemctl")
    assert host == "host-b"
    assert cmd == "systemctl status app"


def test_dummy_ssh_last_command_to_raises_when_empty() -> None:
    ssh = DummySSH()
    with pytest.raises(AssertionError, match="no commands were sent"):
        ssh.last_command_to("host-z")


# ---------------------------------------------------------------------------
# DummyScheduler
# ---------------------------------------------------------------------------
def test_dummy_scheduler_add_and_lookup() -> None:
    scheduler = DummyScheduler()
    scheduler.add_job(lambda: None, "interval", id="poll", seconds=30)

    assert scheduler.job_ids() == {"poll"}
    assert scheduler.get_job("poll") == {"trigger": "interval", "seconds": 30}


def test_dummy_scheduler_assert_job_scheduled_returns_payload() -> None:
    scheduler = DummyScheduler()
    scheduler.add_job(lambda: None, "date", id="rotate", run_date="2026-04-24T00:00:00")

    job = scheduler.assert_job_scheduled("rotate", trigger="date")
    assert job["run_date"] == "2026-04-24T00:00:00"


def test_dummy_scheduler_assert_job_scheduled_trigger_mismatch() -> None:
    scheduler = DummyScheduler()
    scheduler.add_job(lambda: None, "interval", id="poll", seconds=30)

    with pytest.raises(AssertionError, match="trigger is 'interval', expected 'date'"):
        scheduler.assert_job_scheduled("poll", trigger="date")


def test_dummy_scheduler_assert_job_not_scheduled() -> None:
    scheduler = DummyScheduler()
    scheduler.assert_job_not_scheduled("rotate")

    scheduler.add_job(lambda: None, "date", id="rotate")
    with pytest.raises(AssertionError, match="should not be scheduled"):
        scheduler.assert_job_not_scheduled("rotate")


def test_dummy_scheduler_assert_job_count_and_shutdown_clears() -> None:
    scheduler = DummyScheduler()
    scheduler.add_job(lambda: None, "interval", id="poll", seconds=30)
    scheduler.add_job(lambda: None, "date", id="rotate")

    scheduler.assert_job_count(2)
    scheduler.shutdown()
    scheduler.assert_job_count(0)


def test_dummy_scheduler_remove_job_is_idempotent() -> None:
    scheduler = DummyScheduler()
    scheduler.add_job(lambda: None, "interval", id="poll", seconds=30)

    scheduler.remove_job("poll")
    scheduler.remove_job("poll")  # second call is a no-op, not a KeyError
    scheduler.assert_job_not_scheduled("poll")
