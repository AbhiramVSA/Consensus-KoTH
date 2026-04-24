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
