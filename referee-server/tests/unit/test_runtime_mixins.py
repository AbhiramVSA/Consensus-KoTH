"""Direct unit tests for the three RefereeRuntime mixins.

The pre-split scheduler.py was 1,545 lines of one class. The split
extracted three mixins:

* ComposeOpsMixin (_runtime_compose.py)        — docker compose remote ops
* HaproxyOpsMixin (_runtime_haproxy.py)        — HAProxy admin socket
* BaselineMixin   (_runtime_baselines.py)      — baseline + health checks

Most behavior of these mixins is exercised end-to-end through the
existing lifecycle / API integration tests. This file targets the
shapes that those tests do NOT pin directly: pure helpers, parser
boundaries, and the per-method failure paths.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from _runtime_baselines import BaselineMixin
from _runtime_compose import PORT_BIND_RE, ComposeOpsMixin
from _runtime_haproxy import HaproxyOpsMixin, LISTEN_NAME_RE
from db import Database
from poller import VariantSnapshot
from scheduler import RefereeRuntime
from tests.conftest import DummySSH


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_runtime(tc: unittest.TestCase) -> tuple[RefereeRuntime, Database]:
    fd, raw_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db_path = Path(raw_path)
    db = Database(db_path)
    db.initialize()
    tc.addCleanup(lambda: db_path.exists() and db_path.unlink())
    tc.addCleanup(db.close)
    return RefereeRuntime(db, DummySSH()), db


def _snap(
    *,
    host: str = "192.168.0.102",
    variant: str = "A",
    king: str | None = "unclaimed",
    status: str = "running",
    sections: dict[str, str] | None = None,
) -> VariantSnapshot:
    return VariantSnapshot(
        node_host=host,
        variant=variant,
        king=king,
        king_mtime_epoch=1000,
        status=status,
        sections=sections or {},
        checked_at=datetime.now(UTC),
    )


# ===========================================================================
# Mixin contract: RefereeRuntime inherits from all three.
# ===========================================================================
def test_referee_runtime_inherits_from_every_mixin() -> None:
    assert issubclass(RefereeRuntime, ComposeOpsMixin)
    assert issubclass(RefereeRuntime, HaproxyOpsMixin)
    assert issubclass(RefereeRuntime, BaselineMixin)


def test_method_resolution_routes_through_mixins() -> None:
    # Symbol resolution check: each method that moved must still be
    # accessible as a bound method on a RefereeRuntime instance, and
    # ``inspect.getmodule`` of that method's underlying function must
    # point at the mixin module — not at scheduler.py.
    import inspect

    class _Probe(unittest.TestCase):
        pass

    tc = _Probe()
    runtime, _db = _make_runtime(tc)
    try:
        compose_module = inspect.getmodule(runtime._run_compose_parallel)
        haproxy_module = inspect.getmodule(runtime._haproxy_listeners)
        baselines_module = inspect.getmodule(runtime._capture_baselines)
        assert compose_module is not None and "compose" in compose_module.__name__
        assert haproxy_module is not None and "haproxy" in haproxy_module.__name__
        assert baselines_module is not None and "baselines" in baselines_module.__name__
    finally:
        for cleanup in tc._cleanups[::-1]:
            cleanup[0](*cleanup[1:][0], **cleanup[1:][1])


# ===========================================================================
# ComposeOpsMixin
# ===========================================================================
class ComposeOpsMixinTests(unittest.TestCase):
    def test_port_bind_regex_matches_simple_tcp(self) -> None:
        match = PORT_BIND_RE.match('  - "10001:80"')
        assert match is not None
        assert match.group("host") == "10001"

    def test_port_bind_regex_matches_with_protocol_suffix(self) -> None:
        # H7A's docker-compose.yml has UDP bindings like ``- "10060:161/udp"``.
        match = PORT_BIND_RE.match('  - "10060:161/udp"')
        assert match is not None
        assert match.group("host") == "10060"

    def test_port_bind_regex_skips_non_quoted_lines(self) -> None:
        assert PORT_BIND_RE.match("  - 10001:80") is None

    def test_run_compose_on_node_returns_failure_tuple_on_ssh_exception(self) -> None:
        runtime, _db = _make_runtime(self)
        runtime.ssh_pool.exec = Mock(side_effect=RuntimeError("SSH down"))

        host, ok, output = runtime._run_compose_on_node("192.168.0.102", 1, "ps")

        self.assertEqual(host, "192.168.0.102")
        self.assertFalse(ok)
        self.assertIn("SSH down", output)

    def test_run_compose_on_node_marks_failure_on_nonzero_exit(self) -> None:
        runtime, _db = _make_runtime(self)
        runtime.ssh_pool.exec = Mock(return_value=(1, "", "service not found"))

        host, ok, output = runtime._run_compose_on_node("192.168.0.102", 1, "ps")

        self.assertFalse(ok)
        self.assertIn("service not found", output)

    def test_run_compose_on_node_marks_success_on_zero_exit(self) -> None:
        runtime, _db = _make_runtime(self)
        runtime.ssh_pool.exec = Mock(return_value=(0, "OK\n", ""))

        host, ok, output = runtime._run_compose_on_node("192.168.0.102", 1, "ps")

        self.assertTrue(ok)
        self.assertEqual(output, "OK\n")

    # ``test_run_compose_parallel_returns_empty_when_no_node_hosts``
    # is a module-level pytest function below, because pytest fixtures
    # (settings_override) are not injected into unittest.TestCase methods.

    def test_series_public_ports_caches_after_first_read(self) -> None:
        runtime, _db = _make_runtime(self)

        first = runtime._series_public_ports(1)
        # Second call must hit the cache, not the file system. Force
        # the cache to a known value to detect re-reads.
        runtime._series_port_cache[1] = (99999,)
        second = runtime._series_public_ports(1)
        self.assertEqual(second, (99999,))
        # Sanity: the original parse produced something non-empty when
        # the H1 compose file exists in the repo.
        self.assertIsInstance(first, tuple)


# ===========================================================================
# HaproxyOpsMixin
# ===========================================================================
class HaproxyOpsMixinTests(unittest.TestCase):
    def test_listen_name_regex_extracts_listener_name(self) -> None:
        match = LISTEN_NAME_RE.match("listen p10001")
        assert match is not None
        self.assertEqual(match.group(1), "p10001")

    def test_listen_name_regex_skips_unrelated_lines(self) -> None:
        self.assertIsNone(LISTEN_NAME_RE.match("frontend f10001"))
        self.assertIsNone(LISTEN_NAME_RE.match("  bind *:10001"))

    # The HAProxy tests that need ``settings_override`` are module-level
    # pytest functions below — pytest fixtures do not inject into
    # unittest.TestCase methods.

    def test_haproxy_listeners_uses_cached_value_alone(self) -> None:
        runtime, _db = _make_runtime(self)
        runtime._haproxy_listener_cache = {"cached_listener"}
        # No settings swap needed — the cache short-circuits before
        # the config path is consulted.
        self.assertEqual(runtime._haproxy_listeners(), {"cached_listener"})


# ===========================================================================
# BaselineMixin
# ===========================================================================
class BaselineMixinShapeTests(unittest.TestCase):
    # ``test_expected_snapshot_pairs_is_full_cross_product`` and
    # ``test_snapshot_matrix_issues_flags_unexpected`` are module-level
    # pytest functions below.

    def test_snapshot_matrix_issues_flags_missing(self) -> None:
        runtime, _db = _make_runtime(self)
        # Default fixture: 3 hosts x 3 variants = 9 expected pairs.
        # Provide only one snapshot.
        snapshots = [_snap(host="192.168.0.102", variant="A")]
        issues = runtime._snapshot_matrix_issues(snapshots)
        self.assertEqual(len(issues), 1)
        self.assertTrue(issues[0].startswith("missing snapshots:"))

    def test_snapshot_matrix_issues_empty_for_full_matrix(self) -> None:
        runtime, _db = _make_runtime(self)
        snapshots = [
            _snap(host=host, variant=variant)
            for host in ("192.168.0.102", "192.168.0.103", "192.168.0.106")
            for variant in ("A", "B", "C")
        ]
        self.assertEqual(runtime._snapshot_matrix_issues(snapshots), [])

    def test_running_snapshot_counts_by_variant(self) -> None:
        runtime, _db = _make_runtime(self)
        snapshots = [
            _snap(host="h1", variant="A", status="running"),
            _snap(host="h2", variant="A", status="running"),
            _snap(host="h3", variant="A", status="degraded"),
            _snap(host="h1", variant="B", status="failed"),
        ]
        counts = runtime._running_snapshot_counts_by_variant(snapshots)
        # Only the two running A-variants count; B has zero running.
        self.assertEqual(counts["A"], 2)
        self.assertEqual(counts["B"], 0)

    def test_healthy_running_host_count_dedupes_by_host(self) -> None:
        runtime, _db = _make_runtime(self)
        snapshots = [
            _snap(host="192.168.0.102", variant="A", status="running"),
            _snap(host="192.168.0.102", variant="B", status="running"),
            _snap(host="192.168.0.103", variant="A", status="degraded"),
        ]
        # Three snapshots, two distinct hosts, one of which is degraded
        # so only .102 counts.
        self.assertEqual(runtime._healthy_running_host_count(snapshots), 1)

    def test_evaluate_series_health_collects_issues_per_axis(self) -> None:
        runtime, _db = _make_runtime(self)
        snapshots = [
            _snap(host="192.168.0.102", variant="A", status="running", king="unclaimed"),
            _snap(host="192.168.0.103", variant="A", status="failed"),
            _snap(host="192.168.0.106", variant="A", status="running", king="rogue"),
        ]
        deploy_results = {"192.168.0.106": (False, "boom")}
        issues = runtime._evaluate_series_health(
            series=1, snapshots=snapshots, deploy_results=deploy_results
        )
        joined = " | ".join(issues)
        self.assertIn("missing snapshots", joined)  # B and C absent
        self.assertIn("deploy command failed", joined)
        self.assertIn("status=failed", joined)
        self.assertIn("king.txt='rogue'", joined)


class CaptureBaselinesTests(unittest.TestCase):
    def test_capture_baselines_writes_one_row_per_snapshot(self) -> None:
        runtime, db = _make_runtime(self)
        snapshots = [
            _snap(
                host="192.168.0.102",
                variant="A",
                sections={
                    "SHADOW": "abcdef" * 10 + "abcd  /etc/shadow",
                    "AUTHKEYS": "fedcba" * 10 + "abcd  /root/.ssh/authorized_keys",
                    "IPTABLES": "Chain INPUT (policy ACCEPT)\n",
                    "PORTS": "State\nLISTEN 0 100 *:8080 *:*\n",
                },
            ),
            _snap(host="192.168.0.103", variant="A"),
        ]
        runtime._capture_baselines(1, snapshots)
        for host in ("192.168.0.102", "192.168.0.103"):
            row = db.get_baseline(machine_host=host, variant="A", series=1)
            self.assertIsNotNone(row, host)


class MarkClockDriftTests(unittest.TestCase):
    def test_mark_clock_drift_returns_empty_with_too_few_epochs(self) -> None:
        runtime, _db = _make_runtime(self)
        runtime._log_event_and_webhook = Mock()
        # Only one parsable epoch; statistics.median needs at least 2
        # for the comparison to be meaningful, so we return early.
        snapshots = [_snap(host="h1", sections={"NODE_EPOCH": "1000"})]
        result = runtime._mark_clock_drift_degraded(series=1, snapshots=snapshots)
        self.assertEqual(result, set())
        runtime._log_event_and_webhook.assert_not_called()

    def test_mark_clock_drift_ignores_epoch_fail(self) -> None:
        runtime, _db = _make_runtime(self)
        runtime._log_event_and_webhook = Mock()
        # All probes failed to read the clock; nothing to compare.
        snapshots = [
            _snap(host="h1", sections={"NODE_EPOCH": "EPOCH_FAIL"}),
            _snap(host="h2", sections={"NODE_EPOCH": "EPOCH_FAIL"}),
        ]
        self.assertEqual(
            runtime._mark_clock_drift_degraded(series=1, snapshots=snapshots), set()
        )

    def test_mark_clock_drift_ignores_unparseable_epoch(self) -> None:
        runtime, _db = _make_runtime(self)
        runtime._log_event_and_webhook = Mock()
        snapshots = [
            _snap(host="h1", sections={"NODE_EPOCH": "not-a-number"}),
            _snap(host="h2", sections={"NODE_EPOCH": "1000"}),
        ]
        self.assertEqual(
            runtime._mark_clock_drift_degraded(series=1, snapshots=snapshots), set()
        )


class LogSeriesHealthTests(unittest.TestCase):
    def test_log_series_health_emits_critical_for_non_running(self) -> None:
        runtime, _db = _make_runtime(self)
        runtime._log_event_and_webhook = Mock()
        snapshots = [_snap(host="h1", status="failed")]

        runtime._log_series_health(series=1, snapshots=snapshots)

        runtime._log_event_and_webhook.assert_called_once()
        call_kwargs = runtime._log_event_and_webhook.call_args.kwargs
        self.assertEqual(call_kwargs["severity"], "critical")
        self.assertEqual(call_kwargs["evidence"]["status"], "failed")

    def test_log_series_health_emits_warning_for_lingering_king(self) -> None:
        runtime, _db = _make_runtime(self)
        runtime._log_event_and_webhook = Mock()
        snapshots = [_snap(host="h1", status="running", king="Team Alpha")]

        runtime._log_series_health(series=1, snapshots=snapshots)

        runtime._log_event_and_webhook.assert_called_once()
        call_kwargs = runtime._log_event_and_webhook.call_args.kwargs
        self.assertEqual(call_kwargs["severity"], "warning")
        self.assertEqual(call_kwargs["evidence"]["king"], "Team Alpha")

    def test_log_series_health_silent_for_healthy_unclaimed(self) -> None:
        runtime, _db = _make_runtime(self)
        runtime._log_event_and_webhook = Mock()
        snapshots = [_snap(host="h1", status="running", king="unclaimed")]

        runtime._log_series_health(series=1, snapshots=snapshots)

        runtime._log_event_and_webhook.assert_not_called()


# ===========================================================================
# RuntimeGuardError re-export
# ===========================================================================
def test_runtime_guard_error_re_exported_from_scheduler() -> None:
    # ``from scheduler import RuntimeGuardError`` must keep working
    # even though the class definition moved to scheduler_errors.py.
    from scheduler import RuntimeGuardError as schedule_error
    from scheduler_errors import RuntimeGuardError as canonical

    assert schedule_error is canonical
    assert issubclass(schedule_error, RuntimeError)


# ===========================================================================
# Module-level pytest tests for the methods that need ``settings_override``.
# Pytest fixtures do not inject into unittest.TestCase methods, so each
# fixture-using test lives at module scope and uses tmp_path / pytest's
# own helpers as needed.
# ===========================================================================
def test_run_compose_parallel_returns_empty_when_no_node_hosts(
    tmp_path: Path, settings_override
) -> None:
    db_path = tmp_path / "compose.db"
    db = Database(db_path)
    db.initialize()
    try:
        runtime = RefereeRuntime(db, DummySSH())
        with settings_override(node_hosts=()):
            assert runtime._run_compose_parallel(1, "ps") == {}
    finally:
        db.close()


def test_haproxy_server_name_maps_known_host(tmp_path: Path, settings_override) -> None:
    db = Database(tmp_path / "h.db")
    db.initialize()
    try:
        runtime = RefereeRuntime(db, DummySSH())
        with settings_override(
            node_hosts=("192.168.0.102", "192.168.0.103", "192.168.0.106")
        ):
            assert runtime._haproxy_server_name("192.168.0.102") == "n1"
            assert runtime._haproxy_server_name("192.168.0.103") == "n2"
            assert runtime._haproxy_server_name("192.168.0.106") == "n3"
    finally:
        db.close()


def test_haproxy_server_name_returns_none_for_unknown_host(
    tmp_path: Path, settings_override
) -> None:
    db = Database(tmp_path / "h.db")
    db.initialize()
    try:
        runtime = RefereeRuntime(db, DummySSH())
        with settings_override(node_hosts=("192.168.0.102",)):
            assert runtime._haproxy_server_name("10.0.0.42") is None
    finally:
        db.close()


def test_haproxy_listeners_returns_empty_when_config_missing(
    tmp_path: Path, settings_override
) -> None:
    db = Database(tmp_path / "h.db")
    db.initialize()
    try:
        runtime = RefereeRuntime(db, DummySSH())
        with settings_override(haproxy_config_path=Path("/no/such/haproxy.cfg")):
            runtime._haproxy_listener_cache = None
            assert runtime._haproxy_listeners() == set()
    finally:
        db.close()


def test_haproxy_listeners_parses_listen_blocks(
    tmp_path: Path, settings_override
) -> None:
    cfg = tmp_path / "haproxy.cfg"
    cfg.write_text(
        "listen p10001\n  bind *:10001\nlisten p10010\n  bind *:10010\n",
        encoding="utf-8",
    )
    db = Database(tmp_path / "h.db")
    db.initialize()
    try:
        runtime = RefereeRuntime(db, DummySSH())
        with settings_override(haproxy_config_path=cfg):
            runtime._haproxy_listener_cache = None
            listeners = runtime._haproxy_listeners()
        assert listeners == {"p10001", "p10010"}
    finally:
        db.close()


def test_sync_haproxy_active_series_is_noop_when_socket_missing(
    tmp_path: Path, settings_override
) -> None:
    db = Database(tmp_path / "h.db")
    db.initialize()
    try:
        runtime = RefereeRuntime(db, DummySSH())
        runtime._set_haproxy_series_state = Mock()  # type: ignore[method-assign]
        with settings_override(haproxy_admin_socket_path=tmp_path / "nope.sock"):
            runtime._sync_haproxy_active_series(1)
        runtime._set_haproxy_series_state.assert_not_called()
    finally:
        db.close()


def test_expected_snapshot_pairs_is_full_cross_product(
    tmp_path: Path, settings_override
) -> None:
    db = Database(tmp_path / "p.db")
    db.initialize()
    try:
        runtime = RefereeRuntime(db, DummySSH())
        with settings_override(node_hosts=("h1", "h2"), variants=("A", "B")):
            pairs = runtime._expected_snapshot_pairs()
        assert pairs == {("h1", "A"), ("h1", "B"), ("h2", "A"), ("h2", "B")}
    finally:
        db.close()


def test_snapshot_matrix_issues_flags_unexpected(
    tmp_path: Path, settings_override
) -> None:
    db = Database(tmp_path / "p.db")
    db.initialize()
    try:
        runtime = RefereeRuntime(db, DummySSH())
        with settings_override(node_hosts=("192.168.0.102",), variants=("A",)):
            snapshots = [
                _snap(host="192.168.0.102", variant="A"),
                _snap(host="rogue-host", variant="Z"),
            ]
            issues = runtime._snapshot_matrix_issues(snapshots)
        joined = " | ".join(issues)
        assert "unexpected snapshots:" in joined
        assert "rogue-host/Z" in joined
    finally:
        db.close()
