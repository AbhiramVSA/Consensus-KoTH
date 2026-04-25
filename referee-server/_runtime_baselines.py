"""Baseline-capture and series-health mixin for ``RefereeRuntime``.

Final piece of the god-class split tracked in docs/AUDIT.md §8 row #2.
The mixin owns every method that compares a probe snapshot to a
baseline or evaluates the matrix of snapshots for health:

* ``_capture_baselines`` — record per-(host, variant) baseline hashes
  + signatures right after a successful deploy.
* ``_merge_baseline_violations`` — diff each snapshot against its
  recorded baseline, run every registered baseline detector, drop
  exempt hits.
* ``_expected_snapshot_pairs`` / ``_snapshot_matrix_issues`` —
  surface missing or unexpected (host, variant) coordinates.
* ``_running_snapshot_counts_by_variant`` /
  ``_healthy_running_host_count`` — aggregate the snapshot matrix
  for the validation summary.
* ``_evaluate_series_health`` — combine matrix + king + deploy results
  into a list of human-readable issues.
* ``_validate_series_state`` / ``_validate_current_series_or_raise``
  — public-facing health-check entry points.
* ``_mark_clock_drift_degraded`` — flip ``status="degraded"`` on
  snapshots from nodes whose clock has drifted past the configured
  threshold so the scorer ignores them.
* ``_log_series_health`` — emit per-snapshot warning events after a
  failing deploy so the operator can see exactly which container is
  unhealthy.

State on ``self`` that the mixin reads:

* ``self.poller``           — ``extract_sha256_or_missing``,
                               ``stable_signature``,
                               ``stable_ports_signature``,
                               ``run_cycle``.
* ``self.db``               — ``upsert_baseline``, ``get_baseline``,
                               ``set_competition_state``.
* ``self.ruleset``          — ``violations``, ``find_exemption``.
* ``self._log_event_and_webhook`` — owned by the host class.
* ``self._apply_container_updates`` — owned by the host class.

The host class (``RefereeRuntime``) is responsible for setting these
before any of the mixin methods are called. Production wiring is in
``RefereeRuntime.__init__``.
"""
from __future__ import annotations

import logging
import statistics
from collections import Counter
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from config import SETTINGS
from poller import VariantSnapshot
from scheduler_errors import RuntimeGuardError

logger = logging.getLogger("koth.referee")


if TYPE_CHECKING:  # pragma: no cover - type-only imports
    from db import Database
    from poller import Poller
    from rules import RuleSet


class BaselineMixin:
    """Methods that operate on baselines and the snapshot matrix.

    Inherited by ``RefereeRuntime``; no ``__init__`` of its own.
    """

    # Type hints for documentation. The host class binds the actual
    # values in its ``__init__``.
    poller: "Poller"
    db: "Database"
    ruleset: "RuleSet"

    # ---- Baseline capture & violation merge --------------------------------
    def _capture_baselines(
        self, series: int, snapshots: list[VariantSnapshot]
    ) -> None:
        """Record per-(host, variant) baseline hashes for a freshly
        deployed series. Called by the deployer once a deploy passes
        its health gate; subsequent polls compare against these
        baselines via ``_merge_baseline_violations``.
        """
        for snap in snapshots:
            shadow_hash = self.poller.extract_sha256_or_missing(
                snap.sections.get("SHADOW", "")
            )
            authkeys_hash = self.poller.extract_sha256_or_missing(
                snap.sections.get("AUTHKEYS", "")
            )
            iptables_sig = self.poller.stable_signature(
                snap.sections.get("IPTABLES", "")
            )
            ports_sig = self.poller.stable_ports_signature(
                snap.sections.get("PORTS", "")
            )
            self.db.upsert_baseline(
                machine_host=snap.node_host,
                variant=snap.variant,
                series=series,
                shadow_hash=shadow_hash,
                authkeys_hash=authkeys_hash,
                iptables_sig=iptables_sig,
                ports_sig=ports_sig,
            )

    def _merge_baseline_violations(
        self,
        *,
        series: int,
        snapshots: list[VariantSnapshot],
        violations: dict[tuple[str, str], list[Any]],
    ) -> None:
        """Run every registered baseline detector against each snapshot.

        Detection itself lives in ``detectors.py``; this loop:
        (a) fetches the per-snapshot baseline from the DB, (b) computes
        the per-(series, variant) waiver set against the active rule
        set, (c) calls ``detect_all_baseline``, (d) appends non-exempt
        hits to the caller-provided accumulator.
        """
        from detectors import detect_all_baseline  # local: break cycle

        for snap in snapshots:
            baseline = self.db.get_baseline(
                machine_host=snap.node_host,
                variant=snap.variant,
                series=series,
            )
            if baseline is None:
                continue

            key = (snap.node_host, snap.variant)
            # Resolve waivers for this (series, variant) from the active
            # rule set. The team is unknown at this filter point — it
            # resolves later, after quorum picks the owner — so we pass
            # an empty string and rely on the YAML's null team scope
            # matching anything. Production exemptions are
            # team-agnostic; per-team exemptions would have to move
            # this filter further downstream.
            exempted = {
                rule_name
                for rule_name in self.ruleset.violations
                if self.ruleset.find_exemption(
                    violation_name=rule_name,
                    series=series,
                    variant=snap.variant,
                    team="",
                )
                is not None
            }

            hits = detect_all_baseline(snap, baseline)
            if hits:
                bucket = violations.setdefault(key, [])
                for hit in hits:
                    if hit.offense_name not in exempted:
                        bucket.append(hit)

    # ---- Matrix shape -----------------------------------------------------
    def _expected_snapshot_pairs(self) -> set[tuple[str, str]]:
        return {
            (host, variant)
            for host in SETTINGS.node_hosts
            for variant in SETTINGS.variants
        }

    def _snapshot_matrix_issues(
        self, snapshots: list[VariantSnapshot]
    ) -> list[str]:
        expected = self._expected_snapshot_pairs()
        actual = {(snap.node_host, snap.variant) for snap in snapshots}
        missing = sorted(expected - actual)
        extras = sorted(actual - expected)
        issues: list[str] = []
        if missing:
            rendered = ", ".join(f"{host}/{variant}" for host, variant in missing)
            issues.append(f"missing snapshots: {rendered}")
        if extras:
            rendered = ", ".join(f"{host}/{variant}" for host, variant in extras)
            issues.append(f"unexpected snapshots: {rendered}")
        return issues

    def _running_snapshot_counts_by_variant(
        self, snapshots: list[VariantSnapshot]
    ) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for snap in snapshots:
            if snap.status == "running":
                counts[snap.variant] += 1
        return {variant: counts.get(variant, 0) for variant in SETTINGS.variants}

    def _healthy_running_host_count(
        self, snapshots: list[VariantSnapshot]
    ) -> int:
        healthy_hosts = {snap.node_host for snap in snapshots if snap.status == "running"}
        return len(healthy_hosts)

    # ---- Health evaluation ------------------------------------------------
    def _evaluate_series_health(
        self,
        *,
        series: int,
        snapshots: list[VariantSnapshot],
        deploy_results: dict[str, tuple[bool, str]],
    ) -> list[str]:
        _ = series  # accepted for symmetry with the public surface; not used
        issues = self._snapshot_matrix_issues(snapshots)
        healthy_hosts: set[str] = set()
        degraded_hosts: set[str] = set()

        for host, (ok, output) in sorted(deploy_results.items()):
            if not ok:
                issues.append(f"{host}: deploy command failed: {output[:200]}")

        for snap in snapshots:
            if snap.status == "degraded":
                degraded_hosts.add(snap.node_host)
                continue
            if snap.status != "running":
                issues.append(f"{snap.node_host}/{snap.variant}: status={snap.status}")
                continue

            king = (snap.king or "").strip().lower()
            if king != "unclaimed":
                issues.append(f"{snap.node_host}/{snap.variant}: king.txt={snap.king!r}")
                continue

            healthy_hosts.add(snap.node_host)

        if len(healthy_hosts) < SETTINGS.min_healthy_nodes:
            issues.append(
                f"only {len(healthy_hosts)} healthy node(s); "
                f"MIN_HEALTHY_NODES={SETTINGS.min_healthy_nodes}"
            )

        return issues

    def _validate_current_series_or_raise(
        self, *, series: int
    ) -> list[VariantSnapshot]:
        snapshots, summary = self._validate_series_state(series=series)
        if summary["issues"]:
            raise RuntimeGuardError(
                f"Current series H{series} failed resume validation: "
                + "; ".join(summary["issues"])
            )
        self.db.set_competition_state(
            last_validated_series=series,
            last_validated_at=datetime.now(UTC).isoformat(),
        )
        return snapshots

    def _validate_series_state(
        self, *, series: int
    ) -> tuple[list[VariantSnapshot], dict[str, Any]]:
        snapshots, _ = self.poller.run_cycle(series=series)
        self._mark_clock_drift_degraded(series=series, snapshots=snapshots)
        # ``_apply_container_updates`` lives on the host class; the
        # mixin calls it via ``self`` so the host's implementation is
        # the one invoked.
        self._apply_container_updates(series, snapshots)  # type: ignore[attr-defined]

        issues = self._snapshot_matrix_issues(snapshots)
        healthy_hosts = self._healthy_running_host_count(snapshots)
        if healthy_hosts < SETTINGS.min_healthy_nodes:
            issues.append(
                f"only {healthy_hosts} healthy node(s); "
                f"MIN_HEALTHY_NODES={SETTINGS.min_healthy_nodes}"
            )
        healthy_counts = self._running_snapshot_counts_by_variant(snapshots)
        summary = {
            "current_series": series,
            "valid": not issues,
            "complete_snapshot_matrix": not any(
                issue.startswith("missing snapshots:")
                or issue.startswith("unexpected snapshots:")
                for issue in issues
            ),
            "healthy_nodes": healthy_hosts,
            "total_nodes": len(SETTINGS.node_hosts),
            "min_healthy_nodes": SETTINGS.min_healthy_nodes,
            "healthy_counts_by_variant": healthy_counts,
            "issues": issues,
        }
        return snapshots, summary

    # ---- Drift detection --------------------------------------------------
    def _mark_clock_drift_degraded(
        self, *, series: int, snapshots: list[VariantSnapshot]
    ) -> set[str]:
        """Flip ``status="degraded"`` on snapshots whose node has drifted
        past ``SETTINGS.max_clock_drift_seconds`` of the median epoch.

        The scorer's quorum logic excludes degraded snapshots, so a
        clock-drifted node simply stops counting toward quorum until
        the operator fixes its clock. Each drifted host gets one
        ``node_health`` event per call, not one per snapshot, to
        avoid event-log spam.
        """
        epochs: dict[str, int] = {}
        for snap in snapshots:
            raw = snap.sections.get("NODE_EPOCH", "")
            first = raw.splitlines()[0].strip() if raw else ""
            if not first or first == "EPOCH_FAIL":
                continue
            try:
                epochs[snap.node_host] = int(first)
            except ValueError:
                continue

        if len(epochs) < 2:
            return set()

        baseline = int(statistics.median(epochs.values()))
        degraded_hosts = {
            host
            for host, epoch in epochs.items()
            if abs(epoch - baseline) > SETTINGS.max_clock_drift_seconds
        }
        for snap in snapshots:
            if snap.node_host in degraded_hosts and snap.status == "running":
                snap.status = "degraded"

        for host in sorted(degraded_hosts):
            self._log_event_and_webhook(  # type: ignore[attr-defined]
                event_type="node_health",
                severity="warning",
                machine=host,
                series=series,
                detail="Node excluded from scoring due to clock drift",
                evidence={
                    "node_epoch": epochs.get(host),
                    "median_epoch": baseline,
                    "max_clock_drift_seconds": SETTINGS.max_clock_drift_seconds,
                },
            )
        return degraded_hosts

    # ---- Health logging --------------------------------------------------
    def _log_series_health(
        self, *, series: int, snapshots: list[VariantSnapshot]
    ) -> None:
        for snap in snapshots:
            if snap.status != "running":
                self._log_event_and_webhook(  # type: ignore[attr-defined]
                    event_type="node_health",
                    severity="critical",
                    machine=snap.node_host,
                    variant=snap.variant,
                    series=series,
                    team_name=snap.king,
                    detail="Container not healthy after deployment",
                    evidence={"status": snap.status},
                )
                continue
            if snap.king is not None and snap.king.lower() != "unclaimed":
                self._log_event_and_webhook(  # type: ignore[attr-defined]
                    event_type="node_health",
                    severity="warning",
                    machine=snap.node_host,
                    variant=snap.variant,
                    series=series,
                    team_name=snap.king,
                    detail="Container king.txt not reset to unclaimed after deploy",
                    evidence={"king": snap.king},
                )
