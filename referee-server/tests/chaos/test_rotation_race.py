"""Chaos tests — scoring guards across non-running competition states.

``poll_once`` has to be a no-op under anything other than
``status == "running"``. The existing suite covered the ``paused`` case;
these tests exercise the other four: ``stopped``, ``rotating``,
``faulted``, and ``stopping``. Before this file, those branches of the
scheduler were only reached via end-to-end lifecycle tests that also
drove deploys, which made it hard to isolate "did scoring happen?" from
"did the deploy happen?".
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import pytest

from db import Database
from scheduler import RefereeRuntime

from tests.conftest import DummySSH, healthy_matrix, snapshot


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


def _winning_snapshot_matrix(variant: str = "A") -> list:
    """Build a 9-snapshot matrix where Team Alpha wins variant ``variant``
    outright (2-of-3 quorum) and every other variant is unclaimed. Used
    to probe whether the state-gate blocks scoring — if the gate leaks,
    Team Alpha would get a point we can assert against.
    """
    matrix = healthy_matrix(king="unclaimed")
    for snap in matrix:
        if snap.variant == variant and snap.node_host in {"192.168.0.102", "192.168.0.103"}:
            # Replace with a clean Alpha-owned snapshot; the raw VariantSnapshot
            # from healthy_matrix is frozen, so we swap in a fresh one.
            idx = matrix.index(snap)
            matrix[idx] = snapshot(
                node_host=snap.node_host,
                variant=variant,
                king="Team Alpha",
                king_mtime_epoch=1000,
            )
    return matrix


class RotationRaceTests(unittest.TestCase):
    def test_stopped_status_blocks_scoring_even_with_winning_matrix(self) -> None:
        runtime, db = _make_runtime(self)
        db.upsert_team_names(["Team Alpha"])
        db.set_competition_state(status="stopped", current_series=1)
        runtime.poller.run_cycle = Mock(return_value=(_winning_snapshot_matrix(), {}))

        runtime.poll_once()

        self.assertEqual(db.get_team("Team Alpha")["total_points"], 0.0)
        self.assertEqual(db.get_competition()["poll_cycle"], 0)

    def test_rotating_status_blocks_scoring(self) -> None:
        """When the scheduler is mid-rotation, a poll must not award points
        from the pre-rotation series — which may still have winning
        snapshots cached on disk. This prevents a rotation race from
        double-scoring a team.
        """
        runtime, db = _make_runtime(self)
        db.upsert_team_names(["Team Alpha"])
        db.set_competition_state(status="rotating", current_series=1)
        runtime.poller.run_cycle = Mock(return_value=(_winning_snapshot_matrix(), {}))

        runtime.poll_once()

        self.assertEqual(db.get_team("Team Alpha")["total_points"], 0.0)

    def test_faulted_status_blocks_scoring(self) -> None:
        runtime, db = _make_runtime(self)
        db.upsert_team_names(["Team Alpha"])
        db.set_competition_state(status="faulted", current_series=1, fault_reason="test")
        runtime.poller.run_cycle = Mock(return_value=(_winning_snapshot_matrix(), {}))

        runtime.poll_once()

        self.assertEqual(db.get_team("Team Alpha")["total_points"], 0.0)

    def test_stopping_status_blocks_scoring(self) -> None:
        runtime, db = _make_runtime(self)
        db.upsert_team_names(["Team Alpha"])
        db.set_competition_state(status="stopping", current_series=1)
        runtime.poller.run_cycle = Mock(return_value=(_winning_snapshot_matrix(), {}))

        runtime.poll_once()

        self.assertEqual(db.get_team("Team Alpha")["total_points"], 0.0)

    def test_repeated_polls_in_blocked_state_do_not_advance_poll_cycle(self) -> None:
        """Every non-running state should also leave the poll_cycle counter
        alone. Otherwise operators counting polls-since-resume would get
        inflated numbers and think the scorer was running when it was
        not.
        """
        runtime, db = _make_runtime(self)
        db.upsert_team_names(["Team Alpha"])
        db.set_competition_state(status="paused", current_series=1)
        runtime.poller.run_cycle = Mock(return_value=(_winning_snapshot_matrix(), {}))

        for _ in range(3):
            runtime.poll_once()

        self.assertEqual(db.get_competition()["poll_cycle"], 0)
        self.assertEqual(db.get_team("Team Alpha")["total_points"], 0.0)
