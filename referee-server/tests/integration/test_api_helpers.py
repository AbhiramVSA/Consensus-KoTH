"""Targeted coverage for app.py helpers and the state-control endpoints.

These are the small pure functions and the lifecycle-control routes that
the route-level tests in ``test_api.py`` did not exercise. The module
imports the ``app`` module via the same fixture helper so we can call
the underscore-prefixed helpers directly through ``app_module.<name>``
without re-implementing the Jinja / SETTINGS swap.
"""
from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

from tests.integration.test_api import (
    _install_api_test_fixture,
    _override_admin_auth,
)


pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Pure parser helpers
# ---------------------------------------------------------------------------
class ParseEndpointPortTests(unittest.TestCase):
    def setUp(self) -> None:
        _install_api_test_fixture(self)

    def test_ipv4_endpoint(self) -> None:
        self.assertEqual(self.app_module._parse_endpoint_port("192.168.0.1:8080"), 8080)

    def test_ipv6_bracketed_endpoint(self) -> None:
        self.assertEqual(self.app_module._parse_endpoint_port("[::1]:9090"), 9090)

    def test_no_port_returns_none(self) -> None:
        self.assertIsNone(self.app_module._parse_endpoint_port("192.168.0.1"))

    def test_non_numeric_port_returns_none(self) -> None:
        self.assertIsNone(self.app_module._parse_endpoint_port("192.168.0.1:abc"))

    def test_ipv6_without_closing_bracket_returns_none(self) -> None:
        self.assertIsNone(self.app_module._parse_endpoint_port("[::1"))

    def test_ipv6_non_numeric_port_returns_none(self) -> None:
        self.assertIsNone(self.app_module._parse_endpoint_port("[::1]:notaport"))


class ParseEndpointHostPortTests(unittest.TestCase):
    def setUp(self) -> None:
        _install_api_test_fixture(self)

    def test_ipv4(self) -> None:
        self.assertEqual(
            self.app_module._parse_endpoint_host_port("192.168.0.1:8080"),
            ("192.168.0.1", 8080),
        )

    def test_ipv6_strips_brackets(self) -> None:
        self.assertEqual(
            self.app_module._parse_endpoint_host_port("[::1]:9090"),
            ("::1", 9090),
        )

    def test_missing_port_returns_none(self) -> None:
        self.assertIsNone(self.app_module._parse_endpoint_host_port("hostonly"))

    def test_invalid_port_returns_none(self) -> None:
        self.assertIsNone(self.app_module._parse_endpoint_host_port("host:abc"))

    def test_ipv6_invalid_port_returns_none(self) -> None:
        self.assertIsNone(self.app_module._parse_endpoint_host_port("[::1]:abc"))


class SafeIntFloatTests(unittest.TestCase):
    def setUp(self) -> None:
        _install_api_test_fixture(self)

    def test_safe_int_returns_none_on_blanks(self) -> None:
        for blank in (None, "", "-"):
            with self.subTest(value=blank):
                self.assertIsNone(self.app_module._safe_int(blank))

    def test_safe_int_returns_value_on_int_string(self) -> None:
        self.assertEqual(self.app_module._safe_int("42"), 42)

    def test_safe_int_returns_none_on_garbage(self) -> None:
        self.assertIsNone(self.app_module._safe_int("not-a-number"))

    def test_safe_float_returns_none_on_blanks(self) -> None:
        for blank in (None, "", "-"):
            with self.subTest(value=blank):
                self.assertIsNone(self.app_module._safe_float(blank))

    def test_safe_float_strips_percent_suffix(self) -> None:
        self.assertEqual(self.app_module._safe_float("42.5%"), 42.5)

    def test_safe_float_returns_value(self) -> None:
        self.assertEqual(self.app_module._safe_float("0.001"), 0.001)

    def test_safe_float_returns_none_on_garbage(self) -> None:
        self.assertIsNone(self.app_module._safe_float("not-a-float"))


class DockerTimestampTests(unittest.TestCase):
    def setUp(self) -> None:
        _install_api_test_fixture(self)

    def test_zero_value_returns_none(self) -> None:
        # Docker reports an unset timestamp as 0001-01-01T00:00:00Z.
        self.assertIsNone(
            self.app_module._parse_docker_timestamp("0001-01-01T00:00:00Z")
        )

    def test_iso_with_z_suffix_parses(self) -> None:
        result = self.app_module._parse_docker_timestamp("2026-04-25T10:30:00Z")
        self.assertEqual(result, datetime(2026, 4, 25, 10, 30, tzinfo=UTC))

    def test_fractional_seconds_truncated_to_microseconds(self) -> None:
        # Docker emits nanoseconds; Python's fromisoformat handles up to 6
        # digits. The helper truncates the fractional part to 6 digits.
        result = self.app_module._parse_docker_timestamp(
            "2026-04-25T10:30:00.123456789Z"
        )
        self.assertEqual(
            result,
            datetime(2026, 4, 25, 10, 30, 0, 123456, tzinfo=UTC),
        )

    def test_fractional_with_negative_offset(self) -> None:
        result = self.app_module._parse_docker_timestamp(
            "2026-04-25T10:30:00.5-05:00"
        )
        self.assertIsNotNone(result)
        # Already converted from -05:00 offset to a tz-aware datetime.
        self.assertEqual(result.utcoffset(), timedelta(hours=-5))

    def test_empty_string_returns_none(self) -> None:
        self.assertIsNone(self.app_module._parse_docker_timestamp(""))

    def test_garbage_returns_none(self) -> None:
        self.assertIsNone(self.app_module._parse_docker_timestamp("not-a-date"))


class DurationSecondsTests(unittest.TestCase):
    def setUp(self) -> None:
        _install_api_test_fixture(self)

    def test_none_started_returns_none(self) -> None:
        self.assertIsNone(self.app_module._duration_seconds(None))

    def test_explicit_endpoint(self) -> None:
        start = datetime(2026, 4, 25, 10, 0, tzinfo=UTC)
        end = datetime(2026, 4, 25, 10, 0, 30, tzinfo=UTC)
        self.assertEqual(self.app_module._duration_seconds(start, end), 30)

    def test_default_endpoint_is_now(self) -> None:
        start = datetime.now(UTC) - timedelta(seconds=5)
        # Default endpoint is now(); duration should be ~5s.
        result = self.app_module._duration_seconds(start)
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result, 4)
        self.assertLessEqual(result, 7)

    def test_negative_clamped_to_zero(self) -> None:
        # If the caller swaps start and end, the helper clamps to 0
        # rather than returning a negative duration.
        start = datetime(2026, 4, 25, 10, 0, 30, tzinfo=UTC)
        end = datetime(2026, 4, 25, 10, 0, tzinfo=UTC)
        self.assertEqual(self.app_module._duration_seconds(start, end), 0)


# ---------------------------------------------------------------------------
# Listener / port range helpers
# ---------------------------------------------------------------------------
class ListenerSeriesTests(unittest.TestCase):
    def setUp(self) -> None:
        _install_api_test_fixture(self)

    def test_each_series_lookup_table(self) -> None:
        cases = [
            (10001, 1), (10004, 1),
            (10010, 2), (10012, 2),
            (10020, 3), (10023, 3),
            (10030, 4), (10032, 4),
            (10040, 5), (10042, 5),
            (10050, 6), (10055, 6),
            (10061, 7), (10063, 7),
            (10070, 8), (10072, 8),
        ]
        for port, expected in cases:
            with self.subTest(port=port):
                self.assertEqual(self.app_module._listener_series(port), expected)

    def test_unknown_port_returns_none(self) -> None:
        for port in (1, 80, 8080, 10005, 10056, 10065, 99999):
            with self.subTest(port=port):
                self.assertIsNone(self.app_module._listener_series(port))


class FormatPortRangesTests(unittest.TestCase):
    def setUp(self) -> None:
        _install_api_test_fixture(self)

    def test_empty_list(self) -> None:
        self.assertEqual(
            self.app_module._format_port_ranges([]),
            "No challenge ports published",
        )

    def test_single_port(self) -> None:
        self.assertEqual(self.app_module._format_port_ranges([10001]), "10001")

    def test_contiguous_range(self) -> None:
        self.assertEqual(
            self.app_module._format_port_ranges([10001, 10002, 10003]),
            "10001-10003",
        )

    def test_gapped_singletons(self) -> None:
        self.assertEqual(
            self.app_module._format_port_ranges([10001, 10003, 10005]),
            "10001, 10003, 10005",
        )

    def test_mixed_runs_and_singletons(self) -> None:
        self.assertEqual(
            self.app_module._format_port_ranges([10001, 10002, 10003, 10010, 10020, 10021]),
            "10001-10003, 10010, 10020-10021",
        )


# ---------------------------------------------------------------------------
# Compose / variant helpers
# ---------------------------------------------------------------------------
class ComposeServiceNameTests(unittest.TestCase):
    def setUp(self) -> None:
        _install_api_test_fixture(self)

    def test_default_template(self) -> None:
        self.assertEqual(
            self.app_module._compose_service_name(2, "A"),
            "machineH2A",
        )

    def test_template_uses_lowercase_when_requested(self) -> None:
        from config import SETTINGS

        original = SETTINGS.container_name_template
        object.__setattr__(
            SETTINGS,
            "container_name_template",
            "service_{variant_lower}_{series}",
        )
        self.addCleanup(
            lambda: object.__setattr__(SETTINGS, "container_name_template", original)
        )

        self.assertEqual(self.app_module._compose_service_name(3, "C"), "service_c_3")


class SeriesVariantPortsTests(unittest.TestCase):
    def setUp(self) -> None:
        _install_api_test_fixture(self)

    def test_returns_empty_dict_when_runtime_helper_raises(self) -> None:
        # The helper has a blanket ``except Exception``; pinning it
        # here documents that operators get an empty dict on any
        # underlying error rather than a 500.
        self.app_module.runtime._series_public_ports = Mock(side_effect=RuntimeError("boom"))
        self.assertEqual(self.app_module._series_variant_ports(2), {})

    def test_returns_port_to_variant_mapping(self) -> None:
        self.app_module.runtime._series_public_ports = Mock(
            return_value=[10001, 10002, 10003]
        )
        self.assertEqual(
            self.app_module._series_variant_ports(1),
            {10001: "A", 10002: "B", 10003: "C"},
        )

    def test_truncates_to_first_three_variants(self) -> None:
        self.app_module.runtime._series_public_ports = Mock(
            return_value=[10001, 10002, 10003, 10004, 10005]
        )
        ports = self.app_module._series_variant_ports(1)
        self.assertEqual(set(ports.values()), {"A", "B", "C"})


# ---------------------------------------------------------------------------
# HAProxy parser
# ---------------------------------------------------------------------------
class HaproxyServicesParserTests(unittest.TestCase):
    def setUp(self) -> None:
        _install_api_test_fixture(self)

    def _with_config(self, contents: str) -> list[dict]:
        from tempfile import TemporaryDirectory

        ctx = TemporaryDirectory()
        self.addCleanup(ctx.cleanup)
        cfg_path = Path(ctx.name) / "haproxy.cfg"
        cfg_path.write_text(contents, encoding="utf-8")
        previous = self.app_module.HAPROXY_CONFIG_PATH
        self.app_module.HAPROXY_CONFIG_PATH = cfg_path
        self.addCleanup(lambda: setattr(self.app_module, "HAPROXY_CONFIG_PATH", previous))
        return self.app_module._haproxy_services()

    def test_missing_file_returns_empty(self) -> None:
        previous = self.app_module.HAPROXY_CONFIG_PATH
        self.app_module.HAPROXY_CONFIG_PATH = Path("/no/such/path/haproxy.cfg")
        self.addCleanup(lambda: setattr(self.app_module, "HAPROXY_CONFIG_PATH", previous))

        self.assertEqual(self.app_module._haproxy_services(), [])

    def test_listen_block_with_servers(self) -> None:
        services = self._with_config(
            "listen p10010\n"
            "  bind *:10010\n"
            "  server n1 192.168.0.70:10010 check\n"
            "  server n2 192.168.0.103:10010 check\n"
        )
        self.assertEqual(len(services), 1)
        self.assertEqual(services[0]["name"], "p10010")
        self.assertEqual(services[0]["bind_port"], 10010)
        self.assertEqual(len(services[0]["servers"]), 2)

    def test_frontend_backend_block(self) -> None:
        services = self._with_config(
            "frontend f10001\n"
            "  bind *:10001\n"
            "  default_backend b10001\n"
            "backend b10001\n"
            "  server n1 192.168.0.70:10001 check\n"
        )
        self.assertEqual(len(services), 1)
        self.assertEqual(services[0]["name"], "f10001")
        self.assertEqual(services[0]["bind_port"], 10001)
        self.assertEqual(len(services[0]["servers"]), 1)

    def test_comments_and_blank_lines_ignored(self) -> None:
        services = self._with_config(
            "# this is a comment\n"
            "\n"
            "listen p10001\n"
            "  bind *:10001\n"
            "  # commented server\n"
            "  server n1 192.168.0.70:10001 check\n"
        )
        self.assertEqual(len(services), 1)
        self.assertEqual(services[0]["bind_port"], 10001)

    def test_listen_without_servers_is_skipped(self) -> None:
        services = self._with_config(
            "listen p10001\n"
            "  bind *:10001\n"
        )
        self.assertEqual(services, [])

    def test_frontend_without_matching_backend_is_skipped(self) -> None:
        services = self._with_config(
            "frontend f10001\n"
            "  bind *:10001\n"
            "  default_backend nonexistent\n"
        )
        self.assertEqual(services, [])

    def test_listen_without_bind_is_skipped(self) -> None:
        services = self._with_config(
            "listen p10001\n"
            "  server n1 192.168.0.70:10001 check\n"
        )
        self.assertEqual(services, [])


# ---------------------------------------------------------------------------
# State-control admin endpoints
# ---------------------------------------------------------------------------
class StateControlEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        _install_api_test_fixture(self)
        _override_admin_auth(self)

    def test_post_competition_start_invokes_runtime(self) -> None:
        # api_start delegates through run_admin_action -> runtime.start_competition.
        self.app_module.runtime.start_competition = Mock()
        response = self.client.post("/api/competition/start")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        self.app_module.runtime.start_competition.assert_called_once()

    def test_post_competition_stop_invokes_runtime(self) -> None:
        self.app_module.runtime.stop_competition = Mock()
        response = self.client.post("/api/competition/stop")
        self.assertEqual(response.status_code, 200)
        self.app_module.runtime.stop_competition.assert_called_once()

    def test_post_pause_invokes_runtime(self) -> None:
        self.app_module.runtime.pause_rotation = Mock()
        response = self.client.post("/api/pause")
        self.assertEqual(response.status_code, 200)
        self.app_module.runtime.pause_rotation.assert_called_once()

    def test_post_resume_invokes_runtime(self) -> None:
        self.app_module.runtime.resume_rotation = Mock()
        response = self.client.post("/api/resume")
        self.assertEqual(response.status_code, 200)
        self.app_module.runtime.resume_rotation.assert_called_once()

    def test_post_rotate_invokes_runtime(self) -> None:
        self.app_module.runtime.rotate_next_series = Mock()
        response = self.client.post("/api/rotate")
        self.assertEqual(response.status_code, 200)
        self.app_module.runtime.rotate_next_series.assert_called_once()

    def test_post_rotate_restart_invokes_runtime(self) -> None:
        self.app_module.runtime.restart_current_series = Mock()
        response = self.client.post("/api/rotate/restart")
        self.assertEqual(response.status_code, 200)
        self.app_module.runtime.restart_current_series.assert_called_once()

    def test_post_rotate_skip_passes_target_series(self) -> None:
        self.app_module.runtime.rotate_to_series = Mock()
        response = self.client.post(
            "/api/rotate/skip", json={"target_series": 4}
        )
        self.assertEqual(response.status_code, 200)
        self.app_module.runtime.rotate_to_series.assert_called_once_with(4)

    def test_post_poll_invokes_runtime(self) -> None:
        self.app_module.runtime.poll_once = Mock()
        response = self.client.post("/api/poll")
        self.assertEqual(response.status_code, 200)
        self.app_module.runtime.poll_once.assert_called_once()

    def test_post_recover_validate_returns_summary(self) -> None:
        self.app_module.runtime.validate_current_series = Mock(
            return_value={
                "current_series": 2,
                "valid": True,
                "complete_snapshot_matrix": True,
                "healthy_nodes": 3,
                "total_nodes": 3,
                "min_healthy_nodes": 2,
                "healthy_counts_by_variant": {"A": 3, "B": 3, "C": 3},
                "issues": [],
            }
        )
        response = self.client.post("/api/recover/validate")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["valid"])

    def test_runtime_guard_error_translates_to_409(self) -> None:
        # run_admin_action wraps RuntimeGuardError into HTTPException(409).
        from scheduler import RuntimeGuardError

        self.app_module.runtime.start_competition = Mock(
            side_effect=RuntimeGuardError("not from this state")
        )
        response = self.client.post("/api/competition/start")
        self.assertEqual(response.status_code, 409)
        self.assertIn("not from this state", response.json()["detail"])


# ---------------------------------------------------------------------------
# Log tail endpoint helper
# ---------------------------------------------------------------------------
class TailLogTests(unittest.TestCase):
    def setUp(self) -> None:
        _install_api_test_fixture(self)

    def _make_log(self, contents: str) -> Path:
        from tempfile import TemporaryDirectory

        ctx = TemporaryDirectory()
        self.addCleanup(ctx.cleanup)
        log = Path(ctx.name) / "test.log"
        log.write_text(contents, encoding="utf-8")
        return log

    def test_tail_returns_last_n_lines(self) -> None:
        log = self._make_log("a\nb\nc\nd\ne\n")
        result = self.app_module._tail_log(log, source="referee", lines=2)
        self.assertEqual(result.lines, ["d", "e"])
        self.assertEqual(result.source, "referee")

    def test_tail_handles_missing_file(self) -> None:
        result = self.app_module._tail_log(
            Path("/no/such/file.log"), source="haproxy", lines=10
        )
        self.assertEqual(result.lines, [])

    def test_tail_lines_count_exceeds_file(self) -> None:
        log = self._make_log("only-one-line\n")
        result = self.app_module._tail_log(log, source="referee", lines=100)
        self.assertEqual(result.lines, ["only-one-line"])


# ---------------------------------------------------------------------------
# Public dashboard / leaderboard helpers
# ---------------------------------------------------------------------------
class PublicHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        _install_api_test_fixture(self)

    def test_request_host_uses_url_hostname_when_present(self) -> None:
        from fastapi import Request

        scope = {
            "type": "http",
            "headers": [(b"host", b"172.21.0.13:9000")],
            "method": "GET",
            "path": "/",
            "scheme": "http",
            "server": ("172.21.0.13", 9000),
            "query_string": b"",
        }
        request = Request(scope)
        self.assertEqual(self.app_module._request_host(request), "172.21.0.13")

    def test_request_host_falls_back_to_settings(self) -> None:
        from config import SETTINGS

        original = SETTINGS.app_host
        object.__setattr__(SETTINGS, "app_host", "192.168.1.50")
        self.addCleanup(lambda: object.__setattr__(SETTINGS, "app_host", original))

        self.assertEqual(self.app_module._request_host(None), "192.168.1.50")

    def test_request_host_skips_zero_zero_zero_zero_default(self) -> None:
        from config import SETTINGS

        original = SETTINGS.app_host
        object.__setattr__(SETTINGS, "app_host", "0.0.0.0")
        self.addCleanup(lambda: object.__setattr__(SETTINGS, "app_host", original))

        # 0.0.0.0 means "bind everywhere"; not a routable hostname for
        # display. Helper falls through to the hardcoded final default.
        self.assertEqual(self.app_module._request_host(None), "192.168.0.12")

    def test_public_refresh_interval_clamps_to_minimum_three(self) -> None:
        from config import SETTINGS

        original = SETTINGS.poll_interval_seconds
        object.__setattr__(SETTINGS, "poll_interval_seconds", 1)
        self.addCleanup(
            lambda: object.__setattr__(SETTINGS, "poll_interval_seconds", original)
        )

        self.assertEqual(self.app_module._public_refresh_interval_seconds(), 3)

    def test_public_refresh_interval_clamps_to_maximum_ten(self) -> None:
        from config import SETTINGS

        original = SETTINGS.poll_interval_seconds
        object.__setattr__(SETTINGS, "poll_interval_seconds", 600)
        self.addCleanup(
            lambda: object.__setattr__(SETTINGS, "poll_interval_seconds", original)
        )

        self.assertEqual(self.app_module._public_refresh_interval_seconds(), 10)

    def test_public_refresh_interval_returns_proportional_value(self) -> None:
        from config import SETTINGS

        original = SETTINGS.poll_interval_seconds
        object.__setattr__(SETTINGS, "poll_interval_seconds", 30)
        self.addCleanup(
            lambda: object.__setattr__(SETTINGS, "poll_interval_seconds", original)
        )

        # poll_interval_seconds // 6 = 5; clamped within [3, 10] -> 5.
        self.assertEqual(self.app_module._public_refresh_interval_seconds(), 5)


# ---------------------------------------------------------------------------
# Rule-engine admin endpoints
# ---------------------------------------------------------------------------
class RuleEngineAdminEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        _install_api_test_fixture(self)
        _override_admin_auth(self)

    def test_get_rules_returns_active_set(self) -> None:
        response = self.client.get("/api/admin/rules")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["version"], 1)
        self.assertGreaterEqual(len(payload["violations"]), 8)
        names = {entry["name"] for entry in payload["violations"]}
        # Sanity: a few well-known violations from the default YAML.
        self.assertIn("king_perm_changed", names)
        self.assertIn("authkeys_changed", names)
        self.assertIn("shadow_changed", names)
        # Exemptions for H1B and H7B come through in the dict shape.
        scopes = {(e["series"], e["variant"]) for e in payload["exemptions"]}
        self.assertIn((1, "B"), scopes)
        self.assertIn((7, "B"), scopes)

    def test_get_rules_requires_admin_auth(self) -> None:
        # Drop the dependency override added in setUp so the real auth
        # check fires.
        self.app_module.app.dependency_overrides.clear()

        response = self.client.get("/api/admin/rules")
        self.assertEqual(response.status_code, 401)

    def test_post_rules_reload_replaces_active_set(self) -> None:
        from rules import default_ruleset_path

        original_path = default_ruleset_path()
        original_yaml = original_path.read_text(encoding="utf-8")

        # Write a custom ruleset to disk in place of the default. Restore
        # afterwards so the rest of the suite sees the canonical file.
        custom = """
version: 1
violations:
  - {id: 99, name: custom_marker_only, severity: warning}
escalation:
  - {on_offense_count: 1, action: full_ban}
exemptions: []
"""
        original_path.write_text(custom, encoding="utf-8")
        self.addCleanup(lambda: original_path.write_text(original_yaml, encoding="utf-8"))

        response = self.client.post("/api/admin/rules/reload")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["rules"]["violations"]), 1)
        self.assertEqual(payload["rules"]["violations"][0]["name"], "custom_marker_only")

        # Active rule set on the runtime is the reloaded one.
        self.assertEqual(self.app_module.runtime.ruleset.action_for_offense(1), "full_ban")
        # Enforcer gets the same swap.
        self.assertEqual(
            self.app_module.runtime.enforcer.ruleset.action_for_offense(1),
            "full_ban",
        )

    def test_post_rules_reload_logs_admin_event(self) -> None:
        # The reload writes an admin_action event to the events table.
        before = len(self.app_module.db.list_events(limit=20))
        response = self.client.post("/api/admin/rules/reload")
        self.assertEqual(response.status_code, 200)
        events = self.app_module.db.list_events(limit=20)
        self.assertGreater(len(events), before)
        latest = events[0]
        self.assertEqual(latest["type"], "admin_action")
        self.assertIn("rule set reloaded", latest["detail"])

    def test_post_rules_reload_rejects_malformed_yaml_with_422(self) -> None:
        from rules import default_ruleset_path

        original_path = default_ruleset_path()
        original_yaml = original_path.read_text(encoding="utf-8")

        # Write a structurally-broken YAML that the loader will reject.
        original_path.write_text(
            "version: 1\nviolations:\n  - id: 1\n    name: x\n    severity: extreme\n",
            encoding="utf-8",
        )
        self.addCleanup(lambda: original_path.write_text(original_yaml, encoding="utf-8"))

        response = self.client.post("/api/admin/rules/reload")
        self.assertEqual(response.status_code, 422)
        self.assertIn("refusing to reload", response.json()["detail"])

        # The active rule set on the runtime is UNCHANGED; reload is
        # all-or-nothing. Smoke-check by asking for the action; the
        # default 1->warning is still in effect.
        self.assertEqual(
            self.app_module.runtime.ruleset.action_for_offense(1),
            "warning",
        )

    def test_post_rules_reload_requires_admin_auth(self) -> None:
        # Drop the dependency override added in setUp so the real auth
        # check fires.
        self.app_module.app.dependency_overrides.clear()

        response = self.client.post("/api/admin/rules/reload")
        self.assertEqual(response.status_code, 401)

    def test_get_rules_validate_returns_ok_for_default(self) -> None:
        response = self.client.get("/api/admin/rules/validate")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["issues"], [])

    def test_get_rules_validate_flags_orphan_rule(self) -> None:
        # Hot-swap to a rule set whose YAML has an extra rule that no
        # detector implements; the validate endpoint should flag it.
        from rules import RuleSet

        self.app_module.runtime.set_ruleset(
            RuleSet.from_yaml(
                """
version: 1
violations:
  - {id: 1, name: king_perm_changed, severity: critical}
  - {id: 99, name: ghost_violation, severity: critical}
escalation: []
"""
            )
        )

        response = self.client.get("/api/admin/rules/validate")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["ok"])
        joined = " ".join(payload["issues"])
        self.assertIn("ghost_violation", joined)

    def test_get_rules_validate_requires_admin_auth(self) -> None:
        self.app_module.app.dependency_overrides.clear()
        response = self.client.get("/api/admin/rules/validate")
        self.assertEqual(response.status_code, 401)
