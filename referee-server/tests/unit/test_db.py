"""Direct unit tests for the ``db.Database`` repository.

The database layer is 932 lines and has historically only been exercised
as a side effect of integration tests. These tests pin the repository
surface directly against a tmp-path SQLite file: schema initialisation,
team lifecycle, points accounting, ownership, claim observations, events,
baselines, and the reset path used between competitions.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from db import Database


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Initialisation and idempotency
# ---------------------------------------------------------------------------
def test_initialize_is_idempotent(tmp_db: Database) -> None:
    """Re-running initialize() on an already-initialised DB must not raise,
    duplicate the seed row, or recreate tables. The server calls this on
    every startup so idempotency is load-bearing.
    """
    tmp_db.initialize()
    state = tmp_db.get_competition()
    assert state["status"] == "stopped"
    assert state["current_series"] == 0
    assert state["poll_cycle"] == 0


def test_initialize_seeds_public_dashboard_config(tmp_db: Database) -> None:
    cfg = tmp_db.get_public_dashboard_config()
    assert cfg == {
        "orchestrator_host": None,
        "port_ranges": None,
        "headline": None,
        "subheadline": None,
        "updated_at": None,
    }


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------
def test_upsert_team_names_deduplicates(tmp_db: Database) -> None:
    tmp_db.upsert_team_names(["Team Alpha"])
    tmp_db.upsert_team_names(["Team Alpha", "Team Beta"])

    teams = {row["name"] for row in tmp_db.list_teams()}
    assert teams == {"Team Alpha", "Team Beta"}
    assert tmp_db.team_count() == 2


def test_create_team_returns_default_fields(tmp_db: Database) -> None:
    team = tmp_db.create_team("Team Alpha")

    assert team["name"] == "Team Alpha"
    assert team["status"] == "active"
    assert team["offense_count"] == 0
    assert team["total_points"] == 0


def test_team_exists_is_case_sensitive(tmp_db: Database) -> None:
    tmp_db.upsert_team_names(["Team Alpha"])
    assert tmp_db.team_exists("Team Alpha") is True
    assert tmp_db.team_exists("team alpha") is False


def test_increment_team_offense_escalation_boundary(tmp_db: Database) -> None:
    tmp_db.upsert_team_names(["Team Alpha"])

    assert tmp_db.increment_team_offense("Team Alpha") == (1, "warned")
    assert tmp_db.increment_team_offense("Team Alpha") == (2, "series_banned")
    assert tmp_db.increment_team_offense("Team Alpha") == (3, "banned")
    assert tmp_db.increment_team_offense("Team Alpha") == (4, "banned")


def test_increment_team_offense_unknown_team_raises(tmp_db: Database) -> None:
    with pytest.raises(ValueError):
        tmp_db.increment_team_offense("Ghost Team")


def test_set_team_status_updates_fields(tmp_db: Database) -> None:
    tmp_db.upsert_team_names(["Team Alpha"])
    updated = tmp_db.set_team_status("Team Alpha", status="warned", offense_count=1)
    assert updated["status"] == "warned"
    assert updated["offense_count"] == 1


def test_set_team_status_unknown_team_raises(tmp_db: Database) -> None:
    with pytest.raises(KeyError):
        tmp_db.set_team_status("Ghost Team", status="warned")


def test_reset_series_bans_promotes_series_banned_to_warned(tmp_db: Database) -> None:
    tmp_db.upsert_team_names(["Team Alpha", "Team Beta"])
    tmp_db.set_team_status("Team Alpha", status="series_banned", offense_count=2)
    tmp_db.set_team_status("Team Beta", status="banned", offense_count=3)

    tmp_db.reset_series_bans()

    assert tmp_db.get_team("Team Alpha")["status"] == "warned"
    # Full ban is sticky across rotations.
    assert tmp_db.get_team("Team Beta")["status"] == "banned"


# ---------------------------------------------------------------------------
# Points
# ---------------------------------------------------------------------------
def test_add_points_accumulates_on_team(tmp_db: Database) -> None:
    tmp_db.upsert_team_names(["Team Alpha"])

    tmp_db.add_points("Team Alpha", "A", 1, 1.0, 1)
    tmp_db.add_points("Team Alpha", "B", 1, 0.5, 2)

    assert tmp_db.get_team("Team Alpha")["total_points"] == 1.5
    events = tmp_db.list_point_events(team_names=["Team Alpha"])
    assert [event["points"] for event in events] == [1.0, 0.5]


def test_list_point_events_filter_by_team(tmp_db: Database) -> None:
    tmp_db.upsert_team_names(["Team Alpha", "Team Beta"])
    tmp_db.add_points("Team Alpha", "A", 1, 1.0, 1)
    tmp_db.add_points("Team Beta", "A", 1, 2.0, 1)

    alpha = tmp_db.list_point_events(team_names=["Team Alpha"])
    assert {event["team_name"] for event in alpha} == {"Team Alpha"}


def test_increment_poll_cycle_advances_counter_and_stamps_time(tmp_db: Database) -> None:
    before = datetime.now(UTC)
    poll_cycle = tmp_db.increment_poll_cycle()
    assert poll_cycle == 1
    state = tmp_db.get_competition()
    assert datetime.fromisoformat(state["last_poll_at"]) >= before


# ---------------------------------------------------------------------------
# Variant ownership
# ---------------------------------------------------------------------------
def test_set_and_get_variant_owner_roundtrip(tmp_db: Database) -> None:
    tmp_db.set_variant_owner(
        series=3,
        variant="A",
        owner_team="Team Alpha",
        accepted_mtime_epoch=1234,
        source_node_host="192.168.0.102",
        evidence={"reason": "quorum"},
    )

    owner = tmp_db.get_variant_owner(series=3, variant="A")
    assert owner is not None
    assert owner["owner_team"] == "Team Alpha"
    assert owner["accepted_mtime_epoch"] == 1234
    assert owner["evidence_json"] == {"reason": "quorum"}


def test_set_variant_owner_upserts_on_conflict(tmp_db: Database) -> None:
    tmp_db.set_variant_owner(
        series=3,
        variant="A",
        owner_team="Team Alpha",
        accepted_mtime_epoch=1234,
        source_node_host="192.168.0.102",
    )
    tmp_db.set_variant_owner(
        series=3,
        variant="A",
        owner_team="Team Beta",
        accepted_mtime_epoch=1300,
        source_node_host="192.168.0.103",
    )

    owner = tmp_db.get_variant_owner(series=3, variant="A")
    assert owner["owner_team"] == "Team Beta"
    assert owner["accepted_mtime_epoch"] == 1300


def test_list_variant_owners_is_ordered_by_variant(tmp_db: Database) -> None:
    for variant in ("C", "A", "B"):
        tmp_db.set_variant_owner(
            series=2,
            variant=variant,
            owner_team=f"Team {variant}",
            accepted_mtime_epoch=1000,
            source_node_host="192.168.0.102",
        )

    owners = tmp_db.list_variant_owners(series=2)
    assert [owner["variant"] for owner in owners] == ["A", "B", "C"]


# ---------------------------------------------------------------------------
# Events, violations, and active_violations
# ---------------------------------------------------------------------------
def test_add_event_roundtrips_evidence_json(tmp_db: Database) -> None:
    tmp_db.add_event(
        event_type="violation",
        severity="critical",
        detail="king_deleted",
        machine="machineH2A",
        variant="A",
        series=2,
        team_name="Team Alpha",
        evidence={"king": "FILE_MISSING"},
    )

    events = tmp_db.list_events(limit=5)
    assert events[0]["evidence"] == {"king": "FILE_MISSING"}


def test_list_events_supports_type_filter(tmp_db: Database) -> None:
    tmp_db.add_event(event_type="violation", severity="warning", detail="x")
    tmp_db.add_event(event_type="ban", severity="critical", detail="y")

    violations = tmp_db.list_events(limit=5, event_type="violation")
    assert [event["type"] for event in violations] == ["violation"]


def test_record_violation_stores_evidence_json_and_action(tmp_db: Database) -> None:
    tmp_db.record_violation(
        team_name="Team Alpha",
        machine="machineH2A",
        variant="A",
        series=2,
        offense_id=1,
        offense_name="king_perm_changed",
        evidence={"perm": "600"},
        action_taken="warning",
    )

    rows = tmp_db.list_violations()
    assert len(rows) == 1
    assert rows[0]["action_taken"] == "warning"


def test_active_violations_replace_is_idempotent(tmp_db: Database) -> None:
    entries = {
        ("Team Alpha", "machineH2A", "A", 2, "king_perm_changed", "sig1"),
    }
    tmp_db.replace_active_violations(series=2, entries=entries)
    assert tmp_db.get_active_violation_keys(series=2) == entries

    tmp_db.replace_active_violations(series=2, entries=set())
    assert tmp_db.get_active_violation_keys(series=2) == set()


# ---------------------------------------------------------------------------
# Containers, baselines, claim observations
# ---------------------------------------------------------------------------
def test_upsert_container_status_upserts_on_conflict(tmp_db: Database) -> None:
    now = datetime.now(UTC).isoformat()
    tmp_db.upsert_container_status(
        machine_host="192.168.0.102",
        variant="A",
        container_id="c1",
        series=1,
        status="running",
        king="unclaimed",
        king_mtime_epoch=1,
        last_checked=now,
    )
    tmp_db.upsert_container_status(
        machine_host="192.168.0.102",
        variant="A",
        container_id="c2",
        series=1,
        status="degraded",
        king="Team Alpha",
        king_mtime_epoch=2,
        last_checked=now,
    )

    containers = tmp_db.list_containers(series=1)
    assert len(containers) == 1
    assert containers[0]["container_id"] == "c2"
    assert containers[0]["status"] == "degraded"


def test_upsert_baseline_upserts_on_conflict(tmp_db: Database) -> None:
    tmp_db.upsert_baseline(
        machine_host="192.168.0.102",
        variant="A",
        series=1,
        shadow_hash="old",
        authkeys_hash="old",
        iptables_sig="ok",
        ports_sig="ok",
    )
    tmp_db.upsert_baseline(
        machine_host="192.168.0.102",
        variant="A",
        series=1,
        shadow_hash="new",
        authkeys_hash="new",
        iptables_sig="ok",
        ports_sig="ok",
    )

    baseline = tmp_db.get_baseline(machine_host="192.168.0.102", variant="A", series=1)
    assert baseline["shadow_hash"] == "new"


def test_get_baseline_returns_none_when_missing(tmp_db: Database) -> None:
    assert tmp_db.get_baseline(machine_host="unknown", variant="A", series=1) is None


def test_add_claim_observations_persists_rows(tmp_db: Database) -> None:
    now = datetime.now(UTC).isoformat()
    tmp_db.add_claim_observations(
        [
            {
                "poll_cycle": 1,
                "series": 2,
                "node_host": "192.168.0.102",
                "variant": "A",
                "status": "running",
                "king": "Team Alpha",
                "king_mtime_epoch": 100,
                "observed_at": now,
                "selected": True,
                "selection_reason": "quorum",
            }
        ]
    )

    rows = tmp_db.list_claim_observations(limit=5)
    assert rows[0]["selected"] == 1  # SQLite 0/1 surfacing
    assert rows[0]["selection_reason"] == "quorum"


def test_add_claim_observations_empty_noop(tmp_db: Database) -> None:
    tmp_db.add_claim_observations([])
    assert tmp_db.list_claim_observations(limit=5) == []


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------
def test_reset_for_new_competition_clears_state_but_keeps_teams(tmp_db: Database) -> None:
    tmp_db.upsert_team_names(["Team Alpha"])
    tmp_db.increment_team_offense("Team Alpha")
    tmp_db.add_points("Team Alpha", "A", 1, 1.0, 1)
    tmp_db.add_event(event_type="violation", severity="warning", detail="x")
    tmp_db.set_competition_state(status="running", current_series=5)

    tmp_db.reset_for_new_competition()

    team = tmp_db.get_team("Team Alpha")
    assert team is not None
    assert team["total_points"] == 0
    assert team["offense_count"] == 0
    assert team["status"] == "active"

    assert tmp_db.list_events(limit=5) == []
    assert tmp_db.list_point_events(team_names=["Team Alpha"]) == []


# ---------------------------------------------------------------------------
# Public dashboard config + notifications
# ---------------------------------------------------------------------------
def test_set_public_dashboard_config_partial_update(tmp_db: Database) -> None:
    updated = tmp_db.set_public_dashboard_config(headline="Welcome")
    assert updated["headline"] == "Welcome"
    assert updated["subheadline"] is None
    assert updated["updated_at"] is not None


def test_public_notifications_create_list_delete(tmp_db: Database) -> None:
    created = tmp_db.create_public_notification(message="Hello", severity="info")
    assert created["id"]

    listed = tmp_db.list_public_notifications(limit=5)
    assert [row["message"] for row in listed] == ["Hello"]

    assert tmp_db.delete_public_notification(created["id"]) is True
    assert tmp_db.delete_public_notification(created["id"]) is False
    assert tmp_db.list_public_notifications(limit=5) == []


# ---------------------------------------------------------------------------
# Schema guardrails
# ---------------------------------------------------------------------------
def test_ensure_column_is_idempotent(tmp_db: Database) -> None:
    """_ensure_column is called on every initialize(); re-calling it against
    an already-present column must be a no-op rather than an ALTER that
    would fail duplicate-column errors.
    """
    with tmp_db.tx() as conn:
        tmp_db._ensure_column(conn, table="competition", column="last_poll_at", definition="TEXT")
        tmp_db._ensure_column(conn, table="competition", column="last_poll_at", definition="TEXT")
    # If this didn't raise we're good.


# ===========================================================================
# Edge cases.
#
# The tests above cover the happy paths. The block below targets
# input-boundary and data-integrity behaviors that a future refactor or
# migration might silently change: parameter binding under injection-like
# inputs, unicode roundtrip, permissive vs. restrictive input handling at
# the DB layer (validation is the app layer's job; this layer trusts its
# caller), transaction rollback on exception, and the exact limit/filter
# contracts on the list_* queries.
# ===========================================================================

import sqlite3

import pytest as _pytest


# ---------------------------------------------------------------------------
# Parameter-binding safety and unicode
# ---------------------------------------------------------------------------
def test_team_name_with_sql_meta_characters_is_stored_literally(tmp_db: Database) -> None:
    # Classic injection probe: if the db layer builds queries by string
    # concatenation anywhere, this name would either break the statement
    # or mutate another table. SQLite parameter binding makes it a literal.
    nasty = "Robert'); DROP TABLE teams; --"
    tmp_db.upsert_team_names([nasty])

    team = tmp_db.get_team(nasty)
    assert team is not None
    assert team["name"] == nasty
    # Tables still exist and hold the injected literal:
    assert tmp_db.team_count() == 1


def test_team_name_with_double_quote_roundtrips(tmp_db: Database) -> None:
    tmp_db.upsert_team_names(['Team "Alpha"'])
    assert tmp_db.team_exists('Team "Alpha"')


def test_team_name_with_backslash_roundtrips(tmp_db: Database) -> None:
    tmp_db.upsert_team_names(["Team\\Alpha"])
    assert tmp_db.team_exists("Team\\Alpha")


def test_team_name_with_unicode_roundtrips(tmp_db: Database) -> None:
    for name in ("Team Α", "Team 中文", "Team 🚀", "Équipe Alpha"):
        tmp_db.upsert_team_names([name])
        assert tmp_db.team_exists(name), name
    assert tmp_db.team_count() == 4


def test_team_name_with_null_byte_is_permitted_at_db_layer(tmp_db: Database) -> None:
    # SQLite allows embedded NUL in TEXT fields. The caller (app.py) is
    # responsible for rejecting them via ``is_valid_team_claim``. This
    # test documents that the DB layer does not second-guess its input —
    # which means removing the app-layer filter would silently persist
    # NUL-containing names, not raise at the DB boundary.
    name = "Team\x00Alpha"
    tmp_db.upsert_team_names([name])
    assert tmp_db.team_exists(name)


def test_upsert_team_names_on_empty_list_is_noop(tmp_db: Database) -> None:
    tmp_db.upsert_team_names([])
    assert tmp_db.team_count() == 0


def test_upsert_team_names_deduplicates_within_single_call(tmp_db: Database) -> None:
    tmp_db.upsert_team_names(["Team Alpha", "Team Alpha", "Team Alpha"])
    assert tmp_db.team_count() == 1


# ---------------------------------------------------------------------------
# Duplicate create, empty names, long names
# ---------------------------------------------------------------------------
def test_create_team_on_duplicate_raises_integrity_error(tmp_db: Database) -> None:
    tmp_db.create_team("Team Alpha")
    with _pytest.raises(sqlite3.IntegrityError):
        tmp_db.create_team("Team Alpha")


def test_empty_string_team_name_is_permitted_at_db_layer(tmp_db: Database) -> None:
    # Same contract as the NUL-byte case: app layer is the gatekeeper.
    tmp_db.upsert_team_names([""])
    assert tmp_db.team_exists("")


def test_very_long_team_name_roundtrips(tmp_db: Database) -> None:
    long_name = "A" * 10_000
    tmp_db.upsert_team_names([long_name])
    assert tmp_db.team_exists(long_name)


# ---------------------------------------------------------------------------
# add_points boundaries
# ---------------------------------------------------------------------------
def test_add_points_with_negative_value_subtracts(tmp_db: Database) -> None:
    tmp_db.upsert_team_names(["Team Alpha"])
    tmp_db.add_points("Team Alpha", "A", 1, 2.0, 1)
    tmp_db.add_points("Team Alpha", "A", 1, -0.5, 2)

    assert tmp_db.get_team("Team Alpha")["total_points"] == 1.5


def test_add_points_with_zero_does_not_change_total(tmp_db: Database) -> None:
    tmp_db.upsert_team_names(["Team Alpha"])
    tmp_db.add_points("Team Alpha", "A", 1, 0.0, 1)
    assert tmp_db.get_team("Team Alpha")["total_points"] == 0.0


def test_add_points_accepts_fractional_values(tmp_db: Database) -> None:
    tmp_db.upsert_team_names(["Team Alpha"])
    tmp_db.add_points("Team Alpha", "A", 1, 0.1, 1)
    tmp_db.add_points("Team Alpha", "A", 1, 0.2, 2)
    # Float addition introduces rounding; assert to a tolerance rather
    # than exact equality.
    assert abs(tmp_db.get_team("Team Alpha")["total_points"] - 0.3) < 1e-9


# ---------------------------------------------------------------------------
# list_events / list_violations boundaries
# ---------------------------------------------------------------------------
def test_list_events_limit_zero_returns_empty(tmp_db: Database) -> None:
    tmp_db.add_event(event_type="violation", severity="warning", detail="x")
    tmp_db.add_event(event_type="ban", severity="critical", detail="y")
    assert tmp_db.list_events(limit=0) == []


def test_list_events_respects_event_type_filter_and_limit(tmp_db: Database) -> None:
    for _ in range(3):
        tmp_db.add_event(event_type="violation", severity="warning", detail="v")
        tmp_db.add_event(event_type="ban", severity="critical", detail="b")

    bans = tmp_db.list_events(limit=2, event_type="ban")
    assert [event["type"] for event in bans] == ["ban", "ban"]
    # Ordering is DESC by id, so the most recent events come first.
    assert bans[0]["id"] > bans[1]["id"]


def test_list_events_returns_empty_when_no_events_match_type(tmp_db: Database) -> None:
    tmp_db.add_event(event_type="violation", severity="warning", detail="x")
    assert tmp_db.list_events(limit=5, event_type="ghost") == []


def test_list_violations_with_limit_truncates(tmp_db: Database) -> None:
    for idx in range(3):
        tmp_db.record_violation(
            team_name="Team Alpha",
            machine="machineH2A",
            variant="A",
            series=2,
            offense_id=1,
            offense_name=f"offense_{idx}",
            evidence={},
            action_taken="warning",
        )

    rows = tmp_db.list_violations(limit=2)
    assert len(rows) == 2
    # Default ordering is ASC by id; limit takes the first two.
    assert [row["offense_name"] for row in rows] == ["offense_0", "offense_1"]


def test_list_violations_with_limit_none_returns_all(tmp_db: Database) -> None:
    for idx in range(3):
        tmp_db.record_violation(
            team_name="Team Alpha",
            machine="machineH2A",
            variant="A",
            series=2,
            offense_id=1,
            offense_name=f"o_{idx}",
            evidence={},
            action_taken="warning",
        )

    rows = tmp_db.list_violations(limit=None)
    assert len(rows) == 3


def test_list_point_events_without_team_filter_returns_all(tmp_db: Database) -> None:
    tmp_db.upsert_team_names(["Team Alpha", "Team Beta"])
    tmp_db.add_points("Team Alpha", "A", 1, 1.0, 1)
    tmp_db.add_points("Team Beta", "B", 1, 2.0, 1)

    events = tmp_db.list_point_events()
    assert len(events) == 2


# ---------------------------------------------------------------------------
# Complex evidence JSON roundtrip
# ---------------------------------------------------------------------------
def test_record_violation_roundtrips_nested_evidence(tmp_db: Database) -> None:
    evidence = {
        "stat": {"perm": "600", "owner": "root:root"},
        "lsattr": ["---i----", "/root/king.txt"],
        "offenders": [{"pid": 1, "cmd": "incrond"}, {"pid": 2, "cmd": "fswatch"}],
        "unicode": "Team 🚀",
    }
    tmp_db.record_violation(
        team_name="Team Alpha",
        machine="machineH2A",
        variant="A",
        series=2,
        offense_id=3,
        offense_name="king_immutable",
        evidence=evidence,
        action_taken="full_ban",
    )

    rows = tmp_db.list_violations()
    assert len(rows) == 1
    # The ``evidence`` column is stored as JSON text; the list_violations
    # query does not deserialize it, so the raw string must be valid
    # JSON of the original shape.
    import json

    restored = json.loads(rows[0]["evidence"])
    assert restored == evidence


def test_add_event_with_none_evidence_stores_null(tmp_db: Database) -> None:
    tmp_db.add_event(event_type="note", severity="info", detail="no evidence", evidence=None)

    events = tmp_db.list_events(limit=1)
    assert events[0]["evidence"] is None


def test_add_event_with_empty_dict_evidence_roundtrips_as_empty_dict(tmp_db: Database) -> None:
    tmp_db.add_event(event_type="note", severity="info", detail="empty", evidence={})

    events = tmp_db.list_events(limit=1)
    assert events[0]["evidence"] == {}


# ---------------------------------------------------------------------------
# set_competition_state edge cases
# ---------------------------------------------------------------------------
def test_set_competition_state_with_no_fields_is_noop(tmp_db: Database) -> None:
    before = tmp_db.get_competition()
    tmp_db.set_competition_state()
    after = tmp_db.get_competition()
    assert before == after


def test_set_competition_state_accepts_explicit_none_values(tmp_db: Database) -> None:
    # Passing ``fault_reason=None`` explicitly is the documented way to
    # clear a fault. The _UNSET sentinel distinguishes "clear" from
    # "leave alone".
    tmp_db.set_competition_state(status="faulted", fault_reason="broken")
    assert tmp_db.get_competition()["fault_reason"] == "broken"

    tmp_db.set_competition_state(fault_reason=None)
    assert tmp_db.get_competition()["fault_reason"] is None


# ---------------------------------------------------------------------------
# variant_ownership edge cases
# ---------------------------------------------------------------------------
def test_get_variant_owner_missing_returns_none(tmp_db: Database) -> None:
    assert tmp_db.get_variant_owner(series=1, variant="A") is None


def test_list_variant_owners_empty_series_returns_empty_list(tmp_db: Database) -> None:
    assert tmp_db.list_variant_owners(series=99) == []


def test_set_variant_owner_with_empty_evidence_dict(tmp_db: Database) -> None:
    tmp_db.set_variant_owner(
        series=1,
        variant="A",
        owner_team="Team Alpha",
        accepted_mtime_epoch=1000,
        source_node_host="192.168.0.102",
        evidence={},
    )
    owner = tmp_db.get_variant_owner(series=1, variant="A")
    assert owner is not None
    assert owner["evidence_json"] == {}


def test_set_variant_owner_with_none_evidence_stores_null(tmp_db: Database) -> None:
    tmp_db.set_variant_owner(
        series=1,
        variant="A",
        owner_team="Team Alpha",
        accepted_mtime_epoch=1000,
        source_node_host="192.168.0.102",
        evidence=None,
    )
    owner = tmp_db.get_variant_owner(series=1, variant="A")
    assert owner is not None
    assert owner["evidence_json"] is None


# ---------------------------------------------------------------------------
# claim_observations edge cases
# ---------------------------------------------------------------------------
def test_add_claim_observations_handles_missing_optional_keys(tmp_db: Database) -> None:
    # ``king``, ``king_mtime_epoch``, ``selection_reason``, and the
    # ``selected`` flag are all optional on the dict shape; the DB layer
    # substitutes None / 0 for missing entries.
    tmp_db.add_claim_observations(
        [
            {
                "poll_cycle": 1,
                "series": 2,
                "node_host": "192.168.0.102",
                "variant": "A",
                "status": "running",
                "observed_at": datetime.now(UTC).isoformat(),
            }
        ]
    )

    rows = tmp_db.list_claim_observations(limit=5)
    assert rows[0]["king"] is None
    assert rows[0]["king_mtime_epoch"] is None
    assert rows[0]["selected"] == 0
    assert rows[0]["selection_reason"] is None


def test_list_claim_observations_with_limit_zero_returns_empty(tmp_db: Database) -> None:
    tmp_db.add_claim_observations(
        [
            {
                "poll_cycle": 1,
                "series": 2,
                "node_host": "192.168.0.102",
                "variant": "A",
                "status": "running",
                "observed_at": datetime.now(UTC).isoformat(),
            }
        ]
    )
    assert tmp_db.list_claim_observations(limit=0) == []


def test_list_claim_observations_filters_by_series(tmp_db: Database) -> None:
    now = datetime.now(UTC).isoformat()
    for series in (1, 2, 3):
        tmp_db.add_claim_observations(
            [
                {
                    "poll_cycle": 1,
                    "series": series,
                    "node_host": "192.168.0.102",
                    "variant": "A",
                    "status": "running",
                    "observed_at": now,
                }
            ]
        )

    rows = tmp_db.list_claim_observations(limit=10, series=2)
    assert [row["series"] for row in rows] == [2]


# ---------------------------------------------------------------------------
# Transaction rollback
# ---------------------------------------------------------------------------
def test_tx_rollback_on_exception_discards_changes(tmp_db: Database) -> None:
    # Open a transaction, insert a team, then raise. The context manager
    # must rollback so the team is not visible to a subsequent read.
    tmp_db.upsert_team_names(["Team Alpha"])
    count_before = tmp_db.team_count()

    class _Boom(RuntimeError):
        pass

    with _pytest.raises(_Boom):
        with tmp_db.tx() as conn:
            conn.execute(
                "INSERT INTO teams (name, status, offense_count, total_points, created_at) "
                "VALUES (?, 'active', 0, 0, ?)",
                ("Team Beta", datetime.now(UTC).isoformat()),
            )
            raise _Boom("simulated failure during tx")

    # Rollback must have erased the Team Beta insert. The prior Alpha
    # row from upsert_team_names stays, because it ran in its own
    # completed transaction.
    assert tmp_db.team_count() == count_before
    assert not tmp_db.team_exists("Team Beta")
    assert tmp_db.team_exists("Team Alpha")


# ---------------------------------------------------------------------------
# reset_for_new_competition edge cases
# ---------------------------------------------------------------------------
def test_reset_for_new_competition_is_safe_on_empty_db(tmp_db: Database) -> None:
    # Before any teams or events exist, reset must not raise. This is
    # the default path the setup CLI takes on a fresh deployment.
    tmp_db.reset_for_new_competition()

    assert tmp_db.list_teams() == []
    assert tmp_db.list_events(limit=5) == []


def test_reset_for_new_competition_preserves_public_dashboard_config(tmp_db: Database) -> None:
    # public_dashboard_config is NOT wiped between competitions — it is
    # operator-owned metadata (headline, port ranges). This test pins
    # that contract so a well-meaning refactor that adds it to the
    # TRUNCATE list does not silently erase the organizer's setup.
    tmp_db.set_public_dashboard_config(headline="Welcome to KoTH")
    tmp_db.reset_for_new_competition()
    assert tmp_db.get_public_dashboard_config()["headline"] == "Welcome to KoTH"


# ---------------------------------------------------------------------------
# Public dashboard config partial updates
# ---------------------------------------------------------------------------
def test_set_public_dashboard_config_sentinel_leaves_unprovided_fields(tmp_db: Database) -> None:
    tmp_db.set_public_dashboard_config(headline="H1", subheadline="S1", port_ranges="10001-10003")
    tmp_db.set_public_dashboard_config(headline="H2")  # only headline changes
    result = tmp_db.get_public_dashboard_config()
    assert result["headline"] == "H2"
    assert result["subheadline"] == "S1"
    assert result["port_ranges"] == "10001-10003"


def test_set_public_dashboard_config_explicit_none_clears_field(tmp_db: Database) -> None:
    tmp_db.set_public_dashboard_config(headline="H1")
    tmp_db.set_public_dashboard_config(headline=None)
    assert tmp_db.get_public_dashboard_config()["headline"] is None
