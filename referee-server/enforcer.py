from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from db import Database
from rules import RuleSet, load_default_ruleset


@dataclass
class EnforcementResult:
    team_name: str
    offense_count: int
    action: str


class Enforcer:
    """Maps team offenses onto disciplinary actions via a declarative rule set.

    The previous implementation hard-coded the cascade
    (1 -> warning, 2 -> series_ban, 3+ -> full_ban) inside this class.
    It now delegates to ``RuleSet.action_for_offense`` so the cascade
    is reviewable on the diff of ``rules.default.yaml`` instead of
    requiring a Python edit. Behavior is preserved on the default
    rule set; tests in ``tests/unit/test_rules.py`` pin both halves.

    The ``team_status`` returned from ``Database.increment_team_offense``
    still drives the persisted ``teams.status`` column (active /
    warned / series_banned / banned). The status string is a DB-level
    concern (it is what the public leaderboard renders) and is left as
    is — the rule engine governs the *action* surfaced to the
    scheduler / webhook / event log, not the team's persistent status.
    """

    def __init__(self, db: Database, ruleset: RuleSet | None = None):
        self._db = db
        self._ruleset = ruleset if ruleset is not None else load_default_ruleset()

    @property
    def ruleset(self) -> RuleSet:
        return self._ruleset

    def set_ruleset(self, ruleset: RuleSet) -> None:
        """Hot-swap the active rule set.

        Called by the future ``/api/admin/rules/reload`` endpoint after
        re-reading the YAML on disk. Idempotent and thread-safe under
        the assumption that callers serialize via the runtime ``RLock``
        (which they do in production).
        """
        self._ruleset = ruleset

    def escalate_team(self, team_name: str) -> EnforcementResult:
        offense_count, _status = self._db.increment_team_offense(team_name)
        action = self._ruleset.action_for_offense(offense_count)
        return EnforcementResult(team_name=team_name, offense_count=offense_count, action=action)

    def record_violation(
        self,
        *,
        team_name: str,
        machine: str,
        variant: str,
        series: int,
        offense_id: int,
        offense_name: str,
        evidence: dict[str, Any],
        action: str,
    ) -> None:
        self._db.record_violation(
            team_name=team_name,
            machine=machine,
            variant=variant,
            series=series,
            offense_id=offense_id,
            offense_name=offense_name,
            evidence=evidence,
            action_taken=action,
        )

        self._db.add_event(
            event_type="violation",
            severity="warning" if action == "warning" else "critical",
            machine=machine,
            variant=variant,
            series=series,
            team_name=team_name,
            detail=f"Violation {offense_id} ({offense_name}) -> {action}",
            evidence=evidence,
        )
