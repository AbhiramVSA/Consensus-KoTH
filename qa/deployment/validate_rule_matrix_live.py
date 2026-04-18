#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import textwrap
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import paramiko
import requests


VARIANTS = ("A", "B", "C")
SERIES = tuple(range(1, 9))
TEAM_PREFIX = "VAL"
REMOTE_REPO = "/opt/KOTH_orchestrator/repo"
REMOTE_REFEREE = f"{REMOTE_REPO}/referee-server"


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
        cleanup = (
            "chmod 700 /root || true; "
            "if test -f /tmp/val_port.pid; then xargs -r kill </tmp/val_port.pid 2>/dev/null || true; fi; "
            "if test -f /tmp/val_incrond.pid; then xargs -r kill </tmp/val_incrond.pid 2>/dev/null || true; fi; "
            "rm -f /etc/cron.d/zz-val-king /tmp/val_incrond /tmp/val_incrond.pid /tmp/val_port.pid /tmp/val_php.log /tmp/val_shadow.bak /tmp/val_authkeys.bak; "
            "rm -f /root/king.txt; printf 'unclaimed\\n' > /root/king.txt; chmod 644 /root/king.txt; chown root:root /root/king.txt || true"
        )
        for idx in range(len(self.node_targets)):
            self.container_shell(series, variant, cleanup, idx)

    def safe_capture(self, series: int, variant: str, team: str) -> ValidationResult:
        self.ensure_series(series)
        self.ensure_team(team)
        baseline_points = float(self.team_record(team)["total_points"])
        baseline_event_id = self.latest_event_id()
        safe_cmd = (
            f"printf '%s\\n' {json.dumps(team)} > /root/king.txt; "
            "chmod 644 /root/king.txt; chown root:root /root/king.txt || true; chmod 700 /root"
        )
        self.container_shell(series, variant, safe_cmd, 0)
        self.container_shell(series, variant, safe_cmd, 1)
        self.container_shell(series, variant, "printf 'unclaimed\\n' > /root/king.txt; chmod 644 /root/king.txt; chmod 700 /root", 2)
        self.poll_once()
        events = self.events_after(baseline_event_id)
        team_after = self.team_record(team)
        violation_events = [e for e in events if e.get("team_name") == team and e["type"] in {"violation", "ban"}]
        point_events = [e for e in events if e.get("team_name") == team and e["type"] == "points_awarded" and e.get("variant") == variant]
        passed = float(team_after["total_points"]) >= baseline_points + 1.0 and not violation_events
        detail = "safe capture scored cleanly" if passed else "safe capture triggered unexpected state"
        self.set_unclaimed(series, variant)
        return ValidationResult(
            name=f"H{series}{variant} safe capture",
            kind="safe",
            series=series,
            variant=variant,
            passed=passed,
            detail=detail,
            evidence={
                "baseline_points": baseline_points,
                "after_points": float(team_after["total_points"]),
                "point_events": point_events,
                "violation_events": violation_events,
            },
        )

    def establish_owner(self, series: int, variant: str, team: str) -> None:
        self.ensure_series(series)
        self.ensure_team(team)
        safe_cmd = (
            f"printf '%s\\n' {json.dumps(team)} > /root/king.txt; "
            "chmod 644 /root/king.txt; chown root:root /root/king.txt || true; chmod 700 /root"
        )
        self.container_shell(series, variant, safe_cmd, 0)
        self.container_shell(series, variant, safe_cmd, 1)
        self.poll_once()

    def dangerous_root_dir(self, series: int, variant: str, team: str) -> ValidationResult:
        self.ensure_series(series)
        self.ensure_team(team)
        baseline_event_id = self.latest_event_id()
        command = (
            f"printf '%s\\n' {json.dumps(team)} > /root/king.txt; "
            "chmod 644 /root/king.txt; chown root:root /root/king.txt || true; chmod 755 /root"
        )
        self.container_shell(series, variant, command, 0)
        self.poll_once()
        events = self.events_after(baseline_event_id)
        violation_events = [
            e
            for e in events
            if e.get("team_name") == team and e["type"] == "violation" and "root_dir_perm_changed" in e["detail"]
        ]
        ban_events = [e for e in events if e.get("team_name") == team and e["type"] == "ban"]
        self.set_unclaimed(series, variant)
        return ValidationResult(
            name=f"H{series}{variant} dangerous root_dir",
            kind="dangerous",
            series=series,
            variant=variant,
            passed=bool(violation_events) and bool(ban_events),
            detail="root_dir_perm_changed detected" if violation_events else "expected root_dir_perm_changed missing",
            evidence={"violation_events": violation_events, "ban_events": ban_events},
        )

    def special_authkeys_safe(self) -> ValidationResult:
        series, variant, team = 1, "B", f"{TEAM_PREFIX}SAFE_H1B_AUTH"
        self.ensure_series(series)
        self.ensure_team(team)
        baseline_points = float(self.team_record(team)["total_points"])
        baseline_event_id = self.latest_event_id()
        cmd = (
            "mkdir -p /root/.ssh; "
            "test -f /root/.ssh/authorized_keys && cp -p /root/.ssh/authorized_keys /tmp/val_authkeys.bak || true; "
            "printf '%s\\n' 'ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQCvalidation validation' >> /root/.ssh/authorized_keys; "
            "chmod 600 /root/.ssh/authorized_keys; "
            f"printf '%s\\n' {json.dumps(team)} > /root/king.txt; chmod 644 /root/king.txt; chmod 700 /root"
        )
        restore = (
            "if test -f /tmp/val_authkeys.bak; then cp -p /tmp/val_authkeys.bak /root/.ssh/authorized_keys; else rm -f /root/.ssh/authorized_keys; fi; "
            "rm -f /tmp/val_authkeys.bak"
        )
        self.container_shell(series, variant, cmd, 0)
        self.container_shell(series, variant, cmd, 1)
        self.poll_once()
        events = self.events_after(baseline_event_id)
        violations = [e for e in events if e.get("team_name") == team and e["type"] in {"violation", "ban"}]
        after_points = float(self.team_record(team)["total_points"])
        for idx in (0, 1):
            self.container_shell(series, variant, restore, idx)
        self.set_unclaimed(series, variant)
        return ValidationResult(
            name="H1B authkeys safe path",
            kind="safe-edge",
            series=series,
            variant=variant,
            passed=not any("authkeys_changed" in e["detail"] for e in violations) and after_points >= baseline_points + 1.0,
            detail="H1B authkeys exemption held" if after_points >= baseline_points + 1.0 else "H1B authkeys safe path failed",
            evidence={"events": events, "baseline_points": baseline_points, "after_points": after_points},
        )

    def special_shadow_safe(self) -> ValidationResult:
        series, variant, team = 7, "B", f"{TEAM_PREFIX}SAFE_H7B_SHADOW"
        self.ensure_series(series)
        self.ensure_team(team)
        baseline_points = float(self.team_record(team)["total_points"])
        baseline_event_id = self.latest_event_id()
        cmd = (
            "cp -p /etc/shadow /tmp/val_shadow.bak; "
            "awk -F: 'BEGIN{OFS=\":\"} $1==\"root\"{$3=($3==\"0\"?\"1\":\"0\")}1' /tmp/val_shadow.bak > /etc/shadow; "
            f"printf '%s\\n' {json.dumps(team)} > /root/king.txt; chmod 644 /root/king.txt; chmod 700 /root"
        )
        restore = "cp -p /tmp/val_shadow.bak /etc/shadow; rm -f /tmp/val_shadow.bak"
        self.container_shell(series, variant, cmd, 0)
        self.container_shell(series, variant, cmd, 1)
        self.poll_once()
        events = self.events_after(baseline_event_id)
        violations = [e for e in events if e.get("team_name") == team and e["type"] in {"violation", "ban"}]
        after_points = float(self.team_record(team)["total_points"])
        for idx in (0, 1):
            self.container_shell(series, variant, restore, idx)
        self.set_unclaimed(series, variant)
        return ValidationResult(
            name="H7B shadow safe path",
            kind="safe-edge",
            series=series,
            variant=variant,
            passed=not any("shadow_changed" in e["detail"] for e in violations) and after_points >= baseline_points + 1.0,
            detail="H7B shadow exemption held" if after_points >= baseline_points + 1.0 else "H7B shadow safe path failed",
            evidence={"events": events, "baseline_points": baseline_points, "after_points": after_points},
        )

    def special_h1c_safe(self) -> ValidationResult:
        series, variant, team = 1, "C", f"{TEAM_PREFIX}SAFE_H1C_ONESHOT"
        self.ensure_series(series)
        self.ensure_team(team)
        baseline_points = float(self.team_record(team)["total_points"])
        baseline_event_id = self.latest_event_id()
        cmd = (
            f"sh -lc \"printf '%s\\\\n' {team} > /root/king.txt; chmod 644 /root/king.txt; chmod 700 /root; sleep 12\""
        )
        self.container_shell(series, variant, cmd, 0)
        self.container_shell(series, variant, cmd, 1)
        self.poll_once()
        events = self.events_after(baseline_event_id)
        violations = [e for e in events if e.get("team_name") == team and e["type"] in {"violation", "ban"}]
        after_points = float(self.team_record(team)["total_points"])
        self.set_unclaimed(series, variant)
        return ValidationResult(
            name="H1C one-shot write path",
            kind="safe-edge",
            series=series,
            variant=variant,
            passed=not any("watchdog_process" in e["detail"] for e in violations) and after_points >= baseline_points + 1.0,
            detail="H1C one-shot write stayed clean" if after_points >= baseline_points + 1.0 else "H1C one-shot write failed",
            evidence={"events": events, "baseline_points": baseline_points, "after_points": after_points},
        )

    def representative_dangerous_tests(self) -> list[ValidationResult]:
        tests: list[ValidationResult] = []
        service_probe_port = 55000 + int(time.time()) % 1000
        # H2A representative direct-rule probes
        tests.extend(
            self._run_representative_matrix(
                series=2,
                variant="A",
                probes=[
                    ("king_perm_changed", "chmod 600 /root/king.txt", "king_perm_changed"),
                    ("king_owner_changed", "chown 65534:65534 /root/king.txt || chown nobody:nogroup /root/king.txt", "king_owner_changed"),
                    ("king_immutable", "chattr +i /root/king.txt", "king_immutable", "chattr -i /root/king.txt || true"),
                    ("king_deleted", "rm -f /root/king.txt", "king_deleted"),
                    ("king_not_regular", "rm -f /root/king.txt && ln -s /etc/passwd /root/king.txt", "king_not_regular"),
                    ("cron_king_persistence", "printf '* * * * * root echo bad >> /root/king.txt\\n' > /etc/cron.d/zz-val-king", "cron_king_persistence"),
                    ("watchdog_process", "ln -sf /bin/sleep /tmp/incrond && /tmp/incrond 120 >/tmp/val_watch.log 2>&1 & echo \\$! >/tmp/val_incrond.pid", "watchdog_process"),
                ],
            )
        )
        # service_ports on H1C (php)
        tests.append(
            self._dangerous_probe(
                series=1,
                variant="C",
                probe_name="service_ports_changed",
                command=f"php -S 0.0.0.0:{service_probe_port} -t /tmp >/tmp/val_php.log 2>&1 & echo \\$! >/tmp/val_port.pid",
                expected="service_ports_changed",
            )
        )
        # iptables on H6A (privileged)
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
        # shadow/authkeys on non-exempt H2C
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
                command="mkdir -p /root/.ssh; test -f /root/.ssh/authorized_keys && cp -p /root/.ssh/authorized_keys /tmp/val_authkeys.bak || true; printf '%s\\n' 'ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQCvalidation validation' >> /root/.ssh/authorized_keys; chmod 600 /root/.ssh/authorized_keys",
                expected="authkeys_changed",
                restore="if test -f /tmp/val_authkeys.bak; then cp -p /tmp/val_authkeys.bak /root/.ssh/authorized_keys; else rm -f /root/.ssh/authorized_keys; fi; rm -f /tmp/val_authkeys.bak",
            )
        )
        return tests

    def _run_representative_matrix(
        self,
        series: int,
        variant: str,
        probes: list[tuple[str, str, str] | tuple[str, str, str, str]],
    ) -> list[ValidationResult]:
        return [
            self._dangerous_probe(
                series=series,
                variant=variant,
                probe_name=probe[0],
                command=probe[1],
                expected=probe[2],
                restore=probe[3] if len(probe) > 3 else None,
            )
            for probe in probes
        ]

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
        self.ensure_series(series)
        team = f"{TEAM_PREFIX}BAD_H{series}{variant}_{probe_name}".replace("-", "_")
        self.ensure_team(team)
        if expected in {"king_deleted", "king_not_regular"}:
            self.establish_owner(series, variant, team)
        baseline_event_id = self.latest_event_id()
        prep = f"printf '%s\\n' {json.dumps(team)} > /root/king.txt; chmod 644 /root/king.txt; chown root:root /root/king.txt || true; chmod 700 /root; {command}"
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
        matching = [e for e in events if e.get("team_name") == team and e["type"] == "violation" and expected in e["detail"]]
        bans = [e for e in events if e.get("team_name") == team and e["type"] == "ban"]
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
                # Ensure node deployment and DB state match again.
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
    parser = argparse.ArgumentParser(description="Validate safe and dangerous referee rule paths against the live cluster.")
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
