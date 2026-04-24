"""Integration tests for the FastAPI admin and participant routes.

Imports the ``app`` module with the ``Jinja2Templates`` patched to a dummy
implementation so template loading does not require the real template tree.
The module-level ``db``, ``runtime``, and ``ssh_pool`` are rebound to
per-test instances so every test starts from an isolated SQLite file.
"""
from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from config import SETTINGS
from db import Database
from scheduler import RefereeRuntime

from tests.conftest import DummyScheduler, DummySSH, DummyTemplates, snapshot


pytestmark = pytest.mark.integration


class ApiEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        original_admin_key = SETTINGS.admin_api_key
        object.__setattr__(SETTINGS, "admin_api_key", "test-admin-key")
        self.addCleanup(lambda: object.__setattr__(SETTINGS, "admin_api_key", original_admin_key))

        fd, raw_path = tempfile.mkstemp(suffix=".db")
        import os

        os.close(fd)
        db_path = Path(raw_path)
        self.addCleanup(lambda: db_path.exists() and db_path.unlink())

        db = Database(db_path)
        db.initialize()
        self.addCleanup(db.close)
        runtime = RefereeRuntime(db, DummySSH())
        runtime.scheduler = DummyScheduler()
        runtime.start_scheduler = Mock()
        runtime.shutdown = Mock()

        sys.modules.pop("app", None)
        with patch("fastapi.templating.Jinja2Templates", DummyTemplates):
            app_module = importlib.import_module("app")
        self.app_module = app_module
        self.original_db = app_module.db
        self.original_runtime = app_module.runtime
        self.original_ssh_pool = app_module.ssh_pool
        app_module.db = db
        app_module.runtime = runtime
        app_module.ssh_pool = runtime.ssh_pool
        self.addCleanup(self._restore_app_globals)

        self.client = TestClient(app_module.app)
        self.addCleanup(self.client.close)
        self.participant_client = TestClient(app_module.participant_app)
        self.addCleanup(self.participant_client.close)

    def _restore_app_globals(self) -> None:
        self.app_module.db = self.original_db
        self.app_module.runtime = self.original_runtime
        self.app_module.ssh_pool = self.original_ssh_pool

    def test_runtime_endpoint_returns_extended_state(self) -> None:
        self.app_module.app.dependency_overrides[self.app_module.require_admin_api_key] = lambda: None
        self.addCleanup(self.app_module.app.dependency_overrides.clear)
        validated_at = datetime.now(UTC).isoformat()
        self.app_module.db.set_competition_state(
            status="faulted",
            current_series=3,
            previous_series=2,
            fault_reason="rotation failed",
            last_validated_series=2,
            last_validated_at=validated_at,
        )
        self.app_module.runtime.scheduler.add_job(
            lambda: None,
            "interval",
            id="poll",
            replace_existing=True,
            max_instances=1,
            seconds=30,
        )
        self.app_module.runtime.scheduler.add_job(
            lambda: None,
            "date",
            id="rotate",
            replace_existing=True,
            max_instances=1,
            run_date=datetime.now(UTC) + timedelta(minutes=1),
        )

        response = self.client.get("/api/runtime")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["competition_status"], "faulted")
        self.assertEqual(payload["current_series"], 3)
        self.assertEqual(payload["previous_series"], 2)
        self.assertEqual(payload["fault_reason"], "rotation failed")
        self.assertEqual(payload["last_validated_series"], 2)
        self.assertIn("poll", payload["active_jobs"])
        self.assertIn("rotate", payload["active_jobs"])

    def test_status_endpoint_filters_stale_container_hosts(self) -> None:
        self.app_module.app.dependency_overrides[self.app_module.require_admin_api_key] = lambda: None
        self.addCleanup(self.app_module.app.dependency_overrides.clear)
        valid_host = self.app_module.SETTINGS.node_hosts[0]
        self.app_module.db.upsert_container_status(
            machine_host="10.0.0.9",
            variant="A",
            container_id="stale",
            series=5,
            status="running",
            king="unclaimed",
            king_mtime_epoch=1,
            last_checked=datetime.now(UTC).isoformat(),
        )
        self.app_module.db.upsert_container_status(
            machine_host=valid_host,
            variant="A",
            container_id="fresh",
            series=5,
            status="running",
            king="unclaimed",
            king_mtime_epoch=1,
            last_checked=datetime.now(UTC).isoformat(),
        )
        self.app_module.db.set_competition_state(status="running", current_series=5)

        response = self.client.get("/api/status")

        self.assertEqual(response.status_code, 200)
        hosts = {item["machine_host"] for item in response.json()["containers"]}
        self.assertNotIn("10.0.0.9", hosts)
        self.assertEqual(hosts, {valid_host})

    def test_runtime_endpoint_requires_admin_key(self) -> None:
        response = self.client.get("/api/runtime")
        self.assertEqual(response.status_code, 401)

    def test_status_endpoint_requires_admin_key(self) -> None:
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 401)

    def test_poll_endpoint_requires_admin_key_and_cannot_award_points(self) -> None:
        self.app_module.db.upsert_team_names(["Team Alpha"])
        self.app_module.db.set_competition_state(status="running", current_series=1)
        self.app_module.runtime.poller.run_cycle = Mock(
            return_value=(
                [
                    snapshot(node_host="192.168.0.102", variant="A", king="Team Alpha", king_mtime_epoch=1000),
                    snapshot(node_host="192.168.0.103", variant="A", king="Team Alpha", king_mtime_epoch=1010),
                    snapshot(node_host="192.168.0.106", variant="A", king="Team Alpha", king_mtime_epoch=1020),
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
        self.app_module.runtime.poll_once = Mock(wraps=self.app_module.runtime.poll_once)

        response = self.client.post("/api/poll")

        self.assertEqual(response.status_code, 401)
        self.app_module.runtime.poll_once.assert_not_called()
        self.assertEqual(self.app_module.db.get_team("Team Alpha")["total_points"], 0.0)
        self.assertEqual(self.app_module.db.get_competition()["poll_cycle"], 0)
        with self.app_module.db._lock:  # noqa: SLF001 - test verifies DB side effects
            point_count = self.app_module.db._conn.execute("SELECT COUNT(*) FROM point_events").fetchone()[0]
        self.assertEqual(point_count, 0)

    def test_dashboard_route_renders_template(self) -> None:
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)

    def test_participant_dashboard_route_renders_template(self) -> None:
        response = self.participant_client.get("/")

        self.assertEqual(response.status_code, 200)

    def test_participant_leaderboard_route_renders_template(self) -> None:
        response = self.participant_client.get("/leaderboard")

        self.assertEqual(response.status_code, 200)

    def test_lb_endpoint_parses_frontend_backend_haproxy_config(self) -> None:
        self.app_module.app.dependency_overrides[self.app_module.require_admin_api_key] = lambda: None
        self.addCleanup(self.app_module.app.dependency_overrides.clear)
        cfg = """
frontend h1a
  bind *:10001
  default_backend h1a_nodes
backend h1a_nodes
  balance roundrobin
  server n1 192.168.0.70:10001 check
  server n2 192.168.0.103:10001 check
  server n3 192.168.0.106:10001 check
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            haproxy_cfg = Path(tmpdir) / "haproxy.cfg"
            haproxy_cfg.write_text(cfg, encoding="utf-8")
            previous_path = self.app_module.HAPROXY_CONFIG_PATH
            self.app_module.HAPROXY_CONFIG_PATH = haproxy_cfg
            try:
                response = self.client.get("/api/lb")
            finally:
                self.app_module.HAPROXY_CONFIG_PATH = previous_path

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["configured"])
        self.assertEqual(len(payload["services"]), 1)
        self.assertEqual(payload["services"][0]["name"], "h1a")
        self.assertEqual(payload["services"][0]["bind_port"], 10001)
        self.assertEqual(len(payload["services"][0]["servers"]), 3)

    def test_lb_endpoint_requires_admin_key(self) -> None:
        response = self.client.get("/api/lb")
        self.assertEqual(response.status_code, 401)

    def test_routing_and_telemetry_endpoints_require_admin_key(self) -> None:
        self.assertEqual(self.client.get("/api/routing").status_code, 401)
        self.assertEqual(self.client.get("/api/telemetry").status_code, 401)

    def test_routing_endpoint_returns_active_listener_view(self) -> None:
        self.app_module.app.dependency_overrides[self.app_module.require_admin_api_key] = lambda: None
        self.addCleanup(self.app_module.app.dependency_overrides.clear)
        payload = self.app_module.RoutingStatusResponse(
            configured=True,
            current_series=6,
            services=[
                self.app_module.RoutingServiceResponse(
                    name="p10050",
                    bind_port=10050,
                    variant="A",
                    inbound_connections=12,
                    backend_connections=9,
                    routing_text="n1 192.168.0.70:10050 [UP] -> n2 192.168.0.103:10050 [UP]",
                    servers=[
                        self.app_module.RoutingServerResponse(
                            name="n1",
                            host="192.168.0.70",
                            port=10050,
                            status="UP",
                            check_status="L4OK",
                            active_connections=5,
                            last_change_seconds=12,
                        ),
                        self.app_module.RoutingServerResponse(
                            name="n2",
                            host="192.168.0.103",
                            port=10050,
                            status="UP",
                            check_status="L4OK",
                            active_connections=4,
                            last_change_seconds=12,
                        ),
                    ],
                )
            ],
            total_inbound_connections=12,
            total_backend_connections=9,
            note=None,
        )

        with patch.object(self.app_module, "_routing_status", return_value=payload):
            response = self.client.get("/api/routing")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["current_series"], 6)
        self.assertEqual(body["services"][0]["variant"], "A")
        self.assertEqual(body["services"][0]["servers"][0]["status"], "UP")

    def test_telemetry_endpoint_returns_host_and_container_data(self) -> None:
        self.app_module.app.dependency_overrides[self.app_module.require_admin_api_key] = lambda: None
        self.addCleanup(self.app_module.app.dependency_overrides.clear)
        payload = self.app_module.TelemetryStatusResponse(
            current_series=2,
            generated_at=datetime.now(UTC),
            hosts=[
                self.app_module.HostTelemetryResponse(
                    host="192.168.0.12",
                    role="lb",
                    reachable=True,
                    loadavg_1m=0.15,
                    loadavg_5m=0.20,
                    loadavg_15m=0.25,
                    mem_used_mb=1024,
                    mem_total_mb=4096,
                    mem_percent=25.0,
                    disk_used_gb=40.0,
                    disk_total_gb=128.0,
                    disk_percent=31.3,
                    uptime_seconds=3600,
                    docker_status="active",
                    haproxy_status="active",
                    referee_status="active",
                    error=None,
                )
            ],
            containers=[
                self.app_module.ContainerTelemetryResponse(
                    machine_host="192.168.0.70",
                    variant="A",
                    container_id="H2A_Node1",
                    series=2,
                    status="running",
                    health="healthy",
                    king="Team Alpha",
                    cpu_percent=1.2,
                    memory_usage="12MiB / 4GiB",
                    memory_percent=0.3,
                    pids=7,
                    restart_count=1,
                    started_at="2026-04-18T12:00:00Z",
                    finished_at=None,
                    exit_code=0,
                    oom_killed=False,
                    uptime_seconds=120,
                    downtime_seconds=8,
                    error=None,
                )
            ],
            note=None,
        )

        with patch.object(self.app_module, "_telemetry_status", return_value=payload):
            response = self.client.get("/api/telemetry")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["current_series"], 2)
        self.assertEqual(body["hosts"][0]["haproxy_status"], "active")
        self.assertEqual(body["containers"][0]["container_id"], "H2A_Node1")

    def test_logs_and_claims_endpoints_require_admin_key(self) -> None:
        self.assertEqual(self.client.get("/api/logs/referee").status_code, 401)
        self.assertEqual(self.client.get("/api/logs/haproxy").status_code, 401)
        self.assertEqual(self.client.get("/api/claims").status_code, 401)

    def test_teams_and_events_endpoints_require_admin_key(self) -> None:
        self.assertEqual(self.client.get("/api/teams").status_code, 401)
        self.assertEqual(self.client.get("/api/events").status_code, 401)
        self.assertEqual(self.client.post("/api/admin/teams", json={"name": "Team Alpha"}).status_code, 401)

    def test_recover_validate_endpoint_requires_admin_key(self) -> None:
        response = self.client.post("/api/recover/validate")
        self.assertEqual(response.status_code, 401)

    def test_recover_validate_endpoint_returns_summary(self) -> None:
        self.app_module.app.dependency_overrides[self.app_module.require_admin_api_key] = lambda: None
        self.addCleanup(self.app_module.app.dependency_overrides.clear)
        self.app_module.db.set_competition_state(status="paused", current_series=1)
        self.app_module.runtime.poller.run_cycle = Mock(
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

        response = self.client.post(
            "/api/recover/validate",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["valid"])
        self.assertTrue(payload["complete_snapshot_matrix"])
        self.assertEqual(payload["healthy_nodes"], 3)
        self.assertEqual(payload["total_nodes"], 3)
        self.assertEqual(payload["min_healthy_nodes"], 2)
        self.assertEqual(payload["healthy_counts_by_variant"]["A"], 3)

    def test_claims_endpoint_returns_observations(self) -> None:
        self.app_module.app.dependency_overrides[self.app_module.require_admin_api_key] = lambda: None
        self.addCleanup(self.app_module.app.dependency_overrides.clear)
        self.app_module.db.add_claim_observations(
            [
                {
                    "poll_cycle": 7,
                    "series": 5,
                    "node_host": "192.168.0.70",
                    "variant": "A",
                    "status": "running",
                    "king": "Team Alpha",
                    "king_mtime_epoch": 1234,
                    "observed_at": datetime.now(UTC).isoformat(),
                    "selected": True,
                    "selection_reason": "earliest_quorum",
                }
            ]
        )

        response = self.client.get("/api/claims?limit=10")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload), 1)
        self.assertTrue(payload[0]["selected"])
        self.assertEqual(payload[0]["selection_reason"], "earliest_quorum")

    def test_log_endpoints_return_tail(self) -> None:
        self.app_module.app.dependency_overrides[self.app_module.require_admin_api_key] = lambda: None
        self.addCleanup(self.app_module.app.dependency_overrides.clear)
        with tempfile.TemporaryDirectory() as tmpdir:
            referee_log = Path(tmpdir) / "referee.log"
            haproxy_log = Path(tmpdir) / "haproxy.log"
            referee_log.write_text("a\nb\nc\n", encoding="utf-8")
            haproxy_log.write_text("x\ny\n", encoding="utf-8")
            original_referee = self.app_module.SETTINGS.referee_log_path
            original_haproxy = self.app_module.SETTINGS.haproxy_log_path
            object.__setattr__(self.app_module.SETTINGS, "referee_log_path", referee_log)
            object.__setattr__(self.app_module.SETTINGS, "haproxy_log_path", haproxy_log)
            try:
                referee_response = self.client.get("/api/logs/referee?lines=2")
                haproxy_response = self.client.get("/api/logs/haproxy?lines=1")
            finally:
                object.__setattr__(self.app_module.SETTINGS, "referee_log_path", original_referee)
                object.__setattr__(self.app_module.SETTINGS, "haproxy_log_path", original_haproxy)

        self.assertEqual(referee_response.status_code, 200)
        self.assertEqual(referee_response.json()["lines"], ["b", "c"])
        self.assertEqual(haproxy_response.status_code, 200)
        self.assertEqual(haproxy_response.json()["lines"], ["y"])

    def test_team_admin_endpoint_rejects_invalid_claim_names(self) -> None:
        self.app_module.app.dependency_overrides[self.app_module.require_admin_api_key] = lambda: None
        self.addCleanup(self.app_module.app.dependency_overrides.clear)

        response = self.client.post("/api/admin/teams", json={"name": "unclaimed"})

        self.assertEqual(response.status_code, 422)
        self.assertIn("valid claim", response.json()["detail"])

    def test_team_admin_endpoints_create_ban_and_unban(self) -> None:
        self.app_module.app.dependency_overrides[self.app_module.require_admin_api_key] = lambda: None
        self.addCleanup(self.app_module.app.dependency_overrides.clear)

        create_response = self.client.post("/api/admin/teams", json={"name": "Team Alpha"})
        self.assertEqual(create_response.status_code, 200)
        self.assertEqual(create_response.json()["status"], "active")

        ban_response = self.client.post("/api/admin/teams/Team%20Alpha/ban")
        self.assertEqual(ban_response.status_code, 200)
        self.assertEqual(ban_response.json()["status"], "banned")

        self.app_module.db.increment_team_offense("Team Alpha")
        unban_response = self.client.post("/api/admin/teams/Team%20Alpha/unban")
        self.assertEqual(unban_response.status_code, 200)
        self.assertEqual(unban_response.json()["status"], "active")
        self.assertEqual(unban_response.json()["offense_count"], 0)

    def test_public_dashboard_endpoint_returns_derived_defaults(self) -> None:
        self.app_module.db.set_competition_state(status="running", current_series=2)
        cfg = """
listen p10010
  bind *:10010
  server n1 192.168.0.70:10010 check
listen p10011
  bind *:10011
  server n1 192.168.0.70:10011 check
listen p10012
  bind *:10012
  server n1 192.168.0.70:10012 check
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            haproxy_cfg = Path(tmpdir) / "haproxy.cfg"
            haproxy_cfg.write_text(cfg, encoding="utf-8")
            previous_path = self.app_module.HAPROXY_CONFIG_PATH
            self.app_module.HAPROXY_CONFIG_PATH = haproxy_cfg
            try:
                response = self.participant_client.get(
                    "/api/public/dashboard",
                    headers={"host": "172.21.0.13:9000"},
                )
            finally:
                self.app_module.HAPROXY_CONFIG_PATH = previous_path

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["competition_status"], "running")
        self.assertEqual(payload["current_series"], 2)
        self.assertEqual(payload["orchestrator_host"], "172.21.0.13")
        self.assertEqual(payload["port_ranges"], "10010-10012")
        self.assertEqual(payload["headline"], "Current Access Window")
        self.assertEqual(response.headers["cache-control"], "no-cache, max-age=0, must-revalidate")

    def test_public_leaderboard_endpoint_returns_ranked_participant_safe_rows(self) -> None:
        polled_at = datetime(2026, 4, 20, 11, 15, tzinfo=UTC).isoformat()
        self.app_module.db.set_competition_state(
            status="running",
            current_series=4,
            last_poll_at=polled_at,
        )
        self.app_module.db.upsert_team_names(["Team Alpha", "Team Beta", "Team Gamma"])
        self.app_module.db.add_points("Team Beta", "A", 4, 6.0, 1)
        self.app_module.db.add_points("Team Alpha", "B", 4, 9.5, 1)
        self.app_module.db.set_team_status("Team Gamma", status="warned", offense_count=1)

        response = self.participant_client.get("/api/public/leaderboard")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-cache, max-age=0, must-revalidate")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        payload = response.json()
        self.assertEqual(payload["competition_status"], "running")
        self.assertEqual(payload["current_series"], 4)
        self.assertEqual(
            datetime.fromisoformat(payload["updated_at"].replace("Z", "+00:00")),
            datetime.fromisoformat(polled_at),
        )
        self.assertEqual(payload["scoring_interval_seconds"], self.app_module.SETTINGS.poll_interval_seconds)
        self.assertEqual(payload["refresh_interval_seconds"], 5)
        self.assertEqual(
            payload["teams"],
            [
                {"rank": 1, "name": "Team Alpha", "total_points": 9.5},
                {"rank": 2, "name": "Team Beta", "total_points": 6.0},
                {"rank": 3, "name": "Team Gamma", "total_points": 0.0},
            ],
        )

    def test_increment_poll_cycle_updates_last_poll_at(self) -> None:
        before = datetime.now(UTC)

        poll_cycle = self.app_module.db.increment_poll_cycle()

        self.assertEqual(poll_cycle, 1)
        state = self.app_module.db.get_competition()
        self.assertIsNotNone(state["last_poll_at"])
        self.assertGreaterEqual(datetime.fromisoformat(state["last_poll_at"]), before)

    def test_admin_public_config_and_notifications_flow(self) -> None:
        self.app_module.app.dependency_overrides[self.app_module.require_admin_api_key] = lambda: None
        self.addCleanup(self.app_module.app.dependency_overrides.clear)
        self.app_module.db.upsert_team_names(["Team Alpha"])
        self.app_module.db.add_points("Team Alpha", "A", 2, 1.0, 7)

        config_response = self.client.put(
            "/api/admin/public/config",
            json={
                "orchestrator_host": "172.21.0.13",
                "port_ranges": "10010-10012",
                "headline": "Join Here",
                "subheadline": "Use these ports for the active wave.",
            },
        )
        self.assertEqual(config_response.status_code, 200)
        self.assertEqual(config_response.json()["orchestrator_host"], "172.21.0.13")

        create_response = self.client.post(
            "/api/admin/public/notifications",
            json={"message": "H2 is live now", "severity": "warning"},
        )
        self.assertEqual(create_response.status_code, 200)
        notification_id = create_response.json()["id"]

        list_response = self.client.get("/api/admin/public/notifications")
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(len(list_response.json()), 1)

        public_response = self.participant_client.get("/api/public/dashboard")
        self.assertEqual(public_response.status_code, 200)
        public_payload = public_response.json()
        self.assertEqual(public_payload["orchestrator_host"], "172.21.0.13")
        self.assertEqual(public_payload["port_ranges"], "10010-10012")
        self.assertEqual(public_payload["notifications"][0]["message"], "H2 is live now")
        self.assertEqual(public_payload["teams"][0]["name"], "Team Alpha")
        self.assertEqual(public_payload["leaderboard_series"][0]["team_name"], "Team Alpha")
        self.assertEqual(public_payload["leaderboard_series"][0]["points"][0]["total_points"], 1.0)

        delete_response = self.client.delete(f"/api/admin/public/notifications/{notification_id}")
        self.assertEqual(delete_response.status_code, 200)
        self.assertEqual(delete_response.json()["ok"], True)

    def test_recover_redeploy_endpoint_returns_paused_recovery_result(self) -> None:
        self.app_module.app.dependency_overrides[self.app_module.require_admin_api_key] = lambda: None
        self.addCleanup(self.app_module.app.dependency_overrides.clear)
        self.app_module.db.set_competition_state(status="faulted", current_series=2, fault_reason="broken")
        self.app_module.runtime._run_compose_parallel = Mock(return_value={})
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
        self.app_module.runtime.poller.run_cycle = Mock(return_value=(healthy_snapshots, {}))

        response = self.client.post("/api/recover/redeploy")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["competition_status"], "paused")
        self.assertEqual(payload["current_series"], 2)
        self.assertIsNone(payload["fault_reason"])

    def test_recover_redeploy_endpoint_surfaces_guard_error(self) -> None:
        self.app_module.app.dependency_overrides[self.app_module.require_admin_api_key] = lambda: None
        self.addCleanup(self.app_module.app.dependency_overrides.clear)
        self.app_module.db.set_competition_state(status="running", current_series=2)

        response = self.client.post("/api/recover/redeploy")

        self.assertEqual(response.status_code, 409)
        self.assertIn("paused or faulted", response.json()["detail"])
