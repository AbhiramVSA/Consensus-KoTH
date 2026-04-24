"""Unit tests for the ``Poller`` shell-output parsing and normalisation helpers.

Nothing here actually talks over SSH: every test either calls static helpers
on ``Poller`` or drives it with the recording ``DummySSH`` double from
``conftest.py``.
"""
from __future__ import annotations

import unittest
from datetime import UTC, datetime

import pytest

from poller import Poller, VariantSnapshot

from tests.conftest import DummySSH


pytestmark = pytest.mark.unit


class PollerCompletenessTests(unittest.TestCase):
    def test_partial_output_synthesizes_missing_variants_as_failed(self) -> None:
        from config import SETTINGS  # imported lazily so SETTINGS reflects autouse fixture

        class PartialSSH:
            def exec(self, host: str, command: str):
                _ = command
                return (
                    1,
                    "\n".join(
                        [
                            "===VARIANT:A===",
                            "===KING===",
                            "Team Alpha",
                            "===KING_STAT===",
                            "1000 644 root:root regular file",
                            "===END_VARIANT===",
                        ]
                    ),
                    "simulated failure",
                )

        poller = Poller(PartialSSH())

        snapshots, violations = poller.run_cycle(series=1)

        self.assertEqual(len(snapshots), len(SETTINGS.node_hosts) * len(SETTINGS.variants))
        self.assertEqual({snap.variant for snap in snapshots}, {"A", "B", "C"})
        self.assertEqual({snap.node_host for snap in snapshots}, set(SETTINGS.node_hosts))
        self.assertEqual(
            {snap.variant for snap in snapshots if snap.status == "failed"},
            {"B", "C"},
        )
        self.assertEqual(violations, {})

    def test_watchdog_detection_does_not_flag_one_shot_king_write_command(self) -> None:
        poller = Poller(DummySSH())
        snap = VariantSnapshot(
            node_host="192.168.0.102",
            variant="C",
            king="Team Alpha",
            king_mtime_epoch=1000,
            status="running",
            sections={
                "KING_STAT": "1000 644 root:root regular file",
                "KING": "Team Alpha",
                "ROOT_DIR": "700",
                "IMMUTABLE": "",
                "CRON": "",
                "PROCS": "root  42  0.0  bash -p -c 'echo Team Alpha > /root/king.txt'",
            },
            checked_at=datetime.now(UTC),
        )

        hits = poller._detect_violations(snap)

        self.assertFalse(any(hit.offense_name == "watchdog_process" for hit in hits))

    def test_probe_command_uses_root_exec_and_separates_king_section(self) -> None:
        poller = Poller(DummySSH())

        command = poller._build_probe_command(series=2)

        self.assertIn('docker exec -u 0 "$container_id" sh -lc', command)
        self.assertIn('printf "\\n";', command)

    def test_normalize_king_strips_inline_section_marker(self) -> None:
        self.assertEqual(Poller._normalize_king("unclaimed===KING_STAT==="), "unclaimed")

    def test_stable_ports_signature_ignores_docker_dns_stub_port(self) -> None:
        poller = Poller(DummySSH())
        first = """State  Recv-Q Send-Q Local Address:Port Peer Address:Port
LISTEN 0 4096 127.0.0.11:38271 0.0.0.0:*
LISTEN 0 1 [::ffff:127.0.0.1]:8005 *:*
LISTEN 0 100 *:8080 *:*
"""
        second = """State  Recv-Q Send-Q Local Address:Port Peer Address:Port
LISTEN 0 4096 127.0.0.11:46209 0.0.0.0:*
LISTEN 0 100 *:8080 *:*
LISTEN 0 1 [::ffff:127.0.0.1]:8005 *:*
"""
        changed = """State  Recv-Q Send-Q Local Address:Port Peer Address:Port
LISTEN 0 4096 127.0.0.11:46209 0.0.0.0:*
LISTEN 0 100 *:8081 *:*
LISTEN 0 1 [::ffff:127.0.0.1]:8005 *:*
"""
        self.assertEqual(
            poller.stable_ports_signature(first),
            poller.stable_ports_signature(second),
        )
        self.assertNotEqual(
            poller.stable_ports_signature(first),
            poller.stable_ports_signature(changed),
        )
