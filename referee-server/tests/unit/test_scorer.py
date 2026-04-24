"""Unit tests for ``scorer.resolve_earliest_winners`` and drift marking.

These are the smallest, fastest tests in the suite. They build synthetic
``VariantSnapshot`` lists with the ``snapshot`` helper from ``conftest.py``
and call the scorer directly — no database, no SSH, no scheduler.
"""
from __future__ import annotations

import unittest
from unittest.mock import Mock

import pytest

from scheduler import RefereeRuntime
from scorer import resolve_earliest_winners

from tests.conftest import snapshot


pytestmark = pytest.mark.unit


class ScoringAndDriftTests(unittest.TestCase):
    def test_resolve_winner_excludes_degraded_and_uses_node_priority_tie_break(self) -> None:
        snapshots = [
            snapshot(node_host="192.168.0.106", king="Team A", king_mtime_epoch=900, status="degraded"),
            snapshot(node_host="192.168.0.102", king="Team A", king_mtime_epoch=1000, status="running"),
            snapshot(node_host="192.168.0.103", king="Team A", king_mtime_epoch=1000, status="running"),
        ]

        winners = resolve_earliest_winners(snapshots)
        self.assertEqual(winners["A"].team_name, "Team A")
        self.assertEqual(winners["A"].node_host, "192.168.0.102")
        self.assertEqual(winners["A"].supporting_nodes, 2)

    def test_resolve_winner_skips_malformed_claims(self) -> None:
        snapshots = [
            snapshot(node_host="192.168.0.102", king="Bad\x01Team", king_mtime_epoch=900),
            snapshot(node_host="192.168.0.103", king="Good Team", king_mtime_epoch=950),
            snapshot(node_host="192.168.0.106", king="Good Team", king_mtime_epoch=960),
        ]

        winners = resolve_earliest_winners(snapshots)
        self.assertEqual(winners["A"].team_name, "Good Team")

    def test_resolve_winner_requires_quorum_for_new_owner(self) -> None:
        snapshots = [
            snapshot(node_host="192.168.0.102", king="Team Alpha", king_mtime_epoch=900),
            snapshot(node_host="192.168.0.103", king="Team Beta", king_mtime_epoch=850),
            snapshot(node_host="192.168.0.106", king="unclaimed", king_mtime_epoch=1000),
        ]

        winners = resolve_earliest_winners(snapshots)
        self.assertEqual(winners, {})

    def test_existing_authoritative_owner_wins_when_it_keeps_quorum(self) -> None:
        snapshots = [
            snapshot(node_host="192.168.0.102", king="Team Alpha", king_mtime_epoch=1000),
            snapshot(node_host="192.168.0.103", king="Team Alpha", king_mtime_epoch=1010),
            snapshot(node_host="192.168.0.106", king="Team Beta", king_mtime_epoch=900),
        ]

        winners = resolve_earliest_winners(
            snapshots,
            current_owners={"A": {"owner_team": "Team Alpha"}},
        )
        self.assertEqual(winners["A"].team_name, "Team Alpha")
        self.assertEqual(winners["A"].supporting_nodes, 2)

    def test_mark_clock_drift_degraded(self) -> None:
        runtime = object.__new__(RefereeRuntime)
        runtime._log_event_and_webhook = Mock()

        snapshots = [
            snapshot(node_host="192.168.0.102", node_epoch=1000),
            snapshot(node_host="192.168.0.103", node_epoch=1001),
            snapshot(node_host="192.168.0.106", node_epoch=1010),
        ]

        degraded = RefereeRuntime._mark_clock_drift_degraded(runtime, series=1, snapshots=snapshots)
        self.assertEqual(degraded, {"192.168.0.106"})
        self.assertEqual(snapshots[2].status, "degraded")
        runtime._log_event_and_webhook.assert_called_once()
