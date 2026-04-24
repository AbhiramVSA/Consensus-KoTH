"""Unit tests for ``Settings.validate_runtime``.

All mutations go through the ``settings_override`` fixture so no test can
leave SETTINGS in a dirty state for the next one.
"""
from __future__ import annotations

import pytest

from config import SETTINGS


pytestmark = pytest.mark.unit


def test_validate_runtime_rejects_mismatched_target_count(settings_override) -> None:
    with settings_override(
        node_hosts=("192.168.0.102", "192.168.0.103"),
        node_ssh_targets=("nodeA@192.168.0.102",),
    ):
        with pytest.raises(RuntimeError):
            SETTINGS.validate_runtime()


# ---------------------------------------------------------------------------
# Edge cases for Settings.validate_runtime.
#
# Every guard clause in config.py's ``validate_runtime`` deserves one
# failing-case test so a refactor that reorders the checks or drops one
# fails loudly instead of silently accepting an unsafe configuration.
# ---------------------------------------------------------------------------
def test_validate_runtime_rejects_missing_admin_key_without_unsafe_flag(
    settings_override,
) -> None:
    with settings_override(
        admin_api_key="",
        allow_unsafe_no_admin_api_key=False,
    ):
        with pytest.raises(RuntimeError, match="ADMIN_API_KEY"):
            SETTINGS.validate_runtime()


def test_validate_runtime_accepts_missing_admin_key_with_unsafe_flag(
    settings_override,
) -> None:
    # The "unsafe" flag is the explicit opt-out for local/dev mode. When
    # it is set, validate_runtime must accept an empty admin key. This
    # test pins that escape hatch so a well-meaning refactor does not
    # quietly force production-level auth on dev machines.
    with settings_override(
        admin_api_key="",
        allow_unsafe_no_admin_api_key=True,
    ):
        SETTINGS.validate_runtime()  # must not raise


def test_validate_runtime_rejects_empty_node_hosts(settings_override) -> None:
    with settings_override(node_hosts=()):
        with pytest.raises(RuntimeError, match="NODE_HOSTS"):
            SETTINGS.validate_runtime()


def test_validate_runtime_rejects_min_healthy_below_one(settings_override) -> None:
    with settings_override(min_healthy_nodes=0):
        with pytest.raises(RuntimeError, match="MIN_HEALTHY_NODES"):
            SETTINGS.validate_runtime()


def test_validate_runtime_rejects_min_healthy_above_node_count(settings_override) -> None:
    with settings_override(
        node_hosts=("192.168.0.102", "192.168.0.103"),
        min_healthy_nodes=5,
    ):
        with pytest.raises(RuntimeError, match="MIN_HEALTHY_NODES"):
            SETTINGS.validate_runtime()


def test_validate_runtime_accepts_min_healthy_exactly_node_count(settings_override) -> None:
    # min_healthy_nodes == len(node_hosts) means "every node must be
    # healthy", which is a legitimate if strict operational choice.
    with settings_override(
        node_hosts=("192.168.0.102", "192.168.0.103", "192.168.0.106"),
        min_healthy_nodes=3,
    ):
        SETTINGS.validate_runtime()


def test_validate_runtime_rejects_empty_variants(settings_override) -> None:
    with settings_override(variants=()):
        with pytest.raises(RuntimeError, match="VARIANTS"):
            SETTINGS.validate_runtime()


def test_validate_runtime_rejects_zero_total_series(settings_override) -> None:
    with settings_override(total_series=0):
        with pytest.raises(RuntimeError, match="TOTAL_SERIES"):
            SETTINGS.validate_runtime()


def test_validate_runtime_rejects_zero_deploy_health_timeout(settings_override) -> None:
    with settings_override(deploy_health_timeout_seconds=0):
        with pytest.raises(RuntimeError, match="DEPLOY_HEALTH_TIMEOUT_SECONDS"):
            SETTINGS.validate_runtime()


def test_validate_runtime_rejects_zero_deploy_health_poll(settings_override) -> None:
    with settings_override(deploy_health_poll_seconds=0):
        with pytest.raises(RuntimeError, match="DEPLOY_HEALTH_POLL_SECONDS"):
            SETTINGS.validate_runtime()


def test_validate_runtime_rejects_empty_docker_compose_cmd(settings_override) -> None:
    with settings_override(docker_compose_cmd=""):
        with pytest.raises(RuntimeError, match="DOCKER_COMPOSE_CMD"):
            SETTINGS.validate_runtime()


def test_validate_runtime_empty_node_ssh_targets_is_accepted(settings_override) -> None:
    # Empty NODE_SSH_TARGETS means "use NODE_HOSTS directly with the
    # default SSH_USER" — a legitimate single-user deployment.
    with settings_override(
        node_hosts=("192.168.0.102", "192.168.0.103", "192.168.0.106"),
        node_ssh_targets=(),
    ):
        SETTINGS.validate_runtime()


def test_ssh_target_overrides_zips_correctly_on_match(settings_override) -> None:
    with settings_override(
        node_hosts=("h1", "h2", "h3"),
        node_ssh_targets=("user-a@h1", "user-b@h2", "user-c@h3"),
    ):
        overrides = SETTINGS.ssh_target_overrides()
    assert overrides == {"h1": "user-a@h1", "h2": "user-b@h2", "h3": "user-c@h3"}


def test_ssh_target_overrides_returns_empty_when_targets_unset(settings_override) -> None:
    with settings_override(
        node_hosts=("h1", "h2", "h3"),
        node_ssh_targets=(),
    ):
        assert SETTINGS.ssh_target_overrides() == {}
