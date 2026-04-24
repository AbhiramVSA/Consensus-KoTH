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


# ---------------------------------------------------------------------------
# Edge cases for resolve_earliest_winners.
#
# Each test pins one corner of the decision tree in scorer.py so a refactor
# that inverts a condition, reorders the sort key, or short-circuits a
# filter lands on a failure instead of a silent semantic drift.
# ---------------------------------------------------------------------------
class ScoringEdgeCases(unittest.TestCase):
    def test_empty_snapshot_list_returns_empty_dict(self) -> None:
        self.assertEqual(resolve_earliest_winners([]), {})

    def test_single_snapshot_below_min_quorum_yields_no_winner(self) -> None:
        # One healthy, claimed snapshot is NOT enough for quorum
        # (min_healthy_nodes=2 in the default fixture).
        snapshots = [
            snapshot(node_host="192.168.0.102", king="Team Alpha", king_mtime_epoch=1000),
        ]
        self.assertEqual(resolve_earliest_winners(snapshots), {})

    def test_all_non_running_statuses_are_filtered(self) -> None:
        for status in ("degraded", "failed", "unreachable", "paused", "unknown"):
            with self.subTest(status=status):
                snapshots = [
                    snapshot(node_host=host, king="Team Alpha", king_mtime_epoch=1000, status=status)
                    for host in ("192.168.0.102", "192.168.0.103", "192.168.0.106")
                ]
                self.assertEqual(resolve_earliest_winners(snapshots), {})

    def test_all_unclaimed_yields_no_winner(self) -> None:
        snapshots = [
            snapshot(node_host=host, king="unclaimed", king_mtime_epoch=1000)
            for host in ("192.168.0.102", "192.168.0.103", "192.168.0.106")
        ]
        self.assertEqual(resolve_earliest_winners(snapshots), {})

    def test_mixed_case_unclaimed_is_still_filtered(self) -> None:
        # ``UNCLAIMED`` / ``Unclaimed`` must be treated the same as lowercase.
        snapshots = [
            snapshot(node_host="192.168.0.102", king="UNCLAIMED", king_mtime_epoch=1000),
            snapshot(node_host="192.168.0.103", king="Unclaimed", king_mtime_epoch=1000),
            snapshot(node_host="192.168.0.106", king="unclaimed", king_mtime_epoch=1000),
        ]
        self.assertEqual(resolve_earliest_winners(snapshots), {})

    def test_king_none_is_filtered(self) -> None:
        snapshots = [
            snapshot(node_host="192.168.0.102", king=None, king_mtime_epoch=None, status="running"),
            snapshot(node_host="192.168.0.103", king=None, king_mtime_epoch=None, status="running"),
            snapshot(node_host="192.168.0.106", king="Team Alpha", king_mtime_epoch=1000),
        ]
        self.assertEqual(resolve_earliest_winners(snapshots), {})

    def test_king_valid_but_mtime_epoch_none_is_skipped(self) -> None:
        # A claim is only scoreable if we have a timestamp. Otherwise the
        # sort key would implicitly treat it as mtime=0 and a replica with
        # a missing stat would trump a properly-dated claim.
        snapshots = [
            snapshot(node_host="192.168.0.102", king="Team Alpha", king_mtime_epoch=None),
            snapshot(node_host="192.168.0.103", king="Team Alpha", king_mtime_epoch=None),
            snapshot(node_host="192.168.0.106", king="Team Alpha", king_mtime_epoch=None),
        ]
        self.assertEqual(resolve_earliest_winners(snapshots), {})

    def test_mtime_epoch_zero_is_accepted_as_earliest(self) -> None:
        # mtime=0 is the earliest representable epoch. It must NOT be
        # treated as "missing" just because the ``or 0`` fallback in the
        # sort key would produce the same number.
        snapshots = [
            snapshot(node_host="192.168.0.102", king="Team Alpha", king_mtime_epoch=0),
            snapshot(node_host="192.168.0.103", king="Team Alpha", king_mtime_epoch=500),
            snapshot(node_host="192.168.0.106", king="Team Alpha", king_mtime_epoch=1000),
        ]
        winners = resolve_earliest_winners(snapshots)
        self.assertIn("A", winners)
        self.assertEqual(winners["A"].mtime_epoch, 0)

    def test_malformed_claim_with_newline_is_rejected(self) -> None:
        # Control characters below 0x20 (including \n, \r, \t) must be
        # rejected even though the rest of the claim looks reasonable.
        snapshots = [
            snapshot(node_host="192.168.0.102", king="Team\nAlpha", king_mtime_epoch=900),
            snapshot(node_host="192.168.0.103", king="Team Beta", king_mtime_epoch=950),
            snapshot(node_host="192.168.0.106", king="Team Beta", king_mtime_epoch=960),
        ]
        winners = resolve_earliest_winners(snapshots)
        self.assertEqual(winners["A"].team_name, "Team Beta")

    def test_supporting_nodes_count_reflects_all_matching(self) -> None:
        # When every healthy replica agrees, supporting_nodes equals the
        # total healthy replica count, not min_healthy_nodes.
        snapshots = [
            snapshot(node_host="192.168.0.102", king="Team Alpha", king_mtime_epoch=1000),
            snapshot(node_host="192.168.0.103", king="Team Alpha", king_mtime_epoch=1010),
            snapshot(node_host="192.168.0.106", king="Team Alpha", king_mtime_epoch=1020),
        ]
        winners = resolve_earliest_winners(snapshots)
        self.assertEqual(winners["A"].supporting_nodes, 3)

    def test_current_owner_with_no_remaining_support_falls_back_to_quorum(self) -> None:
        # Authoritative owner "Team Alpha" has only 1 replica supporting
        # them; challenger "Team Beta" has 2. Quorum path wins Beta.
        snapshots = [
            snapshot(node_host="192.168.0.102", king="Team Alpha", king_mtime_epoch=1000),
            snapshot(node_host="192.168.0.103", king="Team Beta", king_mtime_epoch=900),
            snapshot(node_host="192.168.0.106", king="Team Beta", king_mtime_epoch=910),
        ]
        winners = resolve_earliest_winners(
            snapshots,
            current_owners={"A": {"owner_team": "Team Alpha"}},
        )
        self.assertEqual(winners["A"].team_name, "Team Beta")
        self.assertEqual(winners["A"].reason, "earliest_quorum")

    def test_current_owner_reason_is_current_owner_quorum_when_kept(self) -> None:
        snapshots = [
            snapshot(node_host="192.168.0.102", king="Team Alpha", king_mtime_epoch=1000),
            snapshot(node_host="192.168.0.103", king="Team Alpha", king_mtime_epoch=1010),
            snapshot(node_host="192.168.0.106", king="Team Beta", king_mtime_epoch=900),
        ]
        winners = resolve_earliest_winners(
            snapshots,
            current_owners={"A": {"owner_team": "Team Alpha"}},
        )
        self.assertEqual(winners["A"].reason, "current_owner_quorum")

    def test_current_owners_with_empty_owner_team_is_ignored(self) -> None:
        # An empty ``owner_team`` string means "no owner of record", and
        # the scorer must take the ordinary quorum path.
        snapshots = [
            snapshot(node_host="192.168.0.102", king="Team Beta", king_mtime_epoch=900),
            snapshot(node_host="192.168.0.103", king="Team Beta", king_mtime_epoch=910),
            snapshot(node_host="192.168.0.106", king="Team Alpha", king_mtime_epoch=1000),
        ]
        winners = resolve_earliest_winners(
            snapshots,
            current_owners={"A": {"owner_team": ""}},
        )
        self.assertEqual(winners["A"].team_name, "Team Beta")
        self.assertEqual(winners["A"].reason, "earliest_quorum")

    def test_current_owners_missing_variant_key_uses_quorum(self) -> None:
        # current_owners dict present but no entry for this variant:
        # scorer falls straight through to the quorum path.
        snapshots = [
            snapshot(node_host="192.168.0.102", king="Team Alpha", king_mtime_epoch=1000),
            snapshot(node_host="192.168.0.103", king="Team Alpha", king_mtime_epoch=1010),
            snapshot(node_host="192.168.0.106", king="Team Alpha", king_mtime_epoch=1020),
        ]
        winners = resolve_earliest_winners(snapshots, current_owners={"B": {"owner_team": "Team Beta"}})
        self.assertEqual(winners["A"].team_name, "Team Alpha")
        self.assertEqual(winners["A"].reason, "earliest_quorum")

    def test_unknown_node_host_still_counts_but_loses_priority_tiebreak(self) -> None:
        # A snapshot from an IP that is not in NODE_PRIORITY still counts
        # toward quorum, but loses the priority tie-breaker (default 999)
        # and then the host-lexicographic tie-breaker against any known
        # host.
        snapshots = [
            snapshot(node_host="192.168.0.102", king="Team Alpha", king_mtime_epoch=1000),
            snapshot(node_host="10.0.0.5", king="Team Alpha", king_mtime_epoch=1000),
            snapshot(node_host="192.168.0.103", king="Team Alpha", king_mtime_epoch=1000),
        ]
        winners = resolve_earliest_winners(snapshots)
        # .102 is NODE_PRIORITY[0]; it wins the tie-breaker over the
        # unknown 10.0.0.5 host.
        self.assertEqual(winners["A"].node_host, "192.168.0.102")

    def test_snapshot_for_variant_not_in_settings_is_still_scored(self) -> None:
        # A snapshot carrying variant "Z" reaches the scorer via setdefault
        # on the ``by_variant`` dict. If a future refactor restricts the
        # scorer to SETTINGS.variants only, this test surfaces it.
        snapshots = [
            snapshot(node_host="192.168.0.102", variant="Z", king="Team Alpha", king_mtime_epoch=1000),
            snapshot(node_host="192.168.0.103", variant="Z", king="Team Alpha", king_mtime_epoch=1010),
            snapshot(node_host="192.168.0.106", variant="Z", king="Team Alpha", king_mtime_epoch=1020),
        ]
        winners = resolve_earliest_winners(snapshots)
        self.assertIn("Z", winners)
        self.assertEqual(winners["Z"].team_name, "Team Alpha")

    def test_multiple_variants_in_one_poll_each_resolved_independently(self) -> None:
        # Alpha wins A on hosts .102/.103; nobody wins B (split); Gamma
        # wins C on .103/.106.
        snapshots = [
            snapshot(node_host="192.168.0.102", variant="A", king="Team Alpha", king_mtime_epoch=1000),
            snapshot(node_host="192.168.0.103", variant="A", king="Team Alpha", king_mtime_epoch=1010),
            snapshot(node_host="192.168.0.106", variant="A", king="Team Beta", king_mtime_epoch=900),
            snapshot(node_host="192.168.0.102", variant="B", king="Team Alpha", king_mtime_epoch=900),
            snapshot(node_host="192.168.0.103", variant="B", king="Team Beta", king_mtime_epoch=900),
            snapshot(node_host="192.168.0.106", variant="B", king="Team Gamma", king_mtime_epoch=900),
            snapshot(node_host="192.168.0.102", variant="C", king="Team Alpha", king_mtime_epoch=1000),
            snapshot(node_host="192.168.0.103", variant="C", king="Team Gamma", king_mtime_epoch=800),
            snapshot(node_host="192.168.0.106", variant="C", king="Team Gamma", king_mtime_epoch=810),
        ]
        winners = resolve_earliest_winners(snapshots)
        self.assertEqual(winners["A"].team_name, "Team Alpha")
        self.assertNotIn("B", winners)
        self.assertEqual(winners["C"].team_name, "Team Gamma")
