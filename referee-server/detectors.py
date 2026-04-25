"""Declarative detector registry for KoTH violations.

Pulls every ``ViolationHit(...)`` construction out of
``Poller._detect_violations`` and ``RefereeRuntime._merge_baseline_violations``
and into named, registered functions. Adding a new violation now means:

  1. Write a function in this module decorated with ``@snapshot_detector``
     or ``@baseline_detector``.
  2. Add a corresponding entry to ``rules.default.yaml`` (id, name,
     severity, description).

The two halves can be cross-checked at runtime: ``validate_against_ruleset``
diffs the registered detector names against the rule set's violation names
and returns a list of mismatches. The startup wiring uses that to log a
WARN if anything is out of sync.

Two flavors of detector exist because two flavors of violation exist:

* **Snapshot detectors** look at one ``VariantSnapshot`` in isolation and
  return a hit when the probe output matches a static-rule pattern (king
  permission != 644, root_dir permission != 700, immutable flag set,
  etc.). These were the 8 detectors in ``Poller._detect_violations``.

* **Baseline detectors** look at one ``VariantSnapshot`` plus the
  baseline dict captured at deploy time, and return a hit when the
  current observation has drifted from the recorded baseline (shadow
  hash mismatch, authorized_keys hash mismatch, iptables ruleset
  signature mismatch, listening-ports signature mismatch). These were
  the 4 detectors in ``_merge_baseline_violations``.

The two registries are kept distinct (rather than unified behind a
``baseline | None`` arg) so each detector's signature is honest about
what it actually needs. A baseline detector that ignored the baseline
arg would mislead the reader.

Each detector returns ``ViolationHit`` (a hit was produced) or
``None`` (the snapshot is fine on this rule). Returning a list — even
of length 1 — was rejected because every existing detector produces
exactly zero or one hit, and the call site is simpler when the
contract is "Optional[ViolationHit]".

Behavior is byte-equivalent to the legacy if-chains. The poller and
scheduler tests in tests/unit/test_poller_parsing.py and
tests/integration/test_lifecycle.py continue to pass against the
default rule set; this module is a refactor, not a behavior change.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, TypeAlias

# ``Poller`` and ``ViolationHit`` are imported lazily inside the
# detector functions where actually needed, because:
# (a) ``poller.py`` imports this module's dispatchers via late binding
#     to avoid a circular import at startup, and
# (b) the few baseline detectors that reuse Poller's static helpers
#     (extract_sha256_or_missing, stable_signature, stable_ports_signature)
#     do not need to pull Poller in until they actually run.
from poller import ViolationHit, VariantSnapshot


# ---------------------------------------------------------------------------
# Type aliases for the two detector signatures.
# ---------------------------------------------------------------------------
SnapshotDetectorFn: TypeAlias = Callable[[VariantSnapshot], "ViolationHit | None"]
BaselineDetectorFn: TypeAlias = Callable[
    [VariantSnapshot, dict[str, Any]], "ViolationHit | None"
]


# ---------------------------------------------------------------------------
# Registries.
#
# Insertion order is preserved (Python 3.7+ dict guarantee), and the
# dispatch helpers iterate the registry in that order, so the order in
# which hits land on a snapshot matches authoring order in this file.
# Tests that previously depended on the legacy ordering of
# ``_detect_violations`` continue to pass because the registration
# order below mirrors the old if-chain.
# ---------------------------------------------------------------------------
_SNAPSHOT_DETECTORS: dict[str, SnapshotDetectorFn] = {}
_BASELINE_DETECTORS: dict[str, BaselineDetectorFn] = {}


# ---------------------------------------------------------------------------
# Registration decorators.
# ---------------------------------------------------------------------------
def snapshot_detector(name: str) -> Callable[[SnapshotDetectorFn], SnapshotDetectorFn]:
    """Register ``fn`` as the snapshot detector for the named violation.

    Raises ``ValueError`` on duplicate names: a name collision is a
    configuration bug and silently overwriting one detector with
    another would produce a hard-to-find scoring inconsistency.
    """

    def decorator(fn: SnapshotDetectorFn) -> SnapshotDetectorFn:
        if name in _SNAPSHOT_DETECTORS:
            raise ValueError(f"duplicate snapshot detector: {name}")
        _SNAPSHOT_DETECTORS[name] = fn
        return fn

    return decorator


def baseline_detector(name: str) -> Callable[[BaselineDetectorFn], BaselineDetectorFn]:
    def decorator(fn: BaselineDetectorFn) -> BaselineDetectorFn:
        if name in _BASELINE_DETECTORS:
            raise ValueError(f"duplicate baseline detector: {name}")
        _BASELINE_DETECTORS[name] = fn
        return fn

    return decorator


# ---------------------------------------------------------------------------
# Public introspection.
# ---------------------------------------------------------------------------
def snapshot_detectors() -> dict[str, SnapshotDetectorFn]:
    """Return a copy of the snapshot-detector registry.

    Returning a copy keeps the live registry from leaking out as a
    mutable handle. Callers that want to swap the registry — e.g. a
    test that registers a stub detector — must use the decorator on a
    new module so the existing entries stay intact.
    """
    return dict(_SNAPSHOT_DETECTORS)


def baseline_detectors() -> dict[str, BaselineDetectorFn]:
    return dict(_BASELINE_DETECTORS)


def detector_names() -> set[str]:
    """Names of every registered detector (snapshot + baseline)."""
    return set(_SNAPSHOT_DETECTORS) | set(_BASELINE_DETECTORS)


# ---------------------------------------------------------------------------
# Dispatch helpers.
# ---------------------------------------------------------------------------
def detect_all_snapshot(snap: VariantSnapshot) -> list[ViolationHit]:
    """Run every registered snapshot detector against ``snap`` and
    return the non-None hits in registration order.
    """
    hits: list[ViolationHit] = []
    for fn in _SNAPSHOT_DETECTORS.values():
        result = fn(snap)
        if result is not None:
            hits.append(result)
    return hits


def detect_all_baseline(
    snap: VariantSnapshot, baseline: dict[str, Any]
) -> list[ViolationHit]:
    """Run every registered baseline detector against (``snap``, ``baseline``).

    The caller is responsible for skipping snapshots that have no
    baseline (i.e. ``baseline is None``); this function does not
    short-circuit on its own because the dispatch loop has nothing to
    do in that case anyway.
    """
    hits: list[ViolationHit] = []
    for fn in _BASELINE_DETECTORS.values():
        result = fn(snap, baseline)
        if result is not None:
            hits.append(result)
    return hits


# ---------------------------------------------------------------------------
# Ruleset cross-check.
# ---------------------------------------------------------------------------
def validate_against_ruleset(ruleset: Any) -> list[str]:
    """Return human-readable mismatches between the registry and a rule set.

    Two failure modes are interesting:

      * a registered detector with no matching rule entry — the
        detector will fire and produce a hit, but the rule engine has
        no metadata for it (severity, description, exemptions). The
        scheduler still records the violation, but the operator-
        visible UI may render it as "unknown rule".

      * a rule entry with no registered detector — the rule is dead
        weight. No hit can ever be produced for it.

    Both are configuration-time bugs. The list this function returns
    is empty on a healthy installation.
    """
    yaml_names = set(getattr(ruleset, "violations", {}))
    registered = detector_names()

    issues: list[str] = []
    detectors_without_rule = registered - yaml_names
    rules_without_detector = yaml_names - registered

    if detectors_without_rule:
        issues.append(
            "registered detectors with no matching rule entry: "
            + ", ".join(sorted(detectors_without_rule))
        )
    if rules_without_detector:
        issues.append(
            "rule entries with no matching registered detector: "
            + ", ".join(sorted(rules_without_detector))
        )
    return issues


# ===========================================================================
# Snapshot detectors.
#
# Each function below mirrors one branch of the legacy
# ``Poller._detect_violations`` if-chain. The ordering of @snapshot_detector
# decorations matches the legacy order so test expectations on hit-list
# ordering are preserved without explicit sorting.
# ===========================================================================
def _king_stat_fields(snap: VariantSnapshot) -> list[str] | None:
    """Helper: parse the KING_STAT section into whitespace-split fields.

    Returns None if KING_STAT is missing, the probe failed
    (``STAT_FAIL`` substring), or the line has fewer than 4 fields.
    Used by the three king_*_changed detectors so each does not
    re-implement the parsing.
    """
    raw = snap.sections.get("KING_STAT", "")
    if not raw or "STAT_FAIL" in raw:
        return None
    fields = raw.splitlines()[0].split()
    if len(fields) < 4:
        return None
    return fields


@snapshot_detector("king_perm_changed")
def _detect_king_perm_changed(snap: VariantSnapshot) -> ViolationHit | None:
    fields = _king_stat_fields(snap)
    if fields is None:
        return None
    perm = fields[1]
    if perm != "644":
        return ViolationHit(1, "king_perm_changed", {"perm": perm})
    return None


@snapshot_detector("king_owner_changed")
def _detect_king_owner_changed(snap: VariantSnapshot) -> ViolationHit | None:
    fields = _king_stat_fields(snap)
    if fields is None:
        return None
    owner = fields[2]
    if owner != "root:root":
        return ViolationHit(2, "king_owner_changed", {"owner": owner})
    return None


@snapshot_detector("king_not_regular")
def _detect_king_not_regular(snap: VariantSnapshot) -> ViolationHit | None:
    fields = _king_stat_fields(snap)
    if fields is None:
        return None
    file_type = " ".join(fields[3:]).lower()
    if "regular file" not in file_type:
        return ViolationHit(5, "king_not_regular", {"file_type": file_type})
    return None


@snapshot_detector("king_deleted")
def _detect_king_deleted(snap: VariantSnapshot) -> ViolationHit | None:
    king = snap.sections.get("KING", "")
    if "FILE_MISSING" in king:
        return ViolationHit(4, "king_deleted", {"king": king})
    return None


@snapshot_detector("root_dir_perm_changed")
def _detect_root_dir_perm_changed(snap: VariantSnapshot) -> ViolationHit | None:
    root_dir = snap.sections.get("ROOT_DIR", "")
    if root_dir and root_dir.splitlines()[0].strip() != "700":
        return ViolationHit(6, "root_dir_perm_changed", {"root_dir": root_dir.strip()})
    return None


@snapshot_detector("king_immutable")
def _detect_king_immutable(snap: VariantSnapshot) -> ViolationHit | None:
    immutable = snap.sections.get("IMMUTABLE", "")
    # Known issue: ``" i " in f" {immutable} "`` only fires when the
    # ``i`` flag appears as a whitespace-bounded word. Real ``lsattr``
    # output embeds the flag between dashes (``----i---------``) and
    # is therefore missed. The buggy semantics are preserved for now;
    # tests/unit/test_poller_parsing.py pins both sides of the
    # behavior so a future fix has a contract to satisfy.
    if immutable and " i " in f" {immutable} ":
        return ViolationHit(3, "king_immutable", {"lsattr": immutable.strip()})
    return None


@snapshot_detector("cron_king_persistence")
def _detect_cron_king_persistence(snap: VariantSnapshot) -> ViolationHit | None:
    cron = snap.sections.get("CRON", "")
    if re.search(r"king", cron, re.IGNORECASE):
        return ViolationHit(7, "cron_king_persistence", {"cron": cron[:500]})
    return None


@snapshot_detector("watchdog_process")
def _detect_watchdog_process(snap: VariantSnapshot) -> ViolationHit | None:
    procs = snap.sections.get("PROCS", "")
    if re.search(r"inotify|fswatch|incrond", procs, re.IGNORECASE):
        return ViolationHit(8, "watchdog_process", {"procs": procs[:500]})
    return None


# ===========================================================================
# Baseline detectors.
#
# Each compares one section of the current snapshot to a key on the
# baseline dict captured at deploy time, and emits a hit when the two
# differ. The baseline dict shape comes from
# ``Database.get_baseline``: keys are ``ports_sig``, ``iptables_sig``,
# ``shadow_hash``, ``authkeys_hash``.
# ===========================================================================
@baseline_detector("service_ports_changed")
def _detect_service_ports_changed(
    snap: VariantSnapshot, baseline: dict[str, Any]
) -> ViolationHit | None:
    from poller import Poller  # local import to avoid module-load cycle

    ports_sig = Poller.stable_ports_signature(snap.sections.get("PORTS", ""))
    if (
        baseline.get("ports_sig")
        and ports_sig
        and baseline["ports_sig"] != ports_sig
    ):
        return ViolationHit(
            12,
            "service_ports_changed",
            {"expected_sig": baseline["ports_sig"], "actual_sig": ports_sig},
        )
    return None


@baseline_detector("iptables_changed")
def _detect_iptables_changed(
    snap: VariantSnapshot, baseline: dict[str, Any]
) -> ViolationHit | None:
    from poller import Poller

    iptables_sig = Poller.stable_signature(snap.sections.get("IPTABLES", ""))
    if (
        baseline.get("iptables_sig")
        and iptables_sig
        and baseline["iptables_sig"] != iptables_sig
    ):
        return ViolationHit(
            13,
            "iptables_changed",
            {"expected_sig": baseline["iptables_sig"], "actual_sig": iptables_sig},
        )
    return None


@baseline_detector("shadow_changed")
def _detect_shadow_changed(
    snap: VariantSnapshot, baseline: dict[str, Any]
) -> ViolationHit | None:
    from poller import Poller

    shadow_hash = Poller.extract_sha256_or_missing(snap.sections.get("SHADOW", ""))
    if (
        baseline.get("shadow_hash") is not None
        and baseline["shadow_hash"] != shadow_hash
    ):
        return ViolationHit(
            14,
            "shadow_changed",
            {
                "shadow_expected": baseline.get("shadow_hash"),
                "shadow_actual": shadow_hash,
            },
        )
    return None


@baseline_detector("authkeys_changed")
def _detect_authkeys_changed(
    snap: VariantSnapshot, baseline: dict[str, Any]
) -> ViolationHit | None:
    from poller import Poller

    authkeys_hash = Poller.extract_sha256_or_missing(
        snap.sections.get("AUTHKEYS", "")
    )
    if (
        baseline.get("authkeys_hash") is not None
        and baseline["authkeys_hash"] != authkeys_hash
    ):
        return ViolationHit(
            15,
            "authkeys_changed",
            {
                "authkeys_expected": baseline.get("authkeys_hash"),
                "authkeys_actual": authkeys_hash,
            },
        )
    return None
