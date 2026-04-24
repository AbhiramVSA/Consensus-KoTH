#!/usr/bin/env python3
# ruff: noqa: E501
"""Validate safe and dangerous referee rule paths against a live cluster.

WHAT THIS DOES — READ BEFORE RUNNING.

This script is the most destructive piece of tooling in the repository. It
connects to the referee host over SSH, takes a ``.bak`` of the live SQLite
database, and then, for every series H1..H8 and every variant A/B/C:

* writes to ``/root/king.txt`` inside live challenge containers (all three
  challenge nodes, via ``docker exec -u 0``);
* modifies permissions, ownership, SSH authorized_keys, and ``/etc/shadow``
  inside those containers;
* appends rules to the live ``iptables`` chain;
* spawns listener processes on randomly chosen ports and kills them again
  by PID;
* creates teams with the ``VAL`` prefix in the live DB, drives them up to
  bans, and removes them afterwards;
* restarts the ``koth-referee`` systemd unit as sudo when ``.env`` needs
  the poll interval bumped for validation speed.

Pointed at a production event this will corrupt scores, create fake teams,
and leave backups on disk. The DB is restored from the ``.bak`` at the end
of ``LiveValidator.run``, but only if the script exits cleanly — a Ctrl-C
or an unhandled exception can leave mutations behind.

For that reason the script refuses to run unless the operator has
explicitly acknowledged the blast radius by setting
``KOTH_ALLOW_LIVE_MUTATION=yes-I-really-mean-it`` in the environment. Only
run it against a dedicated staging lab you are willing to rebuild.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

import paramiko
import requests


VARIANTS = ("A", "B", "C")
SERIES = tuple(range(1, 9))
TEAM_PREFIX = "VAL"
REMOTE_REPO = "/opt/KOTH_orchestrator/repo"
REMOTE_REFEREE = f"{REMOTE_REPO}/referee-server"

# The operator must acknowledge the blast radius before the script will run.
# Picked a phrase, not a boolean, so that the override cannot land from a
# CI env-map by accident.
LIVE_MUTATION_ENV_VAR = "KOTH_ALLOW_LIVE_MUTATION"
LIVE_MUTATION_TOKEN = "yes-I-really-mean-it"
LIVE_MUTATION_REFUSAL = textwrap.dedent(
    f"""\
    REFUSING TO RUN.

    validate_rule_matrix_live.py mutates live challenge containers, writes
    to /root/king.txt across three nodes, flushes and adds iptables rules,
    kills processes by PID, creates/bans teams in the live SQLite, and may
    restart the koth-referee systemd unit as sudo.

    To acknowledge the blast radius and proceed, export:

        {LIVE_MUTATION_ENV_VAR}={LIVE_MUTATION_TOKEN}

    Run only against a dedicated staging lab that you are willing to
    rebuild from scratch. This script restores the DB from a backup at
    the end of its run, but SIGINT or an unhandled exception can leave
    partial mutations on the live nodes.
    """
).strip()


def _assert_live_mutation_allowed() -> None:
    """Abort unless the explicit acknowledgement token is set in the environment.

    This is the single most important line of safety in the whole QA tree:
    without it, a mis-aimed run against a production event silently corrupts
    scores, creates fake teams, and leaves iptables rules behind.
    """
    if os.environ.get(LIVE_MUTATION_ENV_VAR) == LIVE_MUTATION_TOKEN:
        return
    print(LIVE_MUTATION_REFUSAL, file=sys.stderr)
    raise SystemExit(2)


@dataclass
class ValidationResult:
    name: str
    kind: str
    series: int
    variant: str
    passed: bool
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)


class LBRemote:
    def __init__(self, host: str, user: str, password: str):
        self.host = host
        self.user = user
        self.password = password
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(hostname=host, username=user, password=password, timeout=20)

    def close(self) -> None:
        self.client.close()

    def run(self, command: str, timeout: int = 120) -> tuple[int, str, str]:
        stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
        return stdout.channel.recv_exit_status(), stdout.read().decode(), stderr.read().decode()

    def sudo(self, command: str, timeout: int = 120) -> tuple[int, str, str]:
        wrapped = f"printf '%s\\n' {json.dumps(self.password)} | sudo -S bash -lc {json.dumps(command)}"
        return self.run(wrapped, timeout=timeout)

    def read_env(self) -> dict[str, str]:
        code, out, err = self.run(f"cd {REMOTE_REFEREE} && cat .env", timeout=30)
        if code != 0:
            raise RuntimeError(err or out or "failed to read live .env")
        values: dict[str, str] = {}
        for line in out.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key] = value
        return values

    def write_env_value(self, key: str, value: str) -> None:
        command = textwrap.dedent(
            f"""\
            cd {REMOTE_REFEREE}
            python3 - <<'PY'
            from pathlib import Path
            path = Path('.env')
            key = {key!r}
            value = {value!r}
            lines = path.read_text().splitlines()
            updated = False
            out = []
            for raw in lines:
                if raw.startswith(key + '='):
                    out.append(f"{{key}}={{value}}")
                    updated = True
                else:
                    out.append(raw)
            if not updated:
                out.append(f"{{key}}={{value}}")
            path.write_text('\\n'.join(out) + '\\n')
            PY
            """
        )
        code, out, err = self.run(command, timeout=30)
        if code != 0:
            raise RuntimeError(err or out or f"failed to update {key}")

    def backup_db(self) -> str:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup_path = f"/tmp/referee-validation-{stamp}.db"
        command = textwrap.dedent(
            f"""\
            cd {REMOTE_REFEREE}
            python3 - <<'PY'
            import sqlite3
            src = sqlite3.connect('referee.db')
            dst = sqlite3.connect({backup_path!r})
            src.backup(dst)
            dst.close()
            src.close()
            print({backup_path!r})
            PY
            """
        )
        code, out, err = self.run(command, timeout=60)
        if code != 0:
            raise RuntimeError(err or out or "failed to backup referee DB")
        return out.strip().splitlines()[-1].strip()

    def restore_db(self, backup_path: str) -> None:
        self.sudo(
            textwrap.dedent(
                f"""\
                systemctl stop koth-referee
                rm -f {REMOTE_REFEREE}/referee.db-wal {REMOTE_REFEREE}/referee.db-shm
                cp -f {backup_path} {REMOTE_REFEREE}/referee.db
                chown recon_admin:recon_admin {REMOTE_REFEREE}/referee.db
                systemctl start koth-referee
                """
            ),
            timeout=120,
        )

    def restart_referee(self) -> None:
        self.sudo("systemctl restart koth-referee", timeout=120)

    def node_exec(self, target: str, command: str, timeout: int = 120) -> tuple[int, str, str]:
        remote = (
            f"ssh -o BatchMode=yes -o StrictHostKeyChecking=no -i ~/.ssh/koth_referee {target} 'bash -s' <<'__KOTH_REMOTE__'\n"
            f"{command}\n"
            "__KOTH_REMOTE__\n"
        )
        return self.run(remote, timeout=timeout)


class LiveValidator:
    # Shell snippet that resets a variant to the default unclaimed state and
    # tears down anything the dangerous probes might have left behind. Kept
    # as a module-level constant so it is diffable as a unit rather than
    # encoded inside a large method.
    _RESET_UNCLAIMED_CMD = (
        "chmod 700 /root || true; "
        "if test -f /tmp/val_port.pid; then xargs -r kill </tmp/val_port.pid 2>/dev/null || true; fi; "
        "if test -f /tmp/val_incrond.pid; then xargs -r kill </tmp/val_incrond.pid 2>/dev/null || true; fi; "
        "rm -f /etc/cron.d/zz-val-king /tmp/val_incrond /tmp/val_incrond.pid /tmp/val_port.pid "
        "/tmp/val_php.log /tmp/val_shadow.bak /tmp/val_authkeys.bak; "
        "rm -f /root/king.txt; printf 'unclaimed\\n' > /root/king.txt; "
        "chmod 644 /root/king.txt; chown root:root /root/king.txt || true"
    )

    def __init__(self, lb: LBRemote, api_base: str):
        self.lb = lb
        self.env = lb.read_env()
        self.api_key = self.env["ADMIN_API_KEY"]
        self.session = requests.Session()
        self.session.headers.update({"X-API-Key": self.api_key})
        self.api_base = api_base.rstrip("/")
        self.node_targets = [item.strip() for item in self.env["NODE_SSH_TARGETS"].split(",") if item.strip()]
        self.results: list[ValidationResult] = []

    def get(self, path: str) -> Any:
        response = self.session.get(f"{self.api_base}{path}", timeout=60)
        response.raise_for_status()
        return response.json()

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        response = self.session.post(f"{self.api_base}{path}", json=body or {}, timeout=90)
        response.raise_for_status()
        if not response.text:
            return None
        return response.json()

    def latest_event_id(self) -> int:
        events = self.get("/api/events?limit=1")
        if not events:
            return 0
        return int(events[0]["id"])

    def events_after(self, event_id: int, limit: int = 200) -> list[dict[str, Any]]:
        events = self.get(f"/api/events?limit={limit}")
        return [event for event in events if int(event["id"]) > event_id]

    def team_record(self, name: str) -> dict[str, Any] | None:
        for team in self.get("/api/teams"):
            if team["name"] == name:
                return team
        return None

    def ensure_team(self, name: str) -> None:
        if self.team_record(name) is None:
            self.post("/api/admin/teams", {"name": name})

    def cleanup_validation_artifacts(self) -> None:
        patterns = ("VALSAFE_%", "VALBAD_%", "REVAL%", "codex_tester", "codex_validator")
        sql = textwrap.dedent(
            """\
            cd /opt/KOTH_orchestrator/repo/referee-server
            python3 - <<'PY'
            import sqlite3
            patterns = %s
            conn = sqlite3.connect('referee.db')
            cur = conn.cursor()
            where = ' OR '.join(['team_name LIKE ?' for _ in patterns])
            team_where = ' OR '.join(['name LIKE ?' for _ in patterns])
            owner_where = ' OR '.join(['owner_team LIKE ?' for _ in patterns])
            king_where = ' OR '.join(['king LIKE ?' for _ in patterns])
            for table in ('events', 'point_events', 'violations', 'active_violations'):
                cur.execute(f'DELETE FROM {table} WHERE {where}', patterns)
            cur.execute(f'DELETE FROM claim_observations WHERE {king_where}', patterns)
            cur.execute(f'DELETE FROM variant_ownership WHERE {owner_where}', patterns)
            cur.execute(f'DELETE FROM teams WHERE {team_where}', patterns)
            conn.commit()
            conn.close()
            PY
            """
            % (repr(patterns),)
        )
        code, out, err = self.lb.run(sql, timeout=120)
        if code != 0:
            raise RuntimeError(err or out or "failed to clean validation artifacts")

    def poll_once(self) -> None:
        self.post("/api/poll")
        time.sleep(1.2)

    def ensure_series(self, series: int) -> None:
        runtime = self.get("/api/runtime")
        if int(runtime["current_series"] or 0) != series:
            self.rotate_to_series(series)

    def rotate_to_series(self, series: int) -> None:
        self.post("/api/rotate/skip", {"target_series": series})
        deadline = time.time() + 240
        while time.time() < deadline:
            runtime = self.get("/api/runtime")
            validation = self.post("/api/recover/validate")
            if (
                int(runtime["current_series"]) == series
                and runtime["competition_status"] == "running"
                and validation["valid"]
            ):
                return
            time.sleep(3)
        raise RuntimeError(f"series H{series} failed to become valid in time")

    def container_shell(self, series: int, variant: str, inner_command: str, node_index: int) -> None:
        target = self.node_targets[node_index]
        command = textwrap.dedent(
            f"""\
            cd /opt/KOTH_orchestrator/h{series}
            cid="$(docker compose ps -q machineH{series}{variant} | head -n 1)"
            test -n "$cid"
            docker exec -u 0 "$cid" sh -lc {json.dumps(inner_command)}
            """
        )
        code, out, err = self.lb.node_exec(target, command, timeout=120)
        if code != 0:
            raise RuntimeError(f"{target} H{series}{variant} failed: {err or out}")

    def set_unclaimed(self, series: int, variant: str) -> None:
        """Reset a variant on every node to the pristine unclaimed state."""
        for idx in range(len(self.node_targets)):
            self.container_shell(series, variant, self._RESET_UNCLAIMED_CMD, idx)

    def establish_owner(self, series: int, variant: str, team: str) -> None:
        """Write the team name into /root/king.txt on nodes 0 and 1 so that
        subsequent probes can target an already-owned variant. Node 2 is
        intentionally left unclaimed so the baseline has one clean replica.
        """
        self.ensure_series(series)
        self.ensure_team(team)
        safe_cmd = (
            f"printf '%s\\n' {json.dumps(team)} > /root/king.txt; "
            "chmod 644 /root/king.txt; chown root:root /root/king.txt || true; chmod 700 /root"
        )
        self.container_shell(series, variant, safe_cmd, 0)
        self.container_shell(series, variant, safe_cmd, 1)
        self.poll_once()

    # ------------------------------------------------------------------
    # Unified probe helpers.
    #
    # The legacy version of this file had seven near-identical test harnesses
    # (safe_capture, dangerous_root_dir, special_authkeys_safe,
    #  special_shadow_safe, special_h1c_safe, _run_representative_matrix,
    #  _dangerous_probe). The only differences between them were:
    #
    #   * whether nodes 0/1/2 all get the team-owned king.txt or node 2 stays
    #     unclaimed (safe capture vs everything else),
    #   * whether the assertion is "violation present + ban fired" (dangerous)
    #     or "specific exemption suppressed + scoring succeeded" (safe-edge),
    #   * an optional restore command run inside the container afterwards.
    #
    # The helpers below capture those three axes as parameters, which collapses
    # roughly 200 lines of boilerplate into three wrapper methods. Behavior is
    # unchanged; the shape of the ValidationResult is identical to the old
    # version so the report JSON stays backward compatible.
    # ------------------------------------------------------------------
    def _safe_capture_probe(
        self,
        *,
        series: int,
        variant: str,
        team: str,
    ) -> ValidationResult:
        """Verify a clean team capture on two healthy nodes scores and does not
        trigger violations. Node 2 stays unclaimed on purpose so the scorer
        has to rely on the 2-node quorum rather than a unanimous observation.
        """
        self.ensure_series(series)
        self.ensure_team(team)
        baseline_points = float(self.team_record(team)["total_points"])
        baseline_event_id = self.latest_event_id()
        write_cmd = (
            f"printf '%s\\n' {json.dumps(team)} > /root/king.txt; "
            "chmod 644 /root/king.txt; chown root:root /root/king.txt || true; chmod 700 /root"
        )
        self.container_shell(series, variant, write_cmd, 0)
        self.container_shell(series, variant, write_cmd, 1)
        self.container_shell(
            series,
            variant,
            "printf 'unclaimed\\n' > /root/king.txt; chmod 644 /root/king.txt; chmod 700 /root",
            2,
        )
        self.poll_once()
        events = self.events_after(baseline_event_id)
        team_after = self.team_record(team)
        violation_events = [
            event
            for event in events
            if event.get("team_name") == team and event["type"] in {"violation", "ban"}
        ]
        point_events = [
            event
            for event in events
            if event.get("team_name") == team
            and event["type"] == "points_awarded"
            and event.get("variant") == variant
        ]
        passed = (
            float(team_after["total_points"]) >= baseline_points + 1.0 and not violation_events
        )
        self.set_unclaimed(series, variant)
        return ValidationResult(
            name=f"H{series}{variant} safe capture",
            kind="safe",
            series=series,
            variant=variant,
            passed=passed,
            detail="safe capture scored cleanly" if passed else "safe capture triggered unexpected state",
            evidence={
                "baseline_points": baseline_points,
                "after_points": float(team_after["total_points"]),
                "point_events": point_events,
                "violation_events": violation_events,
            },
        )

    def _dangerous_probe(
        self,
        *,
        series: int,
        variant: str,
        probe_name: str,
        command: str,
        expected: str,
        restore: str | None = None,
    ) -> ValidationResult:
        """Run a single dangerous probe on node 0 and expect a specific
        violation name plus a ban event to land in the referee's event log.
        """
        self.ensure_series(series)
        team = f"{TEAM_PREFIX}BAD_H{series}{variant}_{probe_name}".replace("-", "_")
        self.ensure_team(team)
        if expected in {"king_deleted", "king_not_regular"}:
            self.establish_owner(series, variant, team)
        baseline_event_id = self.latest_event_id()
        prep = (
            f"printf '%s\\n' {json.dumps(team)} > /root/king.txt; "
            "chmod 644 /root/king.txt; chown root:root /root/king.txt || true; chmod 700 /root; "
            f"{command}"
        )
        try:
            self.container_shell(series, variant, prep, 0)
        except Exception as exc:
            self.set_unclaimed(series, variant)
            return ValidationResult(
                name=f"H{series}{variant} {probe_name}",
                kind="dangerous",
                series=series,
                variant=variant,
                passed=False,
                detail=f"probe unsupported or failed to run: {exc}",
            )
        self.poll_once()
        events = self.events_after(baseline_event_id)
        matching = [
            event
            for event in events
            if event.get("team_name") == team and event["type"] == "violation" and expected in event["detail"]
        ]
        bans = [event for event in events if event.get("team_name") == team and event["type"] == "ban"]
        if restore:
            self.container_shell(series, variant, restore, 0)
        self.set_unclaimed(series, variant)
        return ValidationResult(
            name=f"H{series}{variant} {probe_name}",
            kind="dangerous",
            series=series,
            variant=variant,
            passed=bool(matching) and bool(bans),
            detail=f"{expected} detected" if matching else f"expected {expected} missing",
            evidence={"events": events},
        )

    def _safe_edge_probe(
        self,
        *,
        series: int,
        variant: str,
        team: str,
        label: str,
        command: str,
        exemption_name: str,
        restore: str | None = None,
    ) -> ValidationResult:
        """Verify that a referee exemption still fires: ``command`` should
        produce the condition that normally triggers ``exemption_name`` but
        the exemption must suppress the violation and the team must still
        score.

        This replaces the legacy ``special_authkeys_safe``,
        ``special_shadow_safe``, and ``special_h1c_safe`` trio, which shared
        the same shape but inlined everything.
        """
        self.ensure_series(series)
        self.ensure_team(team)
        baseline_points = float(self.team_record(team)["total_points"])
        baseline_event_id = self.latest_event_id()
        prep = (
            f"printf '%s\\n' {json.dumps(team)} > /root/king.txt; "
            "chmod 644 /root/king.txt; chmod 700 /root; "
            f"{command}"
        )
        self.container_shell(series, variant, prep, 0)
        self.container_shell(series, variant, prep, 1)
        self.poll_once()
        events = self.events_after(baseline_event_id)
        violations = [
            event
            for event in events
            if event.get("team_name") == team and event["type"] in {"violation", "ban"}
        ]
        after_points = float(self.team_record(team)["total_points"])
        if restore:
            for idx in (0, 1):
                self.container_shell(series, variant, restore, idx)
        self.set_unclaimed(series, variant)
        exemption_held = not any(exemption_name in event["detail"] for event in violations)
        scored = after_points >= baseline_points + 1.0
        passed = exemption_held and scored
        detail = (
            f"{label}: exemption held" if passed
            else f"{label}: expected no {exemption_name} and +1 score, "
                 f"got violations={[e['detail'] for e in violations]} points_delta={after_points - baseline_points}"
        )
        return ValidationResult(
            name=label,
            kind="safe-edge",
            series=series,
            variant=variant,
            passed=passed,
            detail=detail,
            evidence={
                "events": events,
                "baseline_points": baseline_points,
                "after_points": after_points,
            },
        )

    # ------------------------------------------------------------------
    # Curated test suites. Each method below assembles parameters for the
    # three probe helpers above; the helpers do the work.
    # ------------------------------------------------------------------
    def safe_capture(self, series: int, variant: str, team: str) -> ValidationResult:
        return self._safe_capture_probe(series=series, variant=variant, team=team)

    def dangerous_root_dir(self, series: int, variant: str, team: str) -> ValidationResult:
        _ = team  # team name is derived from probe_name to keep the report shape stable
        return self._dangerous_probe(
            series=series,
            variant=variant,
            probe_name="root_dir_perm_changed",
            command="chmod 755 /root",
            expected="root_dir_perm_changed",
        )

    def special_authkeys_safe(self) -> ValidationResult:
        return self._safe_edge_probe(
            series=1,
            variant="B",
            team=f"{TEAM_PREFIX}SAFE_H1B_AUTH",
            label="H1B authkeys safe path",
            command=(
                "mkdir -p /root/.ssh; "
                "test -f /root/.ssh/authorized_keys && cp -p /root/.ssh/authorized_keys /tmp/val_authkeys.bak || true; "
                "printf '%s\\n' 'ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQCvalidation validation' >> /root/.ssh/authorized_keys; "
                "chmod 600 /root/.ssh/authorized_keys"
            ),
            exemption_name="authkeys_changed",
            restore=(
                "if test -f /tmp/val_authkeys.bak; "
                "then cp -p /tmp/val_authkeys.bak /root/.ssh/authorized_keys; "
                "else rm -f /root/.ssh/authorized_keys; fi; "
                "rm -f /tmp/val_authkeys.bak"
            ),
        )

    def special_shadow_safe(self) -> ValidationResult:
        return self._safe_edge_probe(
            series=7,
            variant="B",
            team=f"{TEAM_PREFIX}SAFE_H7B_SHADOW",
            label="H7B shadow safe path",
            command=(
                "cp -p /etc/shadow /tmp/val_shadow.bak; "
                "awk -F: 'BEGIN{OFS=\":\"} $1==\"root\"{$3=($3==\"0\"?\"1\":\"0\")}1' "
                "/tmp/val_shadow.bak > /etc/shadow"
            ),
            exemption_name="shadow_changed",
            restore="cp -p /tmp/val_shadow.bak /etc/shadow; rm -f /tmp/val_shadow.bak",
        )

    def special_h1c_safe(self) -> ValidationResult:
        team = f"{TEAM_PREFIX}SAFE_H1C_ONESHOT"
        return self._safe_edge_probe(
            series=1,
            variant="C",
            team=team,
            label="H1C one-shot write path",
            command=(
                "sh -lc \"printf '%s\\\\n' "
                f"{team} > /root/king.txt; chmod 644 /root/king.txt; chmod 700 /root; sleep 12\""
            ),
            exemption_name="watchdog_process",
        )

    def representative_dangerous_tests(self) -> list[ValidationResult]:
        service_probe_port = 55000 + int(time.time()) % 1000
        tests: list[ValidationResult] = []
        # H2A: all the direct /root/king.txt rules.
        tests.extend(
            self._run_probe_matrix(
                series=2,
                variant="A",
                probes=[
                    ("king_perm_changed", "chmod 600 /root/king.txt", "king_perm_changed", None),
                    ("king_owner_changed", "chown 65534:65534 /root/king.txt || chown nobody:nogroup /root/king.txt", "king_owner_changed", None),
                    ("king_immutable", "chattr +i /root/king.txt", "king_immutable", "chattr -i /root/king.txt || true"),
                    ("king_deleted", "rm -f /root/king.txt", "king_deleted", None),
                    ("king_not_regular", "rm -f /root/king.txt && ln -s /etc/passwd /root/king.txt", "king_not_regular", None),
                    ("cron_king_persistence", "printf '* * * * * root echo bad >> /root/king.txt\\n' > /etc/cron.d/zz-val-king", "cron_king_persistence", None),
                    ("watchdog_process", "ln -sf /bin/sleep /tmp/incrond && /tmp/incrond 120 >/tmp/val_watch.log 2>&1 & echo \\$! >/tmp/val_incrond.pid", "watchdog_process", None),
                ],
            )
        )
        # service_ports on H1C.
        tests.append(
            self._dangerous_probe(
                series=1,
                variant="C",
                probe_name="service_ports_changed",
                command=f"php -S 0.0.0.0:{service_probe_port} -t /tmp >/tmp/val_php.log 2>&1 & echo \\$! >/tmp/val_port.pid",
                expected="service_ports_changed",
            )
        )
        # iptables on H6A (needs NET_ADMIN).
        tests.append(
            self._dangerous_probe(
                series=6,
                variant="A",
                probe_name="iptables_changed",
                command="iptables -A INPUT -p tcp --dport 54321 -j ACCEPT",
                expected="iptables_changed",
                restore="iptables -D INPUT -p tcp --dport 54321 -j ACCEPT || true",
            )
        )
        # shadow + authkeys on non-exempt H2C.
        tests.append(
            self._dangerous_probe(
                series=2,
                variant="C",
                probe_name="shadow_changed",
                command="cp -p /etc/shadow /tmp/val_shadow.bak && sed -E '0,/^root:[^:]*:[0-9]*/s//root:*:0/' /tmp/val_shadow.bak > /etc/shadow",
                expected="shadow_changed",
                restore="cp -p /tmp/val_shadow.bak /etc/shadow; rm -f /tmp/val_shadow.bak",
            )
        )
        tests.append(
            self._dangerous_probe(
                series=2,
                variant="C",
                probe_name="authkeys_changed",
                command=(
                    "mkdir -p /root/.ssh; "
                    "test -f /root/.ssh/authorized_keys && cp -p /root/.ssh/authorized_keys /tmp/val_authkeys.bak || true; "
                    "printf '%s\\n' 'ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQCvalidation validation' >> /root/.ssh/authorized_keys; "
                    "chmod 600 /root/.ssh/authorized_keys"
                ),
                expected="authkeys_changed",
                restore=(
                    "if test -f /tmp/val_authkeys.bak; "
                    "then cp -p /tmp/val_authkeys.bak /root/.ssh/authorized_keys; "
                    "else rm -f /root/.ssh/authorized_keys; fi; "
                    "rm -f /tmp/val_authkeys.bak"
                ),
            )
        )
        return tests

    def _run_probe_matrix(
        self,
        *,
        series: int,
        variant: str,
        probes: Iterable[tuple[str, str, str, str | None]],
    ) -> list[ValidationResult]:
        return [
            self._dangerous_probe(
                series=series,
                variant=variant,
                probe_name=name,
                command=command,
                expected=expected,
                restore=restore,
            )
            for name, command, expected, restore in probes
        ]

    def run(self) -> list[ValidationResult]:
        self.cleanup_validation_artifacts()
        original_runtime = self.get("/api/runtime")
        original_series = int(original_runtime["current_series"]) if original_runtime["current_series"] else 1
        original_status = original_runtime["competition_status"]
        backup_path = self.lb.backup_db()
        report_meta = {
            "started_at": datetime.now(UTC).isoformat(),
            "original_runtime": original_runtime,
            "backup_path": backup_path,
        }
        try:
            if self.env.get("POLL_INTERVAL_SECONDS") != "10":
                self.lb.write_env_value("POLL_INTERVAL_SECONDS", "10")
                self.lb.restart_referee()
                time.sleep(4)
            if original_status != "running":
                raise RuntimeError(f"live referee is {original_status}, expected running for validation")
            if original_series == 1:
                self.rotate_to_series(2)
                self.rotate_to_series(1)

            for series in SERIES:
                self.rotate_to_series(series)
                for variant in VARIANTS:
                    self.results.append(self.safe_capture(series, variant, f"{TEAM_PREFIX}SAFE_H{series}{variant}"))
                    self.results.append(self.dangerous_root_dir(series, variant, f"{TEAM_PREFIX}BAD_H{series}{variant}_ROOT"))
                if series == 1:
                    self.results.append(self.special_h1c_safe())
                    self.results.append(self.special_authkeys_safe())
                if series == 7:
                    self.results.append(self.special_shadow_safe())
            self.results.extend(self.representative_dangerous_tests())
            self.rotate_to_series(original_series)
        finally:
            self.lb.restore_db(backup_path)
            self.cleanup_validation_artifacts()
            time.sleep(5)
            if original_status == "running":
                self.get("/api/runtime")
        report_meta["completed_at"] = datetime.now(UTC).isoformat()
        report_meta["result_count"] = len(self.results)
        report_meta["passed"] = sum(1 for result in self.results if result.passed)
        report_meta["failed"] = sum(1 for result in self.results if not result.passed)
        report_path = Path("qa/deployment/live-rule-validation-report.json")
        report_path.write_text(
            json.dumps(
                {
                    "meta": report_meta,
                    "results": [asdict(result) for result in self.results],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(json.dumps(report_meta, indent=2))
        print(f"Report written to {report_path}")
        return self.results


def main() -> int:
    _assert_live_mutation_allowed()

    parser = argparse.ArgumentParser(
        description=(
            "Validate safe and dangerous referee rule paths against the live cluster. "
            f"Requires {LIVE_MUTATION_ENV_VAR}={LIVE_MUTATION_TOKEN} in the environment."
        ),
    )
    parser.add_argument("--lb-host", default="192.168.0.12")
    parser.add_argument("--lb-user", default="recon_admin")
    parser.add_argument("--lb-password", default="yoda32")
    parser.add_argument("--api-base", default="http://192.168.0.12:8000")
    args = parser.parse_args()

    lb = LBRemote(args.lb_host, args.lb_user, args.lb_password)
    try:
        validator = LiveValidator(lb, args.api_base)
        results = validator.run()
    finally:
        lb.close()

    failed = [result for result in results if not result.passed]
    if failed:
        print("FAILED CHECKS:")
        for result in failed:
            print(f"- {result.name}: {result.detail}")
        return 1
    print("All validation checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
