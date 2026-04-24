"""Integration tests for the scheduler lifecycle state machine.

These tests drive a real ``RefereeRuntime`` with a real ``Database`` backed
by a tmp-path SQLite file. External effects are stubbed: SSH via
``DummySSH``, time via ``scheduler.time.sleep`` / ``scheduler.time.monotonic``
patches, and the compose runner via ``runtime._run_compose_parallel`` mocks.

Every test in this file starts from ``status='stopped'`` and drives the FSM
through one or more transitions, asserting the durable DB state and the
event log at each step.
"""
from __future__ import annotations

import itertools
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from config import SETTINGS
from db import Database
from poller import VariantSnapshot, ViolationHit
from scheduler import RefereeRuntime, RuntimeGuardError

from tests.conftest import DummyScheduler, DummySSH, snapshot


pytestmark = pytest.mark.integration


class RuntimeSafetyTests(unittest.TestCase):
    def make_runtime(self) -> tuple[RefereeRuntime, Database]:
        fd, raw_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db_path = Path(raw_path)

        db = Database(db_path)
        db.initialize()
        self.addCleanup(lambda: db_path.exists() and db_path.unlink())
        self.addCleanup(db.close)
        runtime = RefereeRuntime(db, DummySSH())
        return runtime, db

    def test_start_competition_requires_team_roster(self) -> None:
        runtime, db = self.make_runtime()
        runtime._run_compose_parallel = Mock(return_value={})
        runtime.poller.run_cycle = Mock(return_value=([], {}))

        with self.assertRaises(RuntimeGuardError):
            runtime.start_competition()

        self.assertEqual(db.get_competition()["status"], "stopped")
        self.assertEqual(db.team_count(), 0)

    def test_start_competition_with_existing_teams_enters_running(self) -> None:
        runtime, db = self.make_runtime()
        db.upsert_team_names(["Team Alpha"])
        runtime._run_compose_parallel = Mock(return_value={})
        runtime.poller.run_cycle = Mock(
            return_value=(
                [
                    snapshot(node_host="192.168.0.102", variant="A", king="unclaimed"),
                    snapshot(node_host="192.168.0.102", variant="B", king="unclaimed"),
                    snapshot(node_host="192.168.0.102", variant="C", king="unclaimed"),
                    snapshot(node_host="192.168.0.103", variant="A", king="unclaimed"),
                    snapshot(node_host="192.168.0.103", variant="B", king="unclaimed"),
                    snapshot(node_host="192.168.0.103", variant="C", king="unclaimed"),
                    snapshot(node_host="192.168.0.106", variant="A", king="unclaimed"),
                    snapshot(node_host="192.168.0.106", variant="B", king="unclaimed"),
                    snapshot(node_host="192.168.0.106", variant="C", king="unclaimed"),
                ],
                {},
            )
        )

        runtime.start_competition()

        state = db.get_competition()
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["current_series"], 1)

    def test_start_competition_rolls_back_failed_deploy(self) -> None:
        runtime, db = self.make_runtime()
        original_timeout = SETTINGS.deploy_health_timeout_seconds
        object.__setattr__(SETTINGS, "deploy_health_timeout_seconds", 1)
        self.addCleanup(lambda: object.__setattr__(SETTINGS, "deploy_health_timeout_seconds", original_timeout))
        db.upsert_team_names(["Team Alpha"])

        def compose(series: int, command: str):
            if "up -d" in command:
                return {"192.168.0.102": (False, "boom")}
            if "down -v" in command:
                return {"192.168.0.102": (True, "rolled back")}
            return {}

        runtime._run_compose_parallel = Mock(side_effect=compose)
        runtime.poller.run_cycle = Mock(return_value=([], {}))

        with patch("scheduler.fire_and_forget", lambda payload: None), patch(
            "scheduler.time.sleep",
            return_value=None,
        ), patch("scheduler.time.monotonic", side_effect=[0, 0, 2, 2]):
            with self.assertRaises(RuntimeGuardError):
                runtime.start_competition()

        state = db.get_competition()
        self.assertEqual(state["status"], "stopped")
        self.assertTrue(
            any(
                event["detail"] == "Competition startup failed; referee left in stopped state"
                for event in db.list_events(limit=20)
            )
        )

    def test_rotate_to_series_pauses_on_failed_health_gate(self) -> None:
        runtime, db = self.make_runtime()
        original_timeout = SETTINGS.deploy_health_timeout_seconds
        object.__setattr__(SETTINGS, "deploy_health_timeout_seconds", 1)
        self.addCleanup(lambda: object.__setattr__(SETTINGS, "deploy_health_timeout_seconds", original_timeout))
        db.upsert_team_names(["Team Alpha"])
        db.set_competition_state(status="running", current_series=1)
        runtime.poll_once = Mock()
        runtime._run_compose_parallel = Mock(
            side_effect=[
                {},
                {
                    "192.168.0.102": (False, "boom"),
                    "192.168.0.103": (False, "boom"),
                    "192.168.0.106": (False, "boom"),
                },
                {},
                {
                    "192.168.0.102": (False, "still-broken"),
                    "192.168.0.103": (False, "still-broken"),
                    "192.168.0.106": (False, "still-broken"),
                },
                {},
            ]
        )
        runtime.poller.run_cycle = Mock(side_effect=itertools.repeat(([], {})))

        with patch("scheduler.fire_and_forget", lambda payload: None), patch(
            "scheduler.time.sleep",
            return_value=None,
        ), patch("scheduler.time.monotonic", side_effect=[0, 2, 0, 2]):
            with self.assertRaises(RuntimeGuardError):
                runtime.rotate_to_series(2)

        state = db.get_competition()
        self.assertEqual(state["status"], "faulted")
        self.assertEqual(state["current_series"], 1)
        self.assertIn("rollback to H1 also failed", state["fault_reason"])

    def test_rotate_to_series_rolls_back_previous_series_after_failed_target_deploy(self) -> None:
        runtime, db = self.make_runtime()
        original_timeout = SETTINGS.deploy_health_timeout_seconds
        object.__setattr__(SETTINGS, "deploy_health_timeout_seconds", 1)
        self.addCleanup(lambda: object.__setattr__(SETTINGS, "deploy_health_timeout_seconds", original_timeout))
        db.upsert_team_names(["Team Alpha"])
        db.increment_team_offense("Team Alpha")
        db.increment_team_offense("Team Alpha")
        db.set_competition_state(status="running", current_series=1)
        runtime.poll_once = Mock()
        runtime._run_compose_parallel = Mock(
            side_effect=[
                {},
                {
                    "192.168.0.102": (False, "boom"),
                    "192.168.0.103": (False, "boom"),
                    "192.168.0.106": (False, "boom"),
                },
                {},
                {},
            ]
        )
        healthy_snapshots = [
            snapshot(node_host="192.168.0.102", variant="A", king="unclaimed"),
            snapshot(node_host="192.168.0.102", variant="B", king="unclaimed"),
            snapshot(node_host="192.168.0.102", variant="C", king="unclaimed"),
            snapshot(node_host="192.168.0.103", variant="A", king="unclaimed"),
            snapshot(node_host="192.168.0.103", variant="B", king="unclaimed"),
            snapshot(node_host="192.168.0.103", variant="C", king="unclaimed"),
            snapshot(node_host="192.168.0.106", variant="A", king="unclaimed"),
            snapshot(node_host="192.168.0.106", variant="B", king="unclaimed"),
            snapshot(node_host="192.168.0.106", variant="C", king="unclaimed"),
        ]
        runtime.poller.run_cycle = Mock(side_effect=[([], {}), (healthy_snapshots, {}), (healthy_snapshots, {})])

        with patch("scheduler.fire_and_forget", lambda payload: None), patch(
            "scheduler.time.sleep",
            return_value=None,
        ), patch("scheduler.time.monotonic", side_effect=[0, 2, 0]):
            runtime.rotate_to_series(2)

        state = db.get_competition()
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["current_series"], 1)
        self.assertTrue(state["next_rotation"])
        self.assertEqual(db.get_team("Team Alpha")["status"], "series_banned")
        self.assertTrue(
            any(
                event["detail"] == "Rotation to H2 failed; automatically rolled back to H1"
                for event in db.list_events(limit=20)
            )
        )

    def test_degraded_node_does_not_block_rotation_when_quorum_holds(self) -> None:
        runtime, db = self.make_runtime()
        db.upsert_team_names(["Team Alpha"])
        db.set_competition_state(status="running", current_series=1)
        runtime.poll_once = Mock()
        runtime._run_compose_parallel = Mock(side_effect=[{}, {}, {}])
        runtime.poller.run_cycle = Mock(
            return_value=(
                [
                    snapshot(node_host="192.168.0.102", variant="A", king="unclaimed", node_epoch=1000),
                    snapshot(node_host="192.168.0.102", variant="B", king="unclaimed", node_epoch=1000),
                    snapshot(node_host="192.168.0.102", variant="C", king="unclaimed", node_epoch=1000),
                    snapshot(node_host="192.168.0.103", variant="A", king="unclaimed", node_epoch=1001),
                    snapshot(node_host="192.168.0.103", variant="B", king="unclaimed", node_epoch=1001),
                    snapshot(node_host="192.168.0.103", variant="C", king="unclaimed", node_epoch=1001),
                    snapshot(node_host="192.168.0.106", variant="A", king="unclaimed", node_epoch=1010),
                    snapshot(node_host="192.168.0.106", variant="B", king="unclaimed", node_epoch=1010),
                    snapshot(node_host="192.168.0.106", variant="C", king="unclaimed", node_epoch=1010),
                ],
                {},
            )
        )

        runtime.rotate_to_series(2)

        state = db.get_competition()
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["current_series"], 2)

    def test_deploy_series_or_raise_retries_until_health_recovers(self) -> None:
        runtime, db = self.make_runtime()
        original_timeout = SETTINGS.deploy_health_timeout_seconds
        object.__setattr__(SETTINGS, "deploy_health_timeout_seconds", 1)
        self.addCleanup(lambda: object.__setattr__(SETTINGS, "deploy_health_timeout_seconds", original_timeout))
        runtime._run_compose_parallel = Mock(return_value={})
        bad_snapshots = [
            snapshot(node_host="192.168.0.102", variant="A", king=None, king_mtime_epoch=None, status="failed"),
            snapshot(node_host="192.168.0.102", variant="B", king="unclaimed"),
            snapshot(node_host="192.168.0.102", variant="C", king="unclaimed"),
            snapshot(node_host="192.168.0.103", variant="A", king=None, king_mtime_epoch=None, status="failed"),
            snapshot(node_host="192.168.0.103", variant="B", king="unclaimed"),
            snapshot(node_host="192.168.0.103", variant="C", king="unclaimed"),
            snapshot(node_host="192.168.0.106", variant="A", king="unclaimed"),
            snapshot(node_host="192.168.0.106", variant="B", king="unclaimed"),
            snapshot(node_host="192.168.0.106", variant="C", king="unclaimed"),
        ]
        good_snapshots = [
            snapshot(node_host="192.168.0.102", variant="A", king="unclaimed"),
            snapshot(node_host="192.168.0.102", variant="B", king="unclaimed"),
            snapshot(node_host="192.168.0.102", variant="C", king="unclaimed"),
            snapshot(node_host="192.168.0.103", variant="A", king="unclaimed"),
            snapshot(node_host="192.168.0.103", variant="B", king="unclaimed"),
            snapshot(node_host="192.168.0.103", variant="C", king="unclaimed"),
            snapshot(node_host="192.168.0.106", variant="A", king="unclaimed"),
            snapshot(node_host="192.168.0.106", variant="B", king="unclaimed"),
            snapshot(node_host="192.168.0.106", variant="C", king="unclaimed"),
        ]
        runtime.poller.run_cycle = Mock(side_effect=[(bad_snapshots, {}), (good_snapshots, {}), (good_snapshots, {})])
        with patch("scheduler.fire_and_forget", lambda payload: None), patch(
            "scheduler.time.sleep",
            return_value=None,
        ), patch("scheduler.time.monotonic", side_effect=[0, 0]):
            result = runtime._deploy_series_or_raise(series=2)
        self.assertEqual(len(result), 9)
        self.assertEqual(runtime.poller.run_cycle.call_count, 3)

    def test_baseline_violation_detects_missing_to_present_authkeys(self) -> None:
        runtime, db = self.make_runtime()
        runtime._capture_baselines(
            1,
            [
                VariantSnapshot(
                    node_host="192.168.0.102",
                    variant="A",
                    king="unclaimed",
                    king_mtime_epoch=1,
                    status="running",
                    sections={"AUTHKEYS": "", "SHADOW": "", "IPTABLES": "ok", "PORTS": "ok"},
                    checked_at=datetime.now(UTC),
                )
            ],
        )

        violations: dict[tuple[str, str], list[object]] = {}
        runtime._merge_baseline_violations(
            series=1,
            snapshots=[
                VariantSnapshot(
                    node_host="192.168.0.102",
                    variant="A",
                    king="Team Alpha",
                    king_mtime_epoch=2,
                    status="running",
                    sections={
                        "AUTHKEYS": f"{'a' * 64}  /root/.ssh/authorized_keys",
                        "SHADOW": "",
                        "IPTABLES": "ok",
                        "PORTS": "ok",
                    },
                    checked_at=datetime.now(UTC),
                )
            ],
            violations=violations,
        )

        hits = violations[("192.168.0.102", "A")]
        self.assertEqual(hits[0].offense_name, "authkeys_changed")

    def test_h1b_authkeys_change_is_exempt_from_baseline_violation(self) -> None:
        runtime, db = self.make_runtime()
        runtime._capture_baselines(
            1,
            [
                VariantSnapshot(
                    node_host="192.168.0.102",
                    variant="B",
                    king="unclaimed",
                    king_mtime_epoch=1,
                    status="running",
                    sections={"AUTHKEYS": "", "SHADOW": "", "IPTABLES": "ok", "PORTS": "ok"},
                    checked_at=datetime.now(UTC),
                )
            ],
        )

        violations: dict[tuple[str, str], list[object]] = {}
        runtime._merge_baseline_violations(
            series=1,
            snapshots=[
                VariantSnapshot(
                    node_host="192.168.0.102",
                    variant="B",
                    king="Team Alpha",
                    king_mtime_epoch=2,
                    status="running",
                    sections={
                        "AUTHKEYS": f"{'a' * 64}  /root/.ssh/authorized_keys",
                        "SHADOW": "",
                        "IPTABLES": "ok",
                        "PORTS": "ok",
                    },
                    checked_at=datetime.now(UTC),
                )
            ],
            violations=violations,
        )

        self.assertEqual(violations.get(("192.168.0.102", "B")), [])

    def test_h7b_shadow_change_is_exempt_from_baseline_violation(self) -> None:
        runtime, db = self.make_runtime()
        runtime._capture_baselines(
            7,
            [
                VariantSnapshot(
                    node_host="192.168.0.102",
                    variant="B",
                    king="unclaimed",
                    king_mtime_epoch=1,
                    status="running",
                    sections={"AUTHKEYS": "", "SHADOW": "", "IPTABLES": "ok", "PORTS": "ok"},
                    checked_at=datetime.now(UTC),
                )
            ],
        )

        violations: dict[tuple[str, str], list[object]] = {}
        runtime._merge_baseline_violations(
            series=7,
            snapshots=[
                VariantSnapshot(
                    node_host="192.168.0.102",
                    variant="B",
                    king="Team Alpha",
                    king_mtime_epoch=2,
                    status="running",
                    sections={
                        "AUTHKEYS": "",
                        "SHADOW": f"{'b' * 64}  /etc/shadow",
                        "IPTABLES": "ok",
                        "PORTS": "ok",
                    },
                    checked_at=datetime.now(UTC),
                )
            ],
            violations=violations,
        )

        self.assertEqual(violations.get(("192.168.0.102", "B")), [])

    def test_baseline_snapshots_without_hits_do_not_escalate_team(self) -> None:
        runtime, db = self.make_runtime()
        db.upsert_team_names(["Team Alpha"])
        db.set_competition_state(status="running", current_series=1)

        snapshots = [
            VariantSnapshot(
                node_host=node_host,
                variant=variant,
                king="Team Alpha" if variant == "A" and node_host != "192.168.0.106" else "unclaimed",
                king_mtime_epoch=1000,
                status="running",
                sections={"AUTHKEYS": "", "SHADOW": "", "IPTABLES": "ok", "PORTS": "ok", "NODE_EPOCH": "1000"},
                checked_at=datetime.now(UTC),
            )
            for node_host in SETTINGS.node_hosts
            for variant in SETTINGS.variants
        ]
        runtime._capture_baselines(1, snapshots)
        runtime.poller.run_cycle = Mock(return_value=(snapshots, {}))

        runtime.poll_once()

        team = db.get_team("Team Alpha")
        self.assertEqual(team["offense_count"], 0)
        self.assertEqual(team["status"], "active")

    def test_repeated_violation_only_escalates_once_until_cleared(self) -> None:
        runtime, db = self.make_runtime()
        db.upsert_team_names(["Team Alpha"])
        db.set_competition_state(status="running", current_series=2)
        baseline_ports = "\n".join(
            [
                "State Recv-Q Send-Q Local Address:Port Peer Address:Port Process",
                "LISTEN 0 128 *:8080 *:* users:((\"java\",pid=1,fd=123))",
            ]
        )
        violating_ports = "\n".join(
            [
                "State Recv-Q Send-Q Local Address:Port Peer Address:Port Process",
                "LISTEN 0 128 *:8080 *:* users:((\"java\",pid=1,fd=123))",
                "LISTEN 0 128 *:9005 *:* users:((\"java\",pid=1,fd=124))",
            ]
        )

        baseline = [
            VariantSnapshot(
                node_host=node_host,
                variant=variant,
                king="unclaimed",
                king_mtime_epoch=1000,
                status="running",
                sections={"AUTHKEYS": "", "SHADOW": "", "IPTABLES": "ok", "PORTS": baseline_ports, "ROOT_DIR": "700", "KING_STAT": "1000 644 root:root regular file", "KING": "unclaimed", "NODE_EPOCH": "1000"},
                checked_at=datetime.now(UTC),
            )
            for node_host in SETTINGS.node_hosts
            for variant in SETTINGS.variants
        ]
        runtime._capture_baselines(2, baseline)

        violating = [
            VariantSnapshot(
                node_host=node_host,
                variant=variant,
                king="Team Alpha" if variant == "C" and node_host == "192.168.0.103" else "unclaimed",
                king_mtime_epoch=1000,
                status="running",
                sections={"AUTHKEYS": "", "SHADOW": "", "IPTABLES": "ok", "PORTS": violating_ports if variant == "C" and node_host == "192.168.0.103" else baseline_ports, "ROOT_DIR": "700", "KING_STAT": "1000 644 root:root regular file", "KING": "Team Alpha" if variant == "C" and node_host == "192.168.0.103" else "unclaimed", "NODE_EPOCH": "1000"},
                checked_at=datetime.now(UTC),
            )
            for node_host in SETTINGS.node_hosts
            for variant in SETTINGS.variants
        ]
        clean = [
            VariantSnapshot(
                node_host=node_host,
                variant=variant,
                king="unclaimed",
                king_mtime_epoch=1000,
                status="running",
                sections={"AUTHKEYS": "", "SHADOW": "", "IPTABLES": "ok", "PORTS": baseline_ports, "ROOT_DIR": "700", "KING_STAT": "1000 644 root:root regular file", "KING": "unclaimed", "NODE_EPOCH": "1000"},
                checked_at=datetime.now(UTC),
            )
            for node_host in SETTINGS.node_hosts
            for variant in SETTINGS.variants
        ]

        runtime.poller.run_cycle = Mock(side_effect=[(violating, {}), (violating, {}), (clean, {}), (violating, {})])

        runtime.poll_once()
        self.assertEqual(db.get_team("Team Alpha")["offense_count"], 1)
        self.assertEqual(len(db.list_violations()), 1)

        runtime.poll_once()
        self.assertEqual(db.get_team("Team Alpha")["offense_count"], 1)
        self.assertEqual(len(db.list_violations()), 1)

        runtime.poll_once()
        self.assertEqual(db.get_team("Team Alpha")["offense_count"], 1)

        runtime.poll_once()
        self.assertEqual(db.get_team("Team Alpha")["offense_count"], 2)
        self.assertEqual(len(db.list_violations()), 2)

    def test_deleted_king_violation_falls_back_to_current_owner(self) -> None:
        runtime, db = self.make_runtime()
        db.upsert_team_names(["Team Alpha"])
        db.set_competition_state(status="running", current_series=2)
        db.set_variant_owner(
            series=2,
            variant="C",
            owner_team="Team Alpha",
            accepted_mtime_epoch=1000,
            source_node_host="192.168.0.103",
            evidence={},
        )
        snapshots = [
            snapshot(node_host="192.168.0.102", variant="A", king="unclaimed", king_mtime_epoch=1),
            snapshot(node_host="192.168.0.103", variant="A", king="unclaimed", king_mtime_epoch=1),
            snapshot(node_host="192.168.0.106", variant="A", king="unclaimed", king_mtime_epoch=1),
            snapshot(node_host="192.168.0.102", variant="B", king="unclaimed", king_mtime_epoch=1),
            snapshot(node_host="192.168.0.103", variant="B", king="unclaimed", king_mtime_epoch=1),
            snapshot(node_host="192.168.0.106", variant="B", king="unclaimed", king_mtime_epoch=1),
            VariantSnapshot(
                node_host="192.168.0.102",
                variant="C",
                king=None,
                king_mtime_epoch=None,
                status="failed",
                sections={"KING": "FILE_MISSING", "KING_STAT": "STAT_FAIL", "ROOT_DIR": "700", "NODE_EPOCH": "1000"},
                checked_at=datetime.now(UTC),
            ),
            snapshot(node_host="192.168.0.103", variant="C", king="Team Alpha", king_mtime_epoch=1000),
            snapshot(node_host="192.168.0.106", variant="C", king="Team Alpha", king_mtime_epoch=1010),
        ]
        runtime.poller.run_cycle = Mock(
            return_value=(
                snapshots,
                {("192.168.0.102", "C"): [ViolationHit(4, "king_deleted", {"king": "FILE_MISSING"})]},
            )
        )

        runtime.poll_once()

        team = db.get_team("Team Alpha")
        self.assertEqual(team["offense_count"], 1)
        violations = db.list_violations()
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0]["offense_name"], "king_deleted")

    def test_pause_blocks_scoring(self) -> None:
        runtime, db = self.make_runtime()
        db.upsert_team_names(["Team Alpha"])
        db.set_competition_state(status="paused", current_series=1)
        runtime.poller.run_cycle = Mock(
            return_value=(
                [snapshot(node_host="192.168.0.102", king="Team Alpha", king_mtime_epoch=1)],
                {},
            )
        )

        runtime.poll_once()

        self.assertEqual(db.get_team("Team Alpha")["total_points"], 0)
        self.assertEqual(db.get_competition()["poll_cycle"], 0)

    def test_quorum_loss_blocks_scoring(self) -> None:
        runtime, db = self.make_runtime()
        db.upsert_team_names(["Team Alpha"])
        db.set_competition_state(status="running", current_series=1)
        runtime.poller.run_cycle = Mock(
            return_value=(
                [
                    snapshot(node_host="192.168.0.102", variant="A", king="Team Alpha", king_mtime_epoch=1),
                    snapshot(node_host="192.168.0.102", variant="B", king="unclaimed", king_mtime_epoch=1),
                    snapshot(node_host="192.168.0.102", variant="C", king="unclaimed", king_mtime_epoch=1),
                    snapshot(node_host="192.168.0.103", variant="A", king=None, king_mtime_epoch=None, status="unreachable"),
                    snapshot(node_host="192.168.0.103", variant="B", king="unclaimed", king_mtime_epoch=1),
                    snapshot(node_host="192.168.0.103", variant="C", king="unclaimed", king_mtime_epoch=1),
                    snapshot(node_host="192.168.0.106", variant="A", king=None, king_mtime_epoch=None, status="unreachable"),
                    snapshot(node_host="192.168.0.106", variant="B", king="unclaimed", king_mtime_epoch=1),
                    snapshot(node_host="192.168.0.106", variant="C", king="unclaimed", king_mtime_epoch=1),
                ],
                {},
            )
        )

        runtime.poll_once()

        self.assertEqual(db.get_team("Team Alpha")["total_points"], 0)
        self.assertTrue(
            any(
                "insufficient healthy replicas" in event["detail"]
                for event in db.list_events(limit=20)
            )
        )

    def test_single_node_earliest_claim_does_not_override_authoritative_owner(self) -> None:
        runtime, db = self.make_runtime()
        db.upsert_team_names(["Team Alpha", "Team Beta"])
        db.set_competition_state(status="running", current_series=1)
        db.set_variant_owner(
            series=1,
            variant="A",
            owner_team="Team Alpha",
            accepted_mtime_epoch=1000,
            source_node_host="192.168.0.102",
            evidence={"source": "test"},
        )
        runtime.poller.run_cycle = Mock(
            return_value=(
                [
                    snapshot(node_host="192.168.0.102", variant="A", king="Team Alpha", king_mtime_epoch=1000),
                    snapshot(node_host="192.168.0.103", variant="A", king="Team Alpha", king_mtime_epoch=1010),
                    snapshot(node_host="192.168.0.106", variant="A", king="Team Beta", king_mtime_epoch=900),
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
        owner = db.get_variant_owner(series=1, variant="A")
        self.assertEqual(owner["owner_team"], "Team Alpha")

    def test_authoritative_owner_is_reconciled_to_divergent_healthy_replica(self) -> None:
        runtime, db = self.make_runtime()
        db.upsert_team_names(["Team Alpha", "Team Beta"])
        db.set_competition_state(status="running", current_series=1)
        db.set_variant_owner(
            series=1,
            variant="A",
            owner_team="Team Alpha",
            accepted_mtime_epoch=1000,
            source_node_host="192.168.0.102",
            evidence={"source": "test"},
        )
        runtime.poller.run_cycle = Mock(
            return_value=(
                [
                    snapshot(node_host="192.168.0.102", variant="A", king="Team Alpha", king_mtime_epoch=1000),
                    snapshot(node_host="192.168.0.103", variant="A", king="Team Alpha", king_mtime_epoch=1010),
                    snapshot(node_host="192.168.0.106", variant="A", king="Team Beta", king_mtime_epoch=900),
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

        ssh = runtime.ssh_pool
        self.assertEqual(len(ssh.commands), 1)
        host, command = ssh.commands[0]
        self.assertEqual(host, "192.168.0.106")
        self.assertIn("Team Alpha", command)
        self.assertTrue(
            any(
                event["detail"] == "Reconciled A replica to authoritative owner"
                for event in db.list_events(limit=20)
            )
        )

    def test_resume_requires_validated_current_series(self) -> None:
        runtime, db = self.make_runtime()
        db.upsert_team_names(["Team Alpha"])
        db.set_competition_state(status="paused", current_series=1)
        runtime.poller.run_cycle = Mock(return_value=([], {}))

        with self.assertRaises(RuntimeGuardError):
            runtime.resume_rotation()

        state = db.get_competition()
        self.assertEqual(state["status"], "faulted")
        self.assertIn("failed resume validation", state["fault_reason"])

    def test_start_scheduler_restores_rotation_job_from_db(self) -> None:
        runtime, db = self.make_runtime()
        future_rotation = datetime.now(UTC) + timedelta(minutes=5)
        db.set_competition_state(status="running", current_series=1, next_rotation=future_rotation.isoformat())
        runtime.scheduler = DummyScheduler()

        runtime.start_scheduler()

        self.assertIn("poll", runtime.scheduler.jobs)
        self.assertIn("rotate", runtime.scheduler.jobs)
        self.assertEqual(runtime.scheduler.jobs["rotate"]["trigger"], "date")
        self.assertEqual(runtime.scheduler.jobs["rotate"]["run_date"], future_rotation)

    def test_runtime_endpoint_model_fields_persist_validation_state(self) -> None:
        runtime, db = self.make_runtime()
        validated_at = datetime.now(UTC).isoformat()
        db.set_competition_state(
            status="faulted",
            current_series=2,
            previous_series=1,
            fault_reason="rotation failed",
            last_validated_series=1,
            last_validated_at=validated_at,
        )

        state = db.get_competition()
        self.assertEqual(state["status"], "faulted")
        self.assertEqual(state["previous_series"], 1)
        self.assertEqual(state["fault_reason"], "rotation failed")
        self.assertEqual(state["last_validated_series"], 1)
        self.assertEqual(state["last_validated_at"], validated_at)

    def test_validate_current_series_returns_summary(self) -> None:
        runtime, db = self.make_runtime()
        db.set_competition_state(status="paused", current_series=1)
        runtime.poller.run_cycle = Mock(
            return_value=(
                [
                    snapshot(node_host="192.168.0.102", variant="A", king="unclaimed"),
                    snapshot(node_host="192.168.0.102", variant="B", king="unclaimed"),
                    snapshot(node_host="192.168.0.102", variant="C", king="unclaimed"),
                    snapshot(node_host="192.168.0.103", variant="A", king="unclaimed"),
                    snapshot(node_host="192.168.0.103", variant="B", king="unclaimed"),
                    snapshot(node_host="192.168.0.103", variant="C", king="unclaimed"),
                    snapshot(node_host="192.168.0.106", variant="A", king="unclaimed"),
                    snapshot(node_host="192.168.0.106", variant="B", king="unclaimed"),
                    snapshot(node_host="192.168.0.106", variant="C", king="unclaimed"),
                ],
                {},
            )
        )

        summary = runtime.validate_current_series()

        self.assertTrue(summary["valid"])
        self.assertTrue(summary["complete_snapshot_matrix"])
        self.assertEqual(summary["healthy_nodes"], 3)
        self.assertEqual(summary["total_nodes"], 3)
        self.assertEqual(summary["min_healthy_nodes"], 2)
        self.assertEqual(summary["healthy_counts_by_variant"]["A"], 3)
        state = db.get_competition()
        self.assertEqual(state["last_validated_series"], 1)
        self.assertIsNotNone(state["last_validated_at"])

    def test_recover_current_series_redeploys_faulted_series_and_leaves_paused(self) -> None:
        runtime, db = self.make_runtime()
        db.set_competition_state(status="faulted", current_series=2, fault_reason="broken")
        runtime._run_compose_parallel = Mock(return_value={})
        healthy_snapshots = [
            snapshot(node_host="192.168.0.102", variant="A", king="unclaimed"),
            snapshot(node_host="192.168.0.102", variant="B", king="unclaimed"),
            snapshot(node_host="192.168.0.102", variant="C", king="unclaimed"),
            snapshot(node_host="192.168.0.103", variant="A", king="unclaimed"),
            snapshot(node_host="192.168.0.103", variant="B", king="unclaimed"),
            snapshot(node_host="192.168.0.103", variant="C", king="unclaimed"),
            snapshot(node_host="192.168.0.106", variant="A", king="unclaimed"),
            snapshot(node_host="192.168.0.106", variant="B", king="unclaimed"),
            snapshot(node_host="192.168.0.106", variant="C", king="unclaimed"),
        ]
        runtime.poller.run_cycle = Mock(return_value=(healthy_snapshots, {}))

        result = runtime.recover_current_series()

        self.assertTrue(result["ok"])
        self.assertEqual(result["competition_status"], "paused")
        state = db.get_competition()
        self.assertEqual(state["status"], "paused")
        self.assertEqual(state["current_series"], 2)
        self.assertIsNone(state["fault_reason"])
        self.assertEqual(state["last_validated_series"], 2)

    def test_recover_current_series_failure_remains_faulted(self) -> None:
        runtime, db = self.make_runtime()
        original_timeout = SETTINGS.deploy_health_timeout_seconds
        object.__setattr__(SETTINGS, "deploy_health_timeout_seconds", 1)
        self.addCleanup(lambda: object.__setattr__(SETTINGS, "deploy_health_timeout_seconds", original_timeout))
        db.set_competition_state(status="faulted", current_series=2, fault_reason="broken")

        def compose(series: int, command: str):
            if "up -d" in command:
                return {"192.168.0.102": (False, "boom")}
            return {}

        runtime._run_compose_parallel = Mock(side_effect=compose)
        runtime.poller.run_cycle = Mock(return_value=([], {}))

        with patch("scheduler.fire_and_forget", lambda payload: None), patch(
            "scheduler.time.sleep",
            return_value=None,
        ), patch("scheduler.time.monotonic", side_effect=[0, 0, 2, 2]):
            with self.assertRaises(RuntimeGuardError):
                runtime.recover_current_series()

        state = db.get_competition()
        self.assertEqual(state["status"], "faulted")
        self.assertIn("Recovery redeploy for H2 failed", state["fault_reason"])
