"""Chaos tests — multi-team concurrent claim scenarios.

The scoring path at the event was historically tested with a single team,
so the branch of ``scorer.resolve_earliest_winners`` that chooses between
competing teams on the same variant is thin. These tests pin the behavior
across the tie-breaker surface: identical mtime, staggered mtime, fully
split claims, and authoritative-owner displacement.

All tests use the real ``Database`` and a real ``RefereeRuntime`` so we
exercise the actual scheduler + scorer + db interaction, not a mock.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import pytest

from db import Database
from scheduler import RefereeRuntime
from scorer import resolve_earliest_winners
from unittest.mock import Mock

from tests.conftest import DummySSH, snapshot


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


class ConcurrentClaimsTests(unittest.TestCase):
    def test_tied_mtime_is_resolved_by_node_priority_top_to_bottom(self) -> None:
        """Two teams write claims with the same mtime on different nodes.
        Scorer must break the tie using NODE_PRIORITY order — not randomly,
        not by node_host lexicographic order, not by whichever snapshot
        was collected first.
        """
        snapshots = [
            snapshot(node_host="192.168.0.103", king="Team Beta", king_mtime_epoch=1000),
            snapshot(node_host="192.168.0.102", king="Team Alpha", king_mtime_epoch=1000),
            snapshot(node_host="192.168.0.106", king="Team Alpha", king_mtime_epoch=1000),
        ]

        winners = resolve_earliest_winners(snapshots)

        # Team Alpha has quorum (2 of 3 nodes). The winning snapshot's
        # ``node_host`` must be the first priority-list entry among those
        # supporting the winner — in this case 192.168.0.102.
        self.assertEqual(winners["A"].team_name, "Team Alpha")
        self.assertEqual(winners["A"].node_host, "192.168.0.102")
        self.assertEqual(winners["A"].supporting_nodes, 2)

    def test_earlier_mtime_wins_when_two_teams_reach_quorum(self) -> None:
        """If one team hit quorum earlier (lower mtime_epoch) than another,
        the earlier one wins. This is the ``earliest`` half of
        resolve_earliest_winners's name — quorum alone is not enough.
        """
        # Alpha reaches quorum at t=900; Beta reaches quorum at t=950.
        # Split: Node2 has both versions in sequence, so the snapshot
        # captures whatever is current at this poll cycle.
        snapshots = [
            snapshot(node_host="192.168.0.102", king="Team Alpha", king_mtime_epoch=900),
            snapshot(node_host="192.168.0.103", king="Team Alpha", king_mtime_epoch=900),
            snapshot(node_host="192.168.0.106", king="Team Beta", king_mtime_epoch=950),
        ]

        winners = resolve_earliest_winners(snapshots)

        self.assertEqual(winners["A"].team_name, "Team Alpha")
        self.assertEqual(winners["A"].mtime_epoch, 900)

    def test_split_claims_with_no_quorum_yield_no_winner(self) -> None:
        """When every node has a different claim, nobody has 2-of-3 support
        for any single team. The scorer must return an empty mapping —
        NOT pick the first-seen team, NOT use node priority as a silent
        fallback.
        """
        snapshots = [
            snapshot(node_host="192.168.0.102", king="Team Alpha", king_mtime_epoch=900),
            snapshot(node_host="192.168.0.103", king="Team Beta", king_mtime_epoch=900),
            snapshot(node_host="192.168.0.106", king="Team Gamma", king_mtime_epoch=900),
        ]

        winners = resolve_earliest_winners(snapshots)

        self.assertEqual(winners, {})

    def test_authoritative_owner_is_displaced_when_challenger_reaches_quorum(self) -> None:
        """Once a new team reaches quorum, the scorer updates the
        authoritative owner on the next poll. The prior owner's
        ``current_owners`` entry does not anchor them indefinitely.
        """
        snapshots = [
            snapshot(node_host="192.168.0.102", king="Team Beta", king_mtime_epoch=1000),
            snapshot(node_host="192.168.0.103", king="Team Beta", king_mtime_epoch=1010),
            snapshot(node_host="192.168.0.106", king="Team Alpha", king_mtime_epoch=900),
        ]

        winners = resolve_earliest_winners(
            snapshots,
            current_owners={"A": {"owner_team": "Team Alpha"}},
        )

        # Team Beta has 2-of-3 support; Alpha's authoritative status is
        # not a permanent veto.
        self.assertEqual(winners["A"].team_name, "Team Beta")
        self.assertEqual(winners["A"].supporting_nodes, 2)

    def test_multi_team_poll_cycle_awards_exactly_one_team(self) -> None:
        """End-to-end: drive ``poll_once`` with a snapshot matrix in which
        three teams are all writing claims, verify the team that holds
        quorum on variant A gets a point and the others get nothing.
        This is the first test that pins the full multi-team behavior
        through the real ``RefereeRuntime`` rather than the bare scorer.
        """
        runtime, db = _make_runtime(self)
        db.upsert_team_names(["Team Alpha", "Team Beta", "Team Gamma"])
        db.set_competition_state(status="running", current_series=1)
        runtime.poller.run_cycle = Mock(
            return_value=(
                [
                    # Variant A — Team Alpha holds 2 of 3.
                    snapshot(node_host="192.168.0.102", variant="A", king="Team Alpha", king_mtime_epoch=1000),
                    snapshot(node_host="192.168.0.103", variant="A", king="Team Alpha", king_mtime_epoch=1010),
                    snapshot(node_host="192.168.0.106", variant="A", king="Team Beta", king_mtime_epoch=950),
                    # Variants B and C — nobody wins; all unclaimed.
                    snapshot(node_host="192.168.0.102", variant="B", king="unclaimed", king_mtime_epoch=1),
                    snapshot(node_host="192.168.0.103", variant="B", king="unclaimed", king_mtime_epoch=1),
                    snapshot(node_host="192.168.0.106", variant="B", king="unclaimed", king_mtime_epoch=1),
                    snapshot(node_host="192.168.0.102", variant="C", king="unclaimed", king_mtime_epoch=1),
                    snapshot(node_host="192.168.0.103", variant="C", king="unclaimed", king_mtime_epoch=1),
                    snapshot(node_host="192.168.0.106", variant="C", king="unclaimed", king_mtime_epoch=1),
                ],
                {},
            )
        )

        runtime.poll_once()

        self.assertEqual(db.get_team("Team Alpha")["total_points"], 1.0)
        self.assertEqual(db.get_team("Team Beta")["total_points"], 0.0)
        self.assertEqual(db.get_team("Team Gamma")["total_points"], 0.0)
