"""Unit tests for ``rules.RuleSet`` — YAML parse, escalation lookup,
exemption matching, and the combined ``evaluate`` decision.

The rule engine sits at the centre of the enforcement pipeline; a
silent semantic drift here would cascade through every team's
discipline. Every branch is pinned with at least one positive and one
negative case so a refactor that flips a comparison or reorders the
exemption check lands on a red test, not on an operator-visible
behavior change at the next event.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rules import (
    ACTIONS_IN_PRIORITY_ORDER,
    EnforcementDecision,
    EscalationStep,
    Exemption,
    RuleSet,
    RuleSetError,
    ViolationRule,
    default_ruleset_path,
    load_default_ruleset,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_MINIMAL_RULES_YAML = """
version: 1
violations:
  - id: 1
    name: king_perm_changed
    severity: critical
    description: "perm probe"
escalation:
  - on_offense_count: 1
    action: warning
  - on_offense_count: 2
    action: series_ban
  - on_offense_count: 3
    action: full_ban
exemptions: []
"""


def _ruleset(yaml_content: str) -> RuleSet:
    return RuleSet.from_yaml(yaml_content)


# ---------------------------------------------------------------------------
# YAML parsing
# ---------------------------------------------------------------------------
class YamlParseTests:
    """Loader correctness — happy path, every required-field check, and
    every value-domain check (severity, action, version).
    """


def test_parse_minimal_valid_yaml() -> None:
    rs = _ruleset(_MINIMAL_RULES_YAML)

    assert rs.version == 1
    assert "king_perm_changed" in rs.violations
    assert rs.violations["king_perm_changed"].severity == "critical"
    assert len(rs.escalation) == 3
    assert rs.escalation[0].on_offense_count == 1
    assert rs.exemptions == ()


def test_parse_empty_document_yields_minimal_ruleset() -> None:
    # Empty YAML (or just whitespace) yields no violations / no escalation
    # / no exemptions but the version defaults to 1.
    rs = _ruleset("")
    assert rs.version == 1
    assert rs.violations == {}
    assert rs.escalation == ()
    assert rs.exemptions == ()


def test_top_level_must_be_a_mapping() -> None:
    with pytest.raises(RuleSetError, match="mapping"):
        _ruleset("- just a list\n")


def test_unsupported_version_raises() -> None:
    with pytest.raises(RuleSetError, match="version"):
        _ruleset("version: 999\n")


def test_invalid_yaml_raises_ruleset_error() -> None:
    # ``: : :`` is genuinely malformed; PyYAML wraps it in YAMLError,
    # which the loader catches and re-raises as RuleSetError.
    with pytest.raises(RuleSetError, match="invalid YAML"):
        _ruleset("violations:\n  -:\n   :\n: : :")


def test_violation_missing_required_field_raises() -> None:
    bad = """
version: 1
violations:
  - id: 1
    name: king_perm_changed
    # severity is missing
"""
    with pytest.raises(RuleSetError, match="severity"):
        _ruleset(bad)


def test_violation_unknown_severity_raises() -> None:
    bad = """
version: 1
violations:
  - id: 1
    name: x
    severity: extreme
"""
    with pytest.raises(RuleSetError, match="severity"):
        _ruleset(bad)


def test_violation_duplicate_name_raises() -> None:
    bad = """
version: 1
violations:
  - {id: 1, name: dup, severity: critical}
  - {id: 2, name: dup, severity: critical}
"""
    with pytest.raises(RuleSetError, match="duplicate"):
        _ruleset(bad)


def test_violation_entry_must_be_a_mapping() -> None:
    bad = """
version: 1
violations:
  - "string-not-a-mapping"
"""
    with pytest.raises(RuleSetError, match="mapping"):
        _ruleset(bad)


def test_escalation_unknown_action_raises() -> None:
    bad = """
version: 1
escalation:
  - on_offense_count: 1
    action: pillory
"""
    with pytest.raises(RuleSetError, match="action"):
        _ruleset(bad)


def test_escalation_zero_offense_count_raises() -> None:
    bad = """
version: 1
escalation:
  - on_offense_count: 0
    action: warning
"""
    with pytest.raises(RuleSetError, match=">= 1"):
        _ruleset(bad)


def test_escalation_negative_offense_count_raises() -> None:
    bad = """
version: 1
escalation:
  - on_offense_count: -1
    action: warning
"""
    with pytest.raises(RuleSetError, match=">= 1"):
        _ruleset(bad)


def test_escalation_steps_are_sorted_by_threshold() -> None:
    # Author the steps out of order; the loader sorts them so the
    # action_for_offense walk is deterministic.
    yaml_text = """
version: 1
escalation:
  - on_offense_count: 5
    action: full_ban
  - on_offense_count: 2
    action: series_ban
  - on_offense_count: 1
    action: warning
"""
    rs = _ruleset(yaml_text)
    counts = [step.on_offense_count for step in rs.escalation]
    assert counts == sorted(counts)


def test_exemption_requires_nonempty_waive_list() -> None:
    bad = """
version: 1
exemptions:
  - series: 1
    variant: B
    waive: []
    reason: missing waive
"""
    with pytest.raises(RuleSetError, match="waive"):
        _ruleset(bad)


def test_exemption_invalid_expires_timestamp_raises() -> None:
    bad = """
version: 1
exemptions:
  - waive: [authkeys_changed]
    reason: x
    expires: not-a-timestamp
"""
    with pytest.raises(RuleSetError, match="expires"):
        _ruleset(bad)


def test_exemption_expires_z_suffix_is_parsed_as_utc() -> None:
    yaml_text = """
version: 1
exemptions:
  - waive: [shadow_changed]
    reason: scheduled waiver
    expires: "2026-12-31T00:00:00Z"
"""
    rs = _ruleset(yaml_text)
    assert rs.exemptions[0].expires == datetime(2026, 12, 31, tzinfo=UTC)


def test_exemption_naive_datetime_is_coerced_to_utc() -> None:
    # PyYAML parses "2026-12-31 00:00:00" as a naive datetime; the
    # loader must coerce to UTC so all subsequent comparisons are
    # timezone-aware.
    yaml_text = """
version: 1
exemptions:
  - waive: [shadow_changed]
    reason: scheduled waiver
    expires: 2026-12-31 00:00:00
"""
    rs = _ruleset(yaml_text)
    expires = rs.exemptions[0].expires
    assert expires is not None
    assert expires.tzinfo is not None


# ---------------------------------------------------------------------------
# action_for_offense
# ---------------------------------------------------------------------------
def test_action_for_offense_uses_highest_matching_threshold() -> None:
    rs = _ruleset(_MINIMAL_RULES_YAML)
    assert rs.action_for_offense(1) == "warning"
    assert rs.action_for_offense(2) == "series_ban"
    assert rs.action_for_offense(3) == "full_ban"
    # Beyond the last threshold, the action stays at the highest step.
    assert rs.action_for_offense(99) == "full_ban"


def test_action_for_offense_below_lowest_threshold_returns_none_string() -> None:
    rs = _ruleset(_MINIMAL_RULES_YAML)
    assert rs.action_for_offense(0) == "none"
    assert rs.action_for_offense(-1) == "none"


def test_action_for_offense_with_no_escalation_returns_none() -> None:
    yaml_text = """
version: 1
escalation: []
"""
    rs = _ruleset(yaml_text)
    assert rs.action_for_offense(5) == "none"


def test_action_for_offense_with_sparse_thresholds() -> None:
    # Operators may skip levels, e.g. "no warning, jump straight to
    # series_ban on the 1st offense, full_ban on the 5th". The lookup
    # must still pick the highest matching threshold.
    yaml_text = """
version: 1
escalation:
  - on_offense_count: 1
    action: series_ban
  - on_offense_count: 5
    action: full_ban
"""
    rs = _ruleset(yaml_text)
    assert rs.action_for_offense(0) == "none"
    assert rs.action_for_offense(1) == "series_ban"
    assert rs.action_for_offense(4) == "series_ban"
    assert rs.action_for_offense(5) == "full_ban"
    assert rs.action_for_offense(6) == "full_ban"


# ---------------------------------------------------------------------------
# Exemption matching
# ---------------------------------------------------------------------------
def _exemption(**overrides) -> Exemption:
    base = {
        "waive": ("authkeys_changed",),
        "reason": "test",
        "owner": "test-owner",
        "series": None,
        "variant": None,
        "team": None,
        "expires": None,
    }
    base.update(overrides)
    return Exemption(**base)


def test_exemption_with_no_match_fields_matches_anything() -> None:
    exemption = _exemption()
    assert exemption.matches(series=1, variant="A", team="Team Alpha")
    assert exemption.matches(series=99, variant="Z", team="Anything")


def test_exemption_with_series_only() -> None:
    exemption = _exemption(series=1)
    assert exemption.matches(series=1, variant="A", team="Team Alpha")
    assert not exemption.matches(series=2, variant="A", team="Team Alpha")


def test_exemption_with_variant_only() -> None:
    exemption = _exemption(variant="B")
    assert exemption.matches(series=1, variant="B", team="Team Alpha")
    assert not exemption.matches(series=1, variant="A", team="Team Alpha")


def test_exemption_with_team_only() -> None:
    exemption = _exemption(team="Team Alpha")
    assert exemption.matches(series=1, variant="A", team="Team Alpha")
    assert not exemption.matches(series=1, variant="A", team="Team Beta")


def test_exemption_combined_match_requires_all_fields() -> None:
    exemption = _exemption(series=1, variant="B", team="Team Alpha")
    assert exemption.matches(series=1, variant="B", team="Team Alpha")
    assert not exemption.matches(series=1, variant="B", team="Team Beta")
    assert not exemption.matches(series=1, variant="A", team="Team Alpha")
    assert not exemption.matches(series=2, variant="B", team="Team Alpha")


def test_exemption_waives_only_listed_violations() -> None:
    exemption = _exemption(waive=("shadow_changed", "authkeys_changed"))
    assert exemption.waives("shadow_changed")
    assert exemption.waives("authkeys_changed")
    assert not exemption.waives("king_deleted")


def test_exemption_with_no_expires_is_always_active() -> None:
    exemption = _exemption()
    assert exemption.is_active()
    assert exemption.is_active(now=datetime(3000, 1, 1, tzinfo=UTC))


def test_exemption_expires_in_the_future_is_active() -> None:
    exemption = _exemption(expires=datetime(2030, 1, 1, tzinfo=UTC))
    assert exemption.is_active(now=datetime(2026, 4, 25, tzinfo=UTC))


def test_exemption_expires_in_the_past_is_inactive() -> None:
    exemption = _exemption(expires=datetime(2020, 1, 1, tzinfo=UTC))
    assert not exemption.is_active(now=datetime(2026, 4, 25, tzinfo=UTC))


def test_exemption_expires_at_the_reference_time_is_inactive() -> None:
    # The boundary is strict ``<``: an exemption that expires AT the
    # reference time has already lapsed.
    expiry = datetime(2026, 4, 25, 12, tzinfo=UTC)
    exemption = _exemption(expires=expiry)
    assert not exemption.is_active(now=expiry)


# ---------------------------------------------------------------------------
# find_exemption (the integration of waive + match + active)
# ---------------------------------------------------------------------------
def test_find_exemption_skips_inactive_entries() -> None:
    yaml_text = """
version: 1
exemptions:
  - waive: [authkeys_changed]
    reason: expired
    series: 1
    variant: B
    expires: "2020-01-01T00:00:00Z"
  - waive: [authkeys_changed]
    reason: still active
    series: 1
    variant: B
"""
    rs = _ruleset(yaml_text)
    found = rs.find_exemption(
        violation_name="authkeys_changed",
        series=1,
        variant="B",
        team="Team Alpha",
    )
    assert found is not None
    assert found.reason == "still active"


def test_find_exemption_returns_first_matching_entry() -> None:
    # Authoring order matters when two exemptions both match — the
    # first wins. Operators should not write overlapping exemptions
    # but if they do, the order is deterministic.
    yaml_text = """
version: 1
exemptions:
  - waive: [shadow_changed]
    reason: more specific
    series: 7
    variant: B
  - waive: [shadow_changed]
    reason: catch-all
"""
    rs = _ruleset(yaml_text)
    found = rs.find_exemption(
        violation_name="shadow_changed", series=7, variant="B", team="any"
    )
    assert found is not None
    assert found.reason == "more specific"


def test_find_exemption_returns_none_when_no_match() -> None:
    yaml_text = """
version: 1
exemptions:
  - waive: [authkeys_changed]
    series: 1
    variant: B
    reason: only H1B
"""
    rs = _ruleset(yaml_text)
    found = rs.find_exemption(
        violation_name="authkeys_changed", series=2, variant="A", team="Team Alpha"
    )
    assert found is None


# ---------------------------------------------------------------------------
# evaluate (the high-level decision)
# ---------------------------------------------------------------------------
def _full_ruleset() -> RuleSet:
    return _ruleset(
        """
version: 1
violations:
  - {id: 1, name: king_perm_changed, severity: critical}
  - {id: 15, name: authkeys_changed, severity: critical}
escalation:
  - {on_offense_count: 1, action: warning}
  - {on_offense_count: 2, action: series_ban}
  - {on_offense_count: 3, action: full_ban}
exemptions:
  - waive: [authkeys_changed]
    series: 1
    variant: B
    reason: H1B intentional privesc path
"""
    )


def test_evaluate_non_exempt_violation_returns_escalation_action() -> None:
    rs = _full_ruleset()
    decision = rs.evaluate(
        violation_name="king_perm_changed",
        series=1,
        variant="A",
        team="Team Alpha",
        offense_count=1,
    )
    assert decision.action == "warning"
    assert decision.exempt is False
    assert decision.exemption_reason is None
    assert decision.rule is not None
    assert decision.rule.name == "king_perm_changed"


def test_evaluate_exempt_violation_returns_action_none() -> None:
    rs = _full_ruleset()
    decision = rs.evaluate(
        violation_name="authkeys_changed",
        series=1,
        variant="B",
        team="Team Alpha",
        offense_count=1,
    )
    assert decision.action == "none"
    assert decision.exempt is True
    assert "intentional" in decision.exemption_reason
    assert decision.rule is not None
    assert decision.rule.name == "authkeys_changed"


def test_evaluate_returns_decision_with_unknown_violation_name() -> None:
    # An unknown violation name is not an error — the engine still
    # returns a decision with rule=None so the caller can audit-log it.
    # That matters because the detection layer can in principle emit a
    # name the rule set hasn't been updated for; failing closed there
    # would silently drop enforcement.
    rs = _full_ruleset()
    decision = rs.evaluate(
        violation_name="ghost_violation",
        series=1,
        variant="A",
        team="Team Alpha",
        offense_count=2,
    )
    assert decision.rule is None
    assert decision.exempt is False
    assert decision.action == "series_ban"


def test_evaluate_exemption_takes_precedence_over_escalation() -> None:
    # Even at offense_count=99 (full_ban territory) an exempt violation
    # produces action="none". This pins the order-of-operations: the
    # exemption check runs before action lookup, not after.
    rs = _full_ruleset()
    decision = rs.evaluate(
        violation_name="authkeys_changed",
        series=1,
        variant="B",
        team="Team Alpha",
        offense_count=99,
    )
    assert decision.action == "none"
    assert decision.exempt is True


def test_evaluate_uses_explicit_now_for_expiry_check() -> None:
    yaml_text = """
version: 1
violations:
  - {id: 15, name: authkeys_changed, severity: critical}
escalation:
  - {on_offense_count: 1, action: warning}
exemptions:
  - waive: [authkeys_changed]
    series: 1
    variant: B
    reason: expires soon
    expires: "2026-06-01T00:00:00Z"
"""
    rs = _ruleset(yaml_text)

    # Before expiry — exempt.
    before = rs.evaluate(
        violation_name="authkeys_changed",
        series=1,
        variant="B",
        team="t",
        offense_count=1,
        now=datetime(2026, 5, 1, tzinfo=UTC),
    )
    assert before.exempt

    # After expiry — escalates normally.
    after = rs.evaluate(
        violation_name="authkeys_changed",
        series=1,
        variant="B",
        team="t",
        offense_count=1,
        now=datetime(2026, 7, 1, tzinfo=UTC),
    )
    assert not after.exempt
    assert after.action == "warning"


# ---------------------------------------------------------------------------
# Default rule-set roundtrip
# ---------------------------------------------------------------------------
def test_default_ruleset_loads_and_pins_existing_behavior() -> None:
    # Mirrors the legacy hardcoded behavior: 12 violations, three
    # escalation steps, two exemptions for (1, B) and (7, B).
    rs = load_default_ruleset()

    assert rs.version == 1

    expected_names = {
        "king_perm_changed",
        "king_owner_changed",
        "king_immutable",
        "king_deleted",
        "king_not_regular",
        "root_dir_perm_changed",
        "cron_king_persistence",
        "watchdog_process",
        "service_ports_changed",
        "iptables_changed",
        "shadow_changed",
        "authkeys_changed",
    }
    assert set(rs.violations) == expected_names

    actions = [step.action for step in rs.escalation]
    assert actions == ["warning", "series_ban", "full_ban"]

    # Two exemptions, scoped exactly as the legacy code's
    # _VIOLATION_EXEMPTIONS dict literal.
    waivers = {(e.series, e.variant): set(e.waive) for e in rs.exemptions}
    assert waivers == {
        (1, "B"): {"authkeys_changed"},
        (7, "B"): {"shadow_changed"},
    }


def test_default_ruleset_path_points_at_yaml_next_to_module() -> None:
    path = default_ruleset_path()
    assert path.exists()
    assert path.suffix == ".yaml"


def test_default_ruleset_evaluate_pins_legacy_h1b_authkeys_exemption() -> None:
    rs = load_default_ruleset()
    decision = rs.evaluate(
        violation_name="authkeys_changed",
        series=1,
        variant="B",
        team="Team Alpha",
        offense_count=1,
    )
    assert decision.exempt is True


def test_default_ruleset_evaluate_pins_legacy_h7b_shadow_exemption() -> None:
    rs = load_default_ruleset()
    decision = rs.evaluate(
        violation_name="shadow_changed",
        series=7,
        variant="B",
        team="Team Alpha",
        offense_count=1,
    )
    assert decision.exempt is True


def test_default_ruleset_evaluate_pins_h1b_unrelated_violation_is_not_exempt() -> None:
    # The (1, B) exemption only waives authkeys_changed, NOT every
    # violation on H1B. A king_perm_changed on H1B must still escalate.
    rs = load_default_ruleset()
    decision = rs.evaluate(
        violation_name="king_perm_changed",
        series=1,
        variant="B",
        team="Team Alpha",
        offense_count=2,
    )
    assert decision.exempt is False
    assert decision.action == "series_ban"


# ---------------------------------------------------------------------------
# to_dict / introspection
# ---------------------------------------------------------------------------
def test_to_dict_round_trips_through_yaml_load() -> None:
    rs = load_default_ruleset()
    rendered = rs.to_dict()

    # Top-level shape.
    assert rendered["version"] == 1
    assert isinstance(rendered["violations"], list)
    assert isinstance(rendered["escalation"], list)
    assert isinstance(rendered["exemptions"], list)

    # Sorted by id for stable display.
    ids = [v["id"] for v in rendered["violations"]]
    assert ids == sorted(ids)

    # Exemption expires=None survives the trip; concrete timestamps
    # are emitted as ISO strings.
    for entry in rendered["exemptions"]:
        assert entry["expires"] is None or isinstance(entry["expires"], str)


# ---------------------------------------------------------------------------
# Constants and types
# ---------------------------------------------------------------------------
def test_actions_in_priority_order_is_low_to_high() -> None:
    assert ACTIONS_IN_PRIORITY_ORDER == ("warning", "series_ban", "full_ban")


def test_violation_rule_dataclass_is_frozen() -> None:
    rule = ViolationRule(id=1, name="x", severity="warning")
    with pytest.raises((AttributeError, TypeError)):
        rule.id = 99  # type: ignore[misc]


def test_escalation_step_dataclass_is_frozen() -> None:
    step = EscalationStep(on_offense_count=1, action="warning")
    with pytest.raises((AttributeError, TypeError)):
        step.action = "full_ban"  # type: ignore[misc]


def test_exemption_dataclass_is_frozen() -> None:
    exemption = _exemption()
    with pytest.raises((AttributeError, TypeError)):
        exemption.reason = "hijacked"  # type: ignore[misc]


def test_ruleset_dataclass_is_frozen() -> None:
    rs = _ruleset(_MINIMAL_RULES_YAML)
    with pytest.raises((AttributeError, TypeError)):
        rs.version = 2  # type: ignore[misc]


def test_enforcement_decision_dataclass_is_frozen() -> None:
    decision = EnforcementDecision(
        violation_name="x",
        series=1,
        variant="A",
        team="t",
        offense_count=0,
        action="none",
        exempt=False,
    )
    with pytest.raises((AttributeError, TypeError)):
        decision.action = "warning"  # type: ignore[misc]
