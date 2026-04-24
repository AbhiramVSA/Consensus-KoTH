"""Chaos tests — SSH and compose failure patterns during deploy.

Covers scenarios from docs/TESTING_AUDIT.md §6 that were previously only
tested through the full start_competition path:

* compose up succeeds on 2-of-3 nodes, fails on 1-of-3
* compose up fails on all 3 nodes and rollback also fails
* post-deploy snapshot matrix shows one "failed" variant; deploy retries
  and eventually succeeds

These tests drive ``_deploy_series_or_raise`` and ``rotate_to_series``
directly with scripted per-node compose responses via
``Mock(side_effect=...)``. The production scheduler treats any non-empty
error map from ``_run_compose_parallel`` as a failed deploy and either
rolls back or faults, depending on where in the FSM the failure landed.
"""
from __future__ import annotations

import itertools
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from config import SETTINGS
from db import Database
from scheduler import RefereeRuntime, RuntimeGuardError

from tests.conftest import DummySSH, healthy_matrix


pytestmark = [pytest.mark.chaos, pytest.mark.integration]


def _make_runtime(tc: unittest.TestCase) -> tuple[RefereeRuntime, Database]:
    fd, raw_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db_path = Path(raw_path)
    db = Database(db_path)
    db.initialize()
    tc.addCleanup(lambda: db_path.exists() and db_path.unlink())
    tc.addCleanup(db.close)
    return RefereeRuntime(db, DummySSH()), db


def _shrink_deploy_timeouts(tc: unittest.TestCase) -> None:
    """Reduce ``deploy_health_timeout_seconds`` to 1 so retry loops exit
    quickly. Restore on teardown so the next test sees the real default.
    """
    original = SETTINGS.deploy_health_timeout_seconds
    object.__setattr__(SETTINGS, "deploy_health_timeout_seconds", 1)
    tc.addCleanup(lambda: object.__setattr__(SETTINGS, "deploy_health_timeout_seconds", original))


class SshPartialFailureTests(unittest.TestCase):
    def test_single_node_compose_failure_causes_deploy_rollback(self) -> None:
        """If compose up fails on one of three nodes, the deploy is a failure
        and the rollback (compose down -v) must fire on all nodes. This
        is the partial-failure scenario flagged in AUDIT §6 row 1: a
        half-deployed series is worse than a failed deploy.
        """
        runtime, db = _make_runtime(self)
        _shrink_deploy_timeouts(self)
        db.upsert_team_names(["Team Alpha"])

        compose_calls: list[tuple[int, str]] = []

        def compose(series: int, command: str) -> dict[str, tuple[bool, str]]:
            compose_calls.append((series, command))
            if "up -d" in command:
                return {
                    "192.168.0.102": (True, "ok"),
                    "192.168.0.103": (True, "ok"),
                    "192.168.0.106": (False, "node down"),
                }
            if "down -v" in command:
                return {
                    "192.168.0.102": (True, "rolled back"),
                    "192.168.0.103": (True, "rolled back"),
                    "192.168.0.106": (True, "rolled back"),
                }
            return {}

        runtime._run_compose_parallel = Mock(side_effect=compose)
        runtime.poller.run_cycle = Mock(return_value=([], {}))

        with patch("scheduler.fire_and_forget", lambda payload: None), patch(
            "scheduler.time.sleep", return_value=None
        ), patch("scheduler.time.monotonic", side_effect=[0, 0, 2, 2]):
            with self.assertRaises(RuntimeGuardError):
                runtime.start_competition()

        # The runtime must have called ``down -v`` to clean up; otherwise
        # the failed node's partial containers would linger into the next
        # deploy attempt.
        self.assertTrue(any("down -v" in cmd for _, cmd in compose_calls))
        self.assertEqual(db.get_competition()["status"], "stopped")

    def test_all_node_compose_failure_and_rollback_failure_leaves_faulted(self) -> None:
        """If both compose up AND the rollback fail, the scheduler must
        leave the FSM in ``faulted`` with a fault_reason. It must NOT
        silently reset to "stopped" — that would mislead the operator
        into thinking the range is clean.
        """
        runtime, db = _make_runtime(self)
        _shrink_deploy_timeouts(self)
        db.upsert_team_names(["Team Alpha"])
        db.set_competition_state(status="running", current_series=1)
        runtime.poll_once = Mock()
        runtime._run_compose_parallel = Mock(
            side_effect=[
                {},  # pre-deploy compose down (OK)
                {  # first up -d — all three nodes fail
                    "192.168.0.102": (False, "boom"),
                    "192.168.0.103": (False, "boom"),
                    "192.168.0.106": (False, "boom"),
                },
                {},  # rollback down -v (OK)
                {  # rollback up -d (also fails)
                    "192.168.0.102": (False, "still broken"),
                    "192.168.0.103": (False, "still broken"),
                    "192.168.0.106": (False, "still broken"),
                },
                {},  # final cleanup
            ]
        )
        runtime.poller.run_cycle = Mock(side_effect=itertools.repeat(([], {})))

        with patch("scheduler.fire_and_forget", lambda payload: None), patch(
            "scheduler.time.sleep", return_value=None
        ), patch("scheduler.time.monotonic", side_effect=[0, 2, 0, 2]):
            with self.assertRaises(RuntimeGuardError):
                runtime.rotate_to_series(2)

        state = db.get_competition()
        self.assertEqual(state["status"], "faulted")
        self.assertEqual(state["current_series"], 1)
        self.assertTrue(state["fault_reason"])

    def test_deploy_health_recovers_after_one_flaky_poll_cycle(self) -> None:
        """This pins the happy-middle: one initial snapshot comes back with
        a ``failed`` status, the deploy retries, the next poll shows a
        clean matrix, deploy succeeds. Without this test, a refactor that
        treats the first failed snapshot as terminal would pass CI.
        """
        runtime, db = _make_runtime(self)
        _shrink_deploy_timeouts(self)

        runtime._run_compose_parallel = Mock(return_value={})

        # First snapshot cycle has two "failed" variants on different nodes.
        bad_matrix = healthy_matrix(king="unclaimed")
        from dataclasses import replace

        bad_matrix[0] = replace(bad_matrix[0], status="failed", king=None, king_mtime_epoch=None)
        bad_matrix[3] = replace(bad_matrix[3], status="failed", king=None, king_mtime_epoch=None)

        good_matrix = healthy_matrix(king="unclaimed")

        runtime.poller.run_cycle = Mock(
            side_effect=[(bad_matrix, {}), (good_matrix, {}), (good_matrix, {})]
        )

        with patch("scheduler.fire_and_forget", lambda payload: None), patch(
            "scheduler.time.sleep", return_value=None
        ), patch("scheduler.time.monotonic", side_effect=[0, 0]):
            result = runtime._deploy_series_or_raise(series=3)

        self.assertEqual(len(result), 9)
        # Must have polled at least twice: once to see the bad state,
        # again after the retry.
        self.assertGreaterEqual(runtime.poller.run_cycle.call_count, 2)
