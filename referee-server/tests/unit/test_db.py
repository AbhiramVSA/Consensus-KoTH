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
