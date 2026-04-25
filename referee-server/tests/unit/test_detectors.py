"""Unit tests for the declarative detector layer.

Two layers of coverage:

* **Registry behavior** — the decorators, the dispatch helpers, the
  cross-check against a rule set. These pin the contract that
  ``Poller`` and ``RefereeRuntime`` rely on without exercising any
  individual detector.

* **Per-detector logic** — every registered detector gets a positive
  case (input that triggers a hit, with the right id / name /
  evidence) and a negative case (input that should NOT trigger). The
  per-detector logic was previously buried inside two if-chains that
  the integration tests in ``test_poller_parsing`` and
  ``test_lifecycle`` exercised end-to-end. This file makes each
  detector independently testable so a future tweak to one detector
  (e.g. tightening the cron-king regex) can land with a focused
  unit test.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

import detectors
from detectors import (
    BaselineDetectorFn,
    SnapshotDetectorFn,
    baseline_detector,
    baseline_detectors,
    detect_all_baseline,
    detect_all_snapshot,
    detector_names,
    snapshot_detector,
    snapshot_detectors,
    validate_against_ruleset,
)
from poller import VariantSnapshot, ViolationHit


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _snap(**sections: str) -> VariantSnapshot:
    return VariantSnapshot(
        node_host="192.168.0.102",
        variant="A",
        king="Team Alpha",
        king_mtime_epoch=1000,
        status="running",
        sections=sections,
        checked_at=datetime.now(UTC),
    )


# ===========================================================================
# Registry behavior
# ===========================================================================
class RegistryBehavior:
    """The decorators and the dispatch helpers."""


def test_snapshot_registry_contains_every_legacy_detector_name() -> None:
    # The eight detector names that used to live as ViolationHit
    # constructions in Poller._detect_violations are all registered.
    expected = {
        "king_perm_changed",
        "king_owner_changed",
        "king_not_regular",
        "king_deleted",
        "root_dir_perm_changed",
        "king_immutable",
        "cron_king_persistence",
        "watchdog_process",
    }
    assert expected.issubset(set(snapshot_detectors()))


def test_baseline_registry_contains_every_legacy_detector_name() -> None:
    expected = {
        "service_ports_changed",
        "iptables_changed",
        "shadow_changed",
        "authkeys_changed",
    }
    assert expected.issubset(set(baseline_detectors()))


def test_detector_names_is_union_of_both_registries() -> None:
    union = set(snapshot_detectors()) | set(baseline_detectors())
    assert detector_names() == union


def test_snapshot_detectors_returns_a_copy() -> None:
    # Mutating the returned dict must not affect the live registry —
    # otherwise a test that adds a stub detector for one assertion would
    # bleed into every later test.
    copy = snapshot_detectors()
    copy["__test_only__"] = lambda snap: None  # type: ignore[assignment]
    assert "__test_only__" not in snapshot_detectors()


def test_duplicate_snapshot_detector_registration_raises() -> None:
    # The first registration of "king_perm_changed" happened at module
    # import. Re-registering must fail loud, not silently overwrite.
    with pytest.raises(ValueError, match="duplicate"):

        @snapshot_detector("king_perm_changed")
        def _dup(snap: VariantSnapshot) -> ViolationHit | None:  # pragma: no cover
            return None


def test_duplicate_baseline_detector_registration_raises() -> None:
    with pytest.raises(ValueError, match="duplicate"):

        @baseline_detector("authkeys_changed")
        def _dup(  # pragma: no cover
            snap: VariantSnapshot, baseline: dict[str, Any]
        ) -> ViolationHit | None:
            return None


def test_detect_all_snapshot_returns_empty_for_empty_sections() -> None:
    snap = _snap()  # no sections -> no detectors should fire
    assert detect_all_snapshot(snap) == []


def test_detect_all_baseline_returns_empty_when_baseline_keys_absent() -> None:
    # Empty baseline -> none of the four baseline detectors have anything
    # to compare against.
    snap = _snap(SHADOW="abc", AUTHKEYS="def", IPTABLES="x", PORTS="y")
    assert detect_all_baseline(snap, {}) == []


def test_detect_all_snapshot_preserves_registration_order() -> None:
    # Pin the legacy ordering: tests that previously relied on
    # ``hits[0].offense_name == 'king_perm_changed'`` for a snapshot
    # that triggers king_perm + king_owner + king_not_regular keep
    # working because the registry iterates in registration order
    # and king_perm_changed is registered first.
    snap = _snap(KING_STAT="1000 600 nobody:nogroup symbolic link")
    hits = detect_all_snapshot(snap)
    names = [hit.offense_name for hit in hits]
    # The first three king_* detectors register in this order.
    assert names == ["king_perm_changed", "king_owner_changed", "king_not_regular"]


# ===========================================================================
# Cross-check against a rule set
# ===========================================================================
def test_validate_against_ruleset_returns_no_issues_for_default() -> None:
    from rules import load_default_ruleset

    issues = validate_against_ruleset(load_default_ruleset())
    assert issues == []


def test_validate_against_ruleset_flags_orphan_detector() -> None:
    # A rule set whose YAML has fewer violations than the registry — the
    # registry has 12 names; we hand it a rule set with a strict subset.
    from rules import RuleSet

    minimal = RuleSet.from_yaml(
        """
version: 1
violations:
  - {id: 1, name: king_perm_changed, severity: critical}
escalation: []
"""
    )
    issues = validate_against_ruleset(minimal)
    assert issues
    joined = " ".join(issues)
    # Some detectors registered for which the rule set has no entry.
    assert "registered detectors with no matching rule entry" in joined
    assert "watchdog_process" in joined


def test_validate_against_ruleset_flags_orphan_rule() -> None:
    from rules import RuleSet

    extra = RuleSet.from_yaml(
        """
version: 1
violations:
  - {id: 1, name: king_perm_changed, severity: critical}
  - {id: 99, name: ghost_violation, severity: critical}
  - {id: 100, name: another_phantom, severity: warning}
escalation: []
"""
    )
    issues = validate_against_ruleset(extra)
    assert issues
    joined = " ".join(issues)
    assert "rule entries with no matching registered detector" in joined
    assert "ghost_violation" in joined
    assert "another_phantom" in joined


# ===========================================================================
# Snapshot detectors
# ===========================================================================
class KingStatDetectors:
    """The three king_* detectors all parse KING_STAT."""


def test_king_perm_changed_fires_on_non_644() -> None:
    hit = detectors._detect_king_perm_changed(_snap(KING_STAT="1000 600 root:root regular file"))
    assert hit is not None
    assert hit.offense_name == "king_perm_changed"
    assert hit.evidence == {"perm": "600"}


def test_king_perm_changed_silent_on_644() -> None:
    hit = detectors._detect_king_perm_changed(_snap(KING_STAT="1000 644 root:root regular file"))
    assert hit is None


def test_king_perm_changed_silent_when_section_missing() -> None:
    hit = detectors._detect_king_perm_changed(_snap())
    assert hit is None


def test_king_perm_changed_silent_on_stat_fail() -> None:
    hit = detectors._detect_king_perm_changed(_snap(KING_STAT="STAT_FAIL"))
    assert hit is None


def test_king_perm_changed_silent_on_truncated_kings_stat() -> None:
    # 3 fields is below the 4-field minimum the parser requires; the
    # detector must return None rather than IndexError.
    hit = detectors._detect_king_perm_changed(_snap(KING_STAT="1000 600 root:root"))
    assert hit is None


def test_king_owner_changed_fires_on_non_root_owner() -> None:
    hit = detectors._detect_king_owner_changed(
        _snap(KING_STAT="1000 644 nobody:nogroup regular file")
    )
    assert hit is not None
    assert hit.offense_name == "king_owner_changed"
    assert hit.evidence == {"owner": "nobody:nogroup"}


def test_king_owner_changed_silent_on_root() -> None:
    hit = detectors._detect_king_owner_changed(
        _snap(KING_STAT="1000 644 root:root regular file")
    )
    assert hit is None


def test_king_not_regular_fires_on_symbolic_link() -> None:
    hit = detectors._detect_king_not_regular(
        _snap(KING_STAT="1000 644 root:root symbolic link")
    )
    assert hit is not None
    assert hit.offense_name == "king_not_regular"
    assert hit.evidence == {"file_type": "symbolic link"}


def test_king_not_regular_silent_on_regular_file() -> None:
    hit = detectors._detect_king_not_regular(
        _snap(KING_STAT="1000 644 root:root regular file")
    )
    assert hit is None


def test_king_not_regular_lowercases_file_type_before_compare() -> None:
    # Stat output of "Regular File" (with capitals) must still NOT
    # trigger; the detector lowercases first.
    hit = detectors._detect_king_not_regular(
        _snap(KING_STAT="1000 644 root:root Regular File")
    )
    assert hit is None


# ---------------------------------------------------------------------------
# Other snapshot detectors
# ---------------------------------------------------------------------------
def test_king_deleted_fires_on_file_missing() -> None:
    hit = detectors._detect_king_deleted(_snap(KING="FILE_MISSING"))
    assert hit is not None
    assert hit.offense_name == "king_deleted"


def test_king_deleted_silent_on_normal_king() -> None:
    hit = detectors._detect_king_deleted(_snap(KING="Team Alpha"))
    assert hit is None


def test_king_deleted_silent_on_empty_section() -> None:
    hit = detectors._detect_king_deleted(_snap())
    assert hit is None


def test_root_dir_perm_changed_fires_on_777() -> None:
    hit = detectors._detect_root_dir_perm_changed(_snap(ROOT_DIR="777"))
    assert hit is not None
    assert hit.offense_name == "root_dir_perm_changed"
    assert hit.evidence == {"root_dir": "777"}


def test_root_dir_perm_changed_silent_on_700() -> None:
    hit = detectors._detect_root_dir_perm_changed(_snap(ROOT_DIR="700"))
    assert hit is None


def test_root_dir_perm_changed_silent_on_700_with_trailing_newline() -> None:
    # Probe output usually ends with a newline; the detector splits
    # on lines and strips, so 700\n must be accepted.
    hit = detectors._detect_root_dir_perm_changed(_snap(ROOT_DIR="700\n"))
    assert hit is None


def test_root_dir_perm_changed_silent_on_empty_section() -> None:
    hit = detectors._detect_root_dir_perm_changed(_snap(ROOT_DIR=""))
    assert hit is None


def test_king_immutable_fires_on_isolated_word_i() -> None:
    # See known-bug pinning in test_poller_parsing.py — the legacy
    # check matches " i " as a whitespace-bounded word.
    hit = detectors._detect_king_immutable(_snap(IMMUTABLE="file has i flag"))
    assert hit is not None
    assert hit.offense_name == "king_immutable"


def test_king_immutable_silent_on_real_lsattr_dashes() -> None:
    # Documents the known bug: real lsattr embeds the i flag in
    # dashes and is missed.
    hit = detectors._detect_king_immutable(
        _snap(IMMUTABLE="----i---------- /root/king.txt")
    )
    assert hit is None


def test_cron_king_persistence_case_insensitive() -> None:
    for cron in ("* * * * * echo king", "# spKING here", "reKing"):
        hit = detectors._detect_cron_king_persistence(_snap(CRON=cron))
        assert hit is not None, cron


def test_cron_king_persistence_silent_on_unrelated_cron() -> None:
    hit = detectors._detect_cron_king_persistence(
        _snap(CRON="* * * * * /usr/local/bin/backup.sh")
    )
    assert hit is None


def test_watchdog_process_fires_on_inotify() -> None:
    hit = detectors._detect_watchdog_process(
        _snap(PROCS="root 42 0.0 inotifywait /root/king.txt")
    )
    assert hit is not None
    assert hit.offense_name == "watchdog_process"


def test_watchdog_process_fires_on_fswatch() -> None:
    hit = detectors._detect_watchdog_process(_snap(PROCS="root 43 0.0 fswatch /root"))
    assert hit is not None


def test_watchdog_process_fires_on_incrond() -> None:
    hit = detectors._detect_watchdog_process(_snap(PROCS="root 44 0.0 /tmp/incrond"))
    assert hit is not None


def test_watchdog_process_silent_on_normal_processes() -> None:
    hit = detectors._detect_watchdog_process(_snap(PROCS="root 1 0.0 /sbin/init"))
    assert hit is None


# ===========================================================================
# Baseline detectors
# ===========================================================================
def _baseline_snap(**sections: str) -> VariantSnapshot:
    return VariantSnapshot(
        node_host="192.168.0.102",
        variant="A",
        king="Team Alpha",
        king_mtime_epoch=1000,
        status="running",
        sections=sections,
        checked_at=datetime.now(UTC),
    )


def test_service_ports_changed_fires_when_signature_drifts() -> None:
    # Use a real-shaped ss output so stable_ports_signature actually
    # produces a value.
    current_ports = "State\nLISTEN 0 100 *:9090 *:*\n"
    snap = _baseline_snap(PORTS=current_ports)
    baseline = {"ports_sig": "stale-sig", "iptables_sig": None, "shadow_hash": None, "authkeys_hash": None}
    hit = detectors._detect_service_ports_changed(snap, baseline)
    assert hit is not None
    assert hit.offense_name == "service_ports_changed"
    assert hit.evidence["expected_sig"] == "stale-sig"
    assert hit.evidence["actual_sig"]  # non-empty


def test_service_ports_changed_silent_when_signatures_match() -> None:
    from poller import Poller

    current_ports = "State\nLISTEN 0 100 *:9090 *:*\n"
    matching_sig = Poller.stable_ports_signature(current_ports)
    snap = _baseline_snap(PORTS=current_ports)
    baseline = {"ports_sig": matching_sig, "iptables_sig": None, "shadow_hash": None, "authkeys_hash": None}
    assert detectors._detect_service_ports_changed(snap, baseline) is None


def test_service_ports_changed_silent_when_baseline_has_no_sig() -> None:
    snap = _baseline_snap(PORTS="State\nLISTEN 0 100 *:9090 *:*\n")
    baseline = {"ports_sig": None, "iptables_sig": None, "shadow_hash": None, "authkeys_hash": None}
    assert detectors._detect_service_ports_changed(snap, baseline) is None


def test_iptables_changed_fires_when_signature_drifts() -> None:
    snap = _baseline_snap(IPTABLES="Chain INPUT (policy ACCEPT)\n")
    baseline = {"iptables_sig": "stale-iptables", "ports_sig": None, "shadow_hash": None, "authkeys_hash": None}
    hit = detectors._detect_iptables_changed(snap, baseline)
    assert hit is not None
    assert hit.offense_name == "iptables_changed"


def test_iptables_changed_silent_when_baseline_unset() -> None:
    snap = _baseline_snap(IPTABLES="Chain INPUT")
    assert detectors._detect_iptables_changed(snap, {"iptables_sig": None}) is None


def test_shadow_changed_fires_on_hash_drift() -> None:
    current = "a" * 64 + "  /etc/shadow"
    snap = _baseline_snap(SHADOW=current)
    baseline = {"shadow_hash": "b" * 64, "ports_sig": None, "iptables_sig": None, "authkeys_hash": None}
    hit = detectors._detect_shadow_changed(snap, baseline)
    assert hit is not None
    assert hit.offense_name == "shadow_changed"
    assert hit.evidence == {"shadow_expected": "b" * 64, "shadow_actual": "a" * 64}


def test_shadow_changed_silent_when_baseline_hash_is_none() -> None:
    snap = _baseline_snap(SHADOW="a" * 64 + "  /etc/shadow")
    baseline = {"shadow_hash": None, "ports_sig": None, "iptables_sig": None, "authkeys_hash": None}
    assert detectors._detect_shadow_changed(snap, baseline) is None


def test_shadow_changed_silent_when_hashes_match() -> None:
    digest = "a" * 64
    snap = _baseline_snap(SHADOW=f"{digest}  /etc/shadow")
    baseline = {"shadow_hash": digest, "ports_sig": None, "iptables_sig": None, "authkeys_hash": None}
    assert detectors._detect_shadow_changed(snap, baseline) is None


def test_authkeys_changed_fires_on_hash_drift() -> None:
    current = "c" * 64 + "  /root/.ssh/authorized_keys"
    snap = _baseline_snap(AUTHKEYS=current)
    baseline = {"authkeys_hash": "d" * 64, "ports_sig": None, "iptables_sig": None, "shadow_hash": None}
    hit = detectors._detect_authkeys_changed(snap, baseline)
    assert hit is not None
    assert hit.offense_name == "authkeys_changed"


def test_authkeys_changed_silent_when_baseline_unset() -> None:
    snap = _baseline_snap(AUTHKEYS="c" * 64 + "  /root/.ssh/authorized_keys")
    baseline = {"authkeys_hash": None, "ports_sig": None, "iptables_sig": None, "shadow_hash": None}
    assert detectors._detect_authkeys_changed(snap, baseline) is None


# ===========================================================================
# Type aliases / surface
# ===========================================================================
def test_type_aliases_are_callables_of_expected_arity() -> None:
    # The aliases are documentation in the type system; this just
    # asserts that the module exposes them so a static check doesn't
    # quietly remove them.
    assert SnapshotDetectorFn is not None
    assert BaselineDetectorFn is not None
