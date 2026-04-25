"""Direct unit tests for ``enforcer.Enforcer``.

The enforcer has two responsibilities:

1. Escalate a team's offense counter and map the new counter to one of
   three disciplinary actions (``warning``, ``series_ban``, ``full_ban``).
2. Persist a violation record and a matching event row to the database.

These tests pin both paths directly against a tmp-path SQLite database,
without going through a full ``RefereeRuntime`` instance.
"""
from __future__ import annotations

import pytest

from db import Database
from enforcer import EnforcementResult, Enforcer


pytestmark = pytest.mark.unit


@pytest.fixture
def enforcer_and_db(tmp_db: Database) -> tuple[Enforcer, Database]:
    tmp_db.upsert_team_names(["Team Alpha", "Team Beta"])
    return Enforcer(tmp_db), tmp_db


def test_first_offense_yields_warning(enforcer_and_db: tuple[Enforcer, Database]) -> None:
    enforcer, db = enforcer_and_db

    result = enforcer.escalate_team("Team Alpha")

    assert isinstance(result, EnforcementResult)
    assert result.team_name == "Team Alpha"
    assert result.offense_count == 1
    assert result.action == "warning"

    team = db.get_team("Team Alpha")
    assert team["offense_count"] == 1
    assert team["status"] == "warned"


def test_second_offense_yields_series_ban(enforcer_and_db: tuple[Enforcer, Database]) -> None:
    enforcer, db = enforcer_and_db

    enforcer.escalate_team("Team Alpha")
    result = enforcer.escalate_team("Team Alpha")

    assert result.offense_count == 2
    assert result.action == "series_ban"
    assert db.get_team("Team Alpha")["status"] == "series_banned"


def test_third_offense_yields_full_ban(enforcer_and_db: tuple[Enforcer, Database]) -> None:
    enforcer, _ = enforcer_and_db

    for _ in range(3):
        result = enforcer.escalate_team("Team Alpha")

    assert result.offense_count == 3
    assert result.action == "full_ban"


def test_fourth_and_later_offenses_stay_full_ban(enforcer_and_db: tuple[Enforcer, Database]) -> None:
    """Once banned, further escalations keep returning ``full_ban`` — the
    offense counter keeps incrementing but the action does not escalate
    further. Documenting the current behavior so any future change is
    explicit.
    """
    enforcer, _ = enforcer_and_db

    for _ in range(5):
        result = enforcer.escalate_team("Team Alpha")

    assert result.offense_count == 5
    assert result.action == "full_ban"


def test_escalation_does_not_cross_team_boundaries(
    enforcer_and_db: tuple[Enforcer, Database],
) -> None:
    enforcer, db = enforcer_and_db

    enforcer.escalate_team("Team Alpha")
    enforcer.escalate_team("Team Alpha")
    result = enforcer.escalate_team("Team Beta")

    assert result.offense_count == 1
    assert result.action == "warning"
    assert db.get_team("Team Alpha")["offense_count"] == 2
    assert db.get_team("Team Beta")["offense_count"] == 1


def test_escalate_unknown_team_raises(tmp_db: Database) -> None:
    enforcer = Enforcer(tmp_db)

    with pytest.raises(ValueError):
        enforcer.escalate_team("Ghost Team")


def test_record_violation_writes_violation_row_and_event(
    enforcer_and_db: tuple[Enforcer, Database],
) -> None:
    enforcer, db = enforcer_and_db

    enforcer.record_violation(
        team_name="Team Alpha",
        machine="machineH2A",
        variant="A",
        series=2,
        offense_id=1,
        offense_name="king_perm_changed",
        evidence={"perm": "600"},
        action="warning",
    )

    violations = db.list_violations()
    assert len(violations) == 1
    assert violations[0]["offense_name"] == "king_perm_changed"
    assert violations[0]["action_taken"] == "warning"

    events = db.list_events(limit=5)
    assert any(
        event["type"] == "violation"
        and event["team_name"] == "Team Alpha"
        and "king_perm_changed" in event["detail"]
        for event in events
    )


def test_record_violation_maps_non_warning_action_to_critical_severity(
    enforcer_and_db: tuple[Enforcer, Database],
) -> None:
    enforcer, db = enforcer_and_db

    enforcer.record_violation(
        team_name="Team Alpha",
        machine="machineH2A",
        variant="A",
        series=2,
        offense_id=3,
        offense_name="king_immutable",
        evidence={"lsattr": "---i-----"},
        action="full_ban",
    )

    events = db.list_events(limit=5)
    violation_event = next(event for event in events if event["type"] == "violation")
    assert violation_event["severity"] == "critical"


def test_record_violation_maps_warning_action_to_warning_severity(
    enforcer_and_db: tuple[Enforcer, Database],
) -> None:
    enforcer, db = enforcer_and_db

    enforcer.record_violation(
        team_name="Team Alpha",
        machine="machineH2A",
        variant="A",
        series=2,
        offense_id=1,
        offense_name="king_perm_changed",
        evidence={"perm": "600"},
        action="warning",
    )

    events = db.list_events(limit=5)
    violation_event = next(event for event in events if event["type"] == "violation")
    assert violation_event["severity"] == "warning"


# ---------------------------------------------------------------------------
# Rule-engine integration.
#
# These tests pin that the Enforcer is actually consulting the
# RuleSet's escalation policy rather than carrying a hardcoded
# if-chain. Each test injects a custom RuleSet whose escalation
# differs from the default and asserts the action surfaces through
# escalate_team unchanged.
# ---------------------------------------------------------------------------
def test_enforcer_uses_default_ruleset_when_none_passed(tmp_db: Database) -> None:
    # No ruleset arg -> load_default_ruleset() result.
    from rules import load_default_ruleset

    enforcer = Enforcer(tmp_db)
    assert enforcer.ruleset.action_for_offense(1) == "warning"
    assert enforcer.ruleset.action_for_offense(2) == "series_ban"
    assert enforcer.ruleset.action_for_offense(3) == "full_ban"
    # Same object identity is not guaranteed; same content is.
    expected = load_default_ruleset()
    assert enforcer.ruleset.violation_names() == expected.violation_names()


def test_enforcer_respects_custom_escalation_policy(tmp_db: Database) -> None:
    # Custom rule set: skip "warning", go straight to series_ban on
    # the 1st offense, full_ban on the 2nd. Pinning that escalate_team
    # surfaces these actions confirms the if-chain has been fully
    # replaced.
    from rules import RuleSet

    yaml_text = """
version: 1
escalation:
  - on_offense_count: 1
    action: series_ban
  - on_offense_count: 2
    action: full_ban
"""
    enforcer = Enforcer(tmp_db, ruleset=RuleSet.from_yaml(yaml_text))
    tmp_db.upsert_team_names(["Team Alpha"])

    first = enforcer.escalate_team("Team Alpha")
    second = enforcer.escalate_team("Team Alpha")

    assert first.action == "series_ban"
    assert second.action == "full_ban"


def test_enforcer_set_ruleset_swaps_active_policy(tmp_db: Database) -> None:
    from rules import RuleSet

    enforcer = Enforcer(tmp_db)
    assert enforcer.ruleset.action_for_offense(1) == "warning"

    # Hot-swap to a no-op ruleset.
    enforcer.set_ruleset(RuleSet.from_yaml("version: 1\nescalation: []\n"))
    tmp_db.upsert_team_names(["Team Alpha"])
    decision = enforcer.escalate_team("Team Alpha")

    # No escalation step -> action_for_offense returns "none".
    assert decision.action == "none"
