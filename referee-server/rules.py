"""Declarative rule engine for KoTH violations.

Replaces two pieces of hardcoded enforcement logic with a single,
YAML-driven model:

* the if/elif/else cascade in ``enforcer.Enforcer.escalate_team`` that
  maps offense count to disciplinary action (1 -> warning, 2 ->
  series_ban, 3+ -> full_ban);
* the ``_VIOLATION_EXEMPTIONS`` dict literal in
  ``scheduler.RefereeRuntime`` that hard-codes per-series/variant
  waivers ((1, B) -> {authkeys_changed}; (7, B) -> {shadow_changed}).

The detection layer — i.e. which shell sections trigger which named
violation — still lives in ``Poller._detect_violations`` and
``RefereeRuntime._merge_baseline_violations``. A subsequent commit
will pull those into a detector registry referenced from the same
YAML; this commit deliberately keeps that scope out so the migration
can land in stages.

YAML schema (``version: 1``):

    version: 1
    violations:
      - id: 1
        name: king_perm_changed
        severity: critical
        description: "/root/king.txt permissions deviate from 644"
    escalation:
      - on_offense_count: 1
        action: warning
      - on_offense_count: 2
        action: series_ban
      - on_offense_count: 3
        action: full_ban
    exemptions:
      - series: 1
        variant: B
        waive: [authkeys_changed]
        reason: "..."
        owner: organizer
        expires: 2026-12-31T00:00:00Z   # optional ISO-8601 (Z or offset OK)

Loaders raise ``RuleSetError`` on schema violations rather than
returning a partial object, so a malformed YAML on disk fails loud
during startup instead of silently degrading enforcement.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml


logger = logging.getLogger("koth.referee")

# Ordered from least to most severe. ``RuleSet._escalation_from_mapping``
# uses this tuple for membership checks; downstream callers can rely on
# the ordering for display purposes.
ACTIONS_IN_PRIORITY_ORDER: tuple[str, ...] = ("warning", "series_ban", "full_ban")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ViolationRule:
    """A single named violation referenced by the detection layer."""

    id: int
    name: str
    severity: str  # one of {"info", "warning", "critical"}
    description: str = ""


@dataclass(frozen=True)
class EscalationStep:
    """One row of the escalation policy.

    ``on_offense_count`` is the inclusive lower bound for the action to
    apply. The ``RuleSet.action_for_offense`` lookup walks the steps in
    ascending order and returns the action of the highest threshold the
    offense count meets, so a 5th offense with thresholds {1, 2, 3} maps
    to the action attached to threshold 3.
    """

    on_offense_count: int
    action: str  # one of ACTIONS_IN_PRIORITY_ORDER


@dataclass(frozen=True)
class Exemption:
    """A documented waiver of a specific violation in a specific scope.

    All match fields default to None, which means "match everything".
    A waiver with no match fields therefore applies globally; that is
    legitimate but rarely what an operator wants. The default YAML
    constrains every exemption to a (series, variant) pair.

    ``expires`` accepts ISO-8601 strings (``Z`` or ``+HH:MM``); naive
    datetimes are coerced to UTC. ``None`` means "never expires".
    """

    waive: tuple[str, ...]
    reason: str
    owner: str
    series: int | None = None
    variant: str | None = None
    team: str | None = None
    expires: datetime | None = None

    def is_active(self, now: datetime | None = None) -> bool:
        if self.expires is None:
            return True
        reference = now if now is not None else datetime.now(UTC)
        return reference < self.expires

    def matches(self, *, series: int, variant: str, team: str) -> bool:
        if self.series is not None and self.series != series:
            return False
        if self.variant is not None and self.variant != variant:
            return False
        if self.team is not None and self.team != team:
            return False
        return True

    def waives(self, violation_name: str) -> bool:
        return violation_name in self.waive


@dataclass(frozen=True)
class EnforcementDecision:
    """The output of ``RuleSet.evaluate``.

    ``action`` is ``"none"`` when the violation is exempt; otherwise it
    is whichever action ``RuleSet.action_for_offense`` picked for the
    given offense count. ``exempt`` and ``exemption_reason`` carry the
    "why" so the caller can render an audit-friendly event.
    """

    violation_name: str
    series: int
    variant: str
    team: str
    offense_count: int
    action: str
    exempt: bool
    exemption_reason: str | None = None
    rule: ViolationRule | None = None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class RuleSetError(ValueError):
    """Raised when a YAML document cannot be parsed into a RuleSet.

    Subclasses ``ValueError`` so callers can use the standard
    ``except ValueError`` idiom; tests that need to assert specifically
    on rule-set errors can match on this type.
    """


# ---------------------------------------------------------------------------
# RuleSet
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RuleSet:
    """Top-level container for a parsed rules YAML."""

    version: int
    violations: dict[str, ViolationRule]
    escalation: tuple[EscalationStep, ...]
    exemptions: tuple[Exemption, ...]

    # ---- Loading ----------------------------------------------------------
    @classmethod
    def from_yaml(cls, content: str) -> "RuleSet":
        try:
            data = yaml.safe_load(content) or {}
        except yaml.YAMLError as exc:
            raise RuleSetError(f"invalid YAML: {exc}") from exc
        return cls._from_mapping(data)

    @classmethod
    def from_path(cls, path: Path) -> "RuleSet":
        return cls.from_yaml(path.read_text(encoding="utf-8"))

    @classmethod
    def _from_mapping(cls, data: Any) -> "RuleSet":
        if not isinstance(data, dict):
            raise RuleSetError("rules document must be a mapping at the top level")
        version = data.get("version", 1)
        if version != 1:
            raise RuleSetError(f"unsupported rules version: {version}")

        violations: dict[str, ViolationRule] = {}
        for entry in data.get("violations", []) or []:
            if not isinstance(entry, dict):
                raise RuleSetError("each violation entry must be a mapping")
            rule = cls._violation_from_mapping(entry)
            if rule.name in violations:
                raise RuleSetError(f"duplicate violation name: {rule.name}")
            violations[rule.name] = rule

        steps: list[EscalationStep] = []
        for entry in data.get("escalation", []) or []:
            if not isinstance(entry, dict):
                raise RuleSetError("each escalation entry must be a mapping")
            steps.append(cls._escalation_from_mapping(entry))
        # Stable sort so the lookup loop in action_for_offense walks the
        # thresholds in ascending order. Equal counts retain authoring
        # order, but the schema disallows duplicates implicitly via the
        # operator's use of distinct counts.
        steps.sort(key=lambda step: step.on_offense_count)

        exemptions: list[Exemption] = []
        for entry in data.get("exemptions", []) or []:
            if not isinstance(entry, dict):
                raise RuleSetError("each exemption entry must be a mapping")
            exemptions.append(cls._exemption_from_mapping(entry))

        return cls(
            version=int(version),
            violations=violations,
            escalation=tuple(steps),
            exemptions=tuple(exemptions),
        )

    @staticmethod
    def _violation_from_mapping(entry: dict[str, Any]) -> ViolationRule:
        for required in ("id", "name", "severity"):
            if required not in entry:
                raise RuleSetError(f"violation missing required field: {required}")
        severity = str(entry["severity"])
        if severity not in ("info", "warning", "critical"):
            raise RuleSetError(f"unknown severity: {severity}")
        return ViolationRule(
            id=int(entry["id"]),
            name=str(entry["name"]),
            severity=severity,
            description=str(entry.get("description", "")),
        )

    @staticmethod
    def _escalation_from_mapping(entry: dict[str, Any]) -> EscalationStep:
        for required in ("on_offense_count", "action"):
            if required not in entry:
                raise RuleSetError(f"escalation step missing required field: {required}")
        action = str(entry["action"])
        if action not in ACTIONS_IN_PRIORITY_ORDER:
            raise RuleSetError(f"unknown action: {action}")
        count = int(entry["on_offense_count"])
        if count < 1:
            raise RuleSetError("on_offense_count must be >= 1")
        return EscalationStep(on_offense_count=count, action=action)

    @staticmethod
    def _exemption_from_mapping(entry: dict[str, Any]) -> Exemption:
        waive_raw = entry.get("waive") or []
        if not isinstance(waive_raw, list) or not waive_raw:
            raise RuleSetError("exemption requires a non-empty 'waive' list")

        expires_raw = entry.get("expires")
        expires: datetime | None = None
        if expires_raw is not None:
            try:
                if isinstance(expires_raw, datetime):
                    expires = (
                        expires_raw if expires_raw.tzinfo else expires_raw.replace(tzinfo=UTC)
                    )
                else:
                    text = str(expires_raw).replace("Z", "+00:00")
                    parsed = datetime.fromisoformat(text)
                    expires = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
            except ValueError as exc:
                raise RuleSetError(f"invalid 'expires' timestamp: {expires_raw}") from exc

        return Exemption(
            waive=tuple(str(name) for name in waive_raw),
            reason=str(entry.get("reason", "")),
            owner=str(entry.get("owner", "")),
            series=int(entry["series"]) if entry.get("series") is not None else None,
            variant=str(entry["variant"]) if entry.get("variant") is not None else None,
            team=str(entry["team"]) if entry.get("team") is not None else None,
            expires=expires,
        )

    # ---- Query API --------------------------------------------------------
    def find_violation(self, name: str) -> ViolationRule | None:
        return self.violations.get(name)

    def find_exemption(
        self,
        *,
        violation_name: str,
        series: int,
        variant: str,
        team: str,
        now: datetime | None = None,
    ) -> Exemption | None:
        for exemption in self.exemptions:
            if not exemption.is_active(now):
                continue
            if not exemption.waives(violation_name):
                continue
            if not exemption.matches(series=series, variant=variant, team=team):
                continue
            return exemption
        return None

    def action_for_offense(self, offense_count: int) -> str:
        """Return the action mapped to ``offense_count``.

        Iterates the escalation steps in ascending order of
        ``on_offense_count`` and returns the action of the highest
        threshold the offense count meets or exceeds. If no step
        matches (e.g. ``offense_count < lowest threshold``) the result
        is ``"none"``.
        """
        action = "none"
        for step in self.escalation:
            if offense_count >= step.on_offense_count:
                action = step.action
        return action

    def evaluate(
        self,
        *,
        violation_name: str,
        series: int,
        variant: str,
        team: str,
        offense_count: int,
        now: datetime | None = None,
    ) -> EnforcementDecision:
        rule = self.find_violation(violation_name)
        exemption = self.find_exemption(
            violation_name=violation_name,
            series=series,
            variant=variant,
            team=team,
            now=now,
        )
        if exemption is not None:
            return EnforcementDecision(
                violation_name=violation_name,
                series=series,
                variant=variant,
                team=team,
                offense_count=offense_count,
                action="none",
                exempt=True,
                exemption_reason=exemption.reason,
                rule=rule,
            )
        action = self.action_for_offense(offense_count)
        return EnforcementDecision(
            violation_name=violation_name,
            series=series,
            variant=variant,
            team=team,
            offense_count=offense_count,
            action=action,
            exempt=False,
            rule=rule,
        )

    # ---- Convenience accessors -------------------------------------------
    def violation_names(self) -> Sequence[str]:
        return tuple(self.violations)

    def to_dict(self) -> dict[str, Any]:
        """Render the active rule set as a JSON-friendly dict.

        Used by the introspection endpoint so an operator can verify
        what is currently loaded without ssh-ing to read the YAML on
        disk.
        """
        return {
            "version": self.version,
            "violations": [
                {
                    "id": rule.id,
                    "name": rule.name,
                    "severity": rule.severity,
                    "description": rule.description,
                }
                for rule in sorted(self.violations.values(), key=lambda r: r.id)
            ],
            "escalation": [
                {"on_offense_count": step.on_offense_count, "action": step.action}
                for step in self.escalation
            ],
            "exemptions": [
                {
                    "waive": list(exemption.waive),
                    "reason": exemption.reason,
                    "owner": exemption.owner,
                    "series": exemption.series,
                    "variant": exemption.variant,
                    "team": exemption.team,
                    "expires": exemption.expires.isoformat() if exemption.expires else None,
                }
                for exemption in self.exemptions
            ],
        }


# ---------------------------------------------------------------------------
# Default rule-set loading
# ---------------------------------------------------------------------------
_DEFAULT_RULESET_PATH = Path(__file__).parent / "rules.default.yaml"


def default_ruleset_path() -> Path:
    """Return the path to the bundled default rule set."""
    return _DEFAULT_RULESET_PATH


def load_default_ruleset() -> RuleSet:
    """Load the bundled ``rules.default.yaml``.

    Raises ``RuleSetError`` if the bundled file is malformed or
    missing — that would be a packaging bug, not a runtime condition.
    """
    return RuleSet.from_path(_DEFAULT_RULESET_PATH)
