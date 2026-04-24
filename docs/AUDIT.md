# Consensus KoTH — High-Level Design Audit

> Scope: `referee-server/` control plane and its coupling to challenge content under `Series H1..H8/`.
> Audience: you (the architect), future contributors, reviewers.
> Posture: blunt. Event-shipped does not mean production-ready. Everything below is actionable.

---

## 0. TL;DR (the three things a principal engineer would flag on first read)

1. **The "rule engine" is not a rule engine.** `enforcer.py` is 62 lines of `if offense == 1 → warn / 2 → series_ban / else → full_ban`. Every violation, threshold, and exemption is hardcoded across `poller.py` (detection IDs 1–15), `scheduler.py` (`_VIOLATION_EXEMPTIONS` dict literal), and `enforcer.py` (escalation cascade). An operator cannot add a rule, change a threshold, or grant an exemption without a code change, test, and redeploy. For a platform whose entire value proposition is "the referee is authoritative," this is the weakest link.
2. **Three god-modules own the system.** `scheduler.py` (1,545 lines), `app.py` (1,425 lines), `db.py` (932 lines) — together 76% of the control plane in three files. Lifecycle, deploy, rotate, recover, scorer-glue, HAProxy parsing, baseline capture, violation merging, webhook firing, *and* time accounting all live in `RefereeRuntime`. This blocks parallel development, makes testing painful, and is the primary reason the rule engine couldn't evolve.
3. **Single-process, single-writer, single-host, single-point-of-failure.** One SQLite file with a global `RLock`, one `BackgroundScheduler`, blocking I/O on FastAPI handlers, SSH done through a cached Paramiko pool, and APScheduler running inside the web server process. It worked for one event on three nodes. It will not survive a second event with more teams, more series, or a referee crash at minute 47.

Everything else in this document is details under those three headings.

---

## 1. Architecture Map

### 1.1 Component diagram (as-is)

```
                         ┌────────────────────────────────────────────┐
                         │           Admin / Participant UI           │
                         │   (Jinja2 templates, vanilla JS, :8000/:9000)
                         └───────────────────────┬────────────────────┘
                                                 │ HTTP (sync)
                                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        app.py   (1,425 lines)                       │
│   FastAPI routes  ·  auth  ·  HAProxy parsing  ·  telemetry shell   │
│   dashboards  ·  public config  ·  leaderboard  ·  events feed      │
└───────────┬───────────────────────────────────┬─────────────────────┘
            │                                   │
            ▼                                   ▼
┌───────────────────────────┐        ┌──────────────────────────────┐
│ RefereeRuntime (scheduler)│───────▶│  Poller  (SSH + shell heredoc)│
│     scheduler.py (1545)   │        │      poller.py  (448)        │
│  lifecycle / deploy /     │        │  builds probe.sh inline,     │
│  rotate / recover / scorer│        │  parses VariantSnapshot,     │
│  glue / baselines /       │        │  emits ViolationHit[1..15]   │
│  violation merge / banner │        └──────────────┬───────────────┘
└───────┬──────────────┬────┘                       │
        │              │                            ▼
        ▼              ▼                  ┌──────────────────────┐
┌───────────────┐  ┌───────────────┐      │  SSHClientPool       │
│   Enforcer    │  │   Scorer      │      │  ssh_client.py (93)  │
│ enforcer.py   │  │ scorer.py(100)│      │  paramiko, cached    │
│   (62 lines)  │  │  quorum win   │      │  per-host clients    │
└──────┬────────┘  └───────┬───────┘      └──────────┬───────────┘
       │                   │                         │
       └─────────┬─────────┘                         │
                 ▼                                   ▼
       ┌────────────────────┐             ┌────────────────────┐
       │  Database (SQLite) │             │  Challenge nodes   │
       │    db.py (932)     │             │  (n1, n2, n3)      │
       │  1 conn, 1 RLock   │             │  docker compose    │
       └────────────────────┘             └────────────────────┘
```

### 1.2 Module responsibilities (as-is vs. as-should-be)

| Module | LOC | Actually does | Should do |
|---|---:|---|---|
| `app.py` | 1,425 | Routes, auth, HAProxy cfg parsing, shell heredocs for host metrics, template rendering, public config, admin moderation | Routing + request validation only. No shell. No HAProxy parsing. No HTML string building. |
| `scheduler.py` | 1,545 | Lifecycle FSM, deploy, rotate, recover, baseline capture, violation merging, scorer glue, webhook firing, HAProxy sync, port caching, listener parsing | FSM orchestration only. Delegate everything else. |
| `db.py` | 932 | Schema, migrations (ad-hoc `_ensure_column`), teams, points, violations, containers, baselines, events, public config, notifications | Split into `repositories/` (teams, scoring, events, config) with an explicit migration layer. |
| `poller.py` | 448 | SSH probe build, parse, violation detection with 15 hardcoded IDs | SSH probe + parse. Violation detection belongs in a rule engine. |
| `enforcer.py` | 62 | Increment counter, map to one of three strings | Evaluate rules, decide action, persist decision, emit event. |
| `scorer.py` | 100 | Quorum winner selection — actually fine | Keep, but make quorum policy pluggable. |
| `ssh_client.py` | 93 | Paramiko pool — acceptable | Add connection health, per-host timeouts, auto-reset on ETIMEDOUT. |
| `config.py` | 141 | `.env` loader + frozen dataclass — acceptable | Move defaults that are network-specific out of code. |
| `models.py` | 298 | Pydantic models for API — acceptable | Keep. |

### 1.3 State ownership & concurrency

- **Writers to SQLite:** `scheduler.poll_once`, `scheduler.rotate_next_series`, `scheduler.deploy_series_or_raise`, `app.py` admin routes, `enforcer.escalate_team`, `enforcer.record_violation`.
- **Readers:** every HTTP route, the scorer, the poller's baseline compare, webhook firing.
- **Serialization:** a single `threading.RLock` in `Database.tx()` plus an additional `RLock` in `RefereeRuntime` held for the entire `poll_once()`. On a 30s cadence with 3 nodes × 3 variants that's fine. Raise team count or series count and it isn't.
- **Concurrency model:** FastAPI on the default sync worker + APScheduler's `BackgroundScheduler` (separate thread) + a `ThreadPoolExecutor` for fan-out SSH. No `async def` anywhere on the hot path. HTTP requests block on DB locks held by the scheduler.

---

## 2. Rule Engine Assessment (the user's specific concern — verdict: gimmicky)

`enforcer.py` in full:

```python
def escalate_team(self, team_name: str) -> EnforcementResult:
    offense_count, status = self._db.increment_team_offense(team_name)
    if status == "warned":
        action = "warning"
    elif status == "series_banned":
        action = "series_ban"
    else:
        action = "full_ban"
    return EnforcementResult(...)
```

That is the entire decision surface. The thresholds `warned / series_banned / banned` live in `db.increment_team_offense` as hardcoded SQL. Violation *detection* is 15 enumerated `ViolationHit(id, name, evidence)` literals scattered across `poller._detect_violations` (`referee-server/poller.py:229-290`). Exemptions are:

```python
# referee-server/scheduler.py:39-42
_VIOLATION_EXEMPTIONS: dict[tuple[int, str], set[str]] = {
    (1, "B"): {"authkeys_changed"},
    (7, "B"): {"shadow_changed"},
}
```

A class-level dict literal. Any rule change means a git commit.

**What a real rule engine would look like:**

```yaml
# referee-server/rules/default.yaml
version: 1
violations:
  - id: king_perm_changed
    detector: poller.king.perm_not_equal
    params: { expected: "644" }
    severity: critical
  - id: cron_king_persistence
    detector: poller.cron.contains_king_write
    severity: critical
escalation:
  - on: [1st_offense]       → warn
  - on: [2nd_offense]       → series_ban { duration: current_series }
  - on: [3rd_offense]       → full_ban   { duration: event }
exemptions:
  - match: { series: 1, variant: B }
    waive: [authkeys_changed]
    reason: "H1B SSH seed writes to authorized_keys as part of intended path"
    owner: organizer
    expires: 2026-04-25T00:00:00Z
```

…loaded at startup, hot-reloadable via an admin endpoint, with every change written to the `events` audit log. Detectors are named functions registered in a dispatch dict. Escalation is a policy object. Exemptions are first-class, versioned, and expire.

**Gap list (rule engine → production):**

1. No declarative rule format. Rules are Python.
2. No separation of detection from escalation. `poller.py` decides what a violation is; `enforcer.py` only counts them.
3. No severity tiering. All violations increment the same counter.
4. No per-team, per-series, or per-variant escalation policy.
5. No exemption expiry, no exemption audit log.
6. No "shadow mode" — can't deploy a new rule in warn-only before enforcing.
7. No unit tests of the enforcer (zero coverage).
8. Unban is manual SQL or a dedicated admin route; no time-boxed bans.
9. Thresholds are baked into DB trigger-like logic (`increment_team_offense`) rather than the policy layer.
10. The scheduler's `team_actions` de-dup (`scheduler.py:~1503`) silently collapses multiple offenses in one cycle to one escalation — arguably wrong, definitely undocumented.

---

## 3. Hardcoding Hotspots

Authoritative list. Each line is a `file:line` reference.

| Category | Location | Value | Fix |
|---|---|---|---|
| Node IPs in defaults | `config.py:59,62` | `192.168.0.70,192.168.0.103,192.168.0.106` | Remove default. Fail loud if `NODE_HOSTS` unset. Local dev uses `.env.example`. |
| LB host | `app.py` telemetry section | `"192.168.0.12"` literal | Move to `HAPROXY_HOST` env var. |
| HAProxy paths | `config.py:103-106` | `/etc/haproxy/haproxy.cfg`, `/run/haproxy/admin.sock`, `/var/log/haproxy.log` | OK as override-able defaults; but log and document that these assume Debian/Ubuntu + systemd. |
| Container name template | `config.py:88-90` | `machineH{series}{variant}` | Template is fine; validate that the rendered string is a valid Compose service name at `Settings.validate_runtime()`. |
| Variants | `config.py:74` | `A,B,C` | Tolerate `N` variants throughout; currently 3 is baked into shell probe heredoc. |
| Total series | `config.py:75` | `8` | Loop bounds in several places assume ≤8; audit with a test that sets `TOTAL_SERIES=16`. |
| Min healthy nodes | `config.py:77` | `2` | Expose as per-variant override; not global. |
| Clock drift threshold | `config.py:76` | `2` seconds | Way too tight for WAN. Make 5s default; allow per-host override. |
| SSH defaults | `config.py:66-68` | `root`, `22`, `~/.ssh/id_rsa` | Acceptable defaults; document in `.env.example`. |
| Docker compose cmd | `config.py:87` | `docker compose` | OK. Validate that it's executable at startup. |
| Deploy health timings | `config.py:82-83` | 45s timeout / 3s poll | Tune per-series; slow series (H4B Spring, H5A Webmin) routinely exceed 45s on cold build. |
| Ownership file path | `poller.py:67` | `/root/king.txt` | Move to rule config; one day a challenge will use a different path. |
| Probe toolchain | `poller.py:~75` | `stat`, `lsattr`, `iptables -L`, `sha256sum` | Assumes GNU coreutils + iptables. No BusyBox, no Alpine probes, no nftables. Move to a pluggable probe module. |
| "unclaimed" sentinel | `poller.py:~212`, scheduler, scorer | literal string | Use `UNCLAIMED = "unclaimed"` module constant, ideally an `enum`. |
| Violation exemptions | `scheduler.py:39-42` | Dict literal | Move to rules config (see §2). |
| Points per cycle | `config.py:81` | `1.0` flat | Support per-variant weighting (difficulty) and time-decay. |
| Series → ports mapping | `app.py` ~lines 901-917 | Hardcoded port range table | Derive from series compose files, or keep in config YAML. |
| HAProxy config parsing | `app.py:159-237` | Regex heredocs | Use `haproxyadmin` library or parse once into a cached struct; currently re-parsed on every `/api/routing`. |
| Host metrics shell | `app.py:625-677` | 650-line shell heredoc baked into Python | Ship as a file `referee-server/probes/host_metrics.sh`, version it, upload to nodes on deploy. |
| Container ID format | `scheduler.py:~743` | `H{series}{variant}_Node{idx}` | OK but belongs in `config.py` with other templates. |

---

## 4. Inefficient Architecture

| # | Finding | Evidence | Impact | Suggested fix |
|---|---|---|---|---|
| 1 | HTTP handlers block on SSH + DB | `app.py` sync routes call into `RefereeRuntime` which holds `_lock` | Any HTTP call during a poll can wait 2–30s | Convert routes to `async def`, move scheduler work off the web process, or at minimum spawn blocking work via `asyncio.to_thread` |
| 2 | Scheduler lives in the web process | `RefereeRuntime.start_scheduler()` called from FastAPI startup | A crash in a handler takes scoring down | Extract scheduler to its own process; communicate via DB + pubsub |
| 3 | APScheduler `BackgroundScheduler` on a single host | `scheduler.py:49` | No HA; no failover | Use APScheduler `AsyncIOScheduler` behind a distributed lock (Postgres advisory, Redis, or etcd) so two referees can run hot-warm |
| 4 | SQLite single-writer | `db.py:14-229` (one connection, `RLock` on every write) | Lock contention under load; no HA | Migrate to Postgres; keep SQLAlchemy-free raw access if you like, but behind a repository interface |
| 5 | N+1 SSH per poll | `poller.run_cycle` + `scheduler._compose_ps` + `scheduler._docker_stats` + `scheduler._docker_inspect` | 6–12 SSH execs / node / cycle | Fold into a single probe script per host; parse one structured blob |
| 6 | Baseline recompute every poll | `scheduler._merge_baseline_violations` recomputes hashes every 30s | Wasted CPU on nodes; wasted bandwidth | Cache baseline hash per (series, variant, node) in DB; invalidate on redeploy |
| 7 | Matrix completeness gate | `scheduler.py:~854` — any missing `(host, variant)` pair skips scoring | One flaky node = zero points for everyone | Score per variant with its own quorum; degrade not fail |
| 8 | No HAProxy config caching | `app.py:159-237` re-parses on every call | Every dashboard refresh re-reads + regex | Parse once, invalidate on `set-active-series` |
| 9 | Rule exemption de-dup drops real events | `scheduler.py:~1503` collapses multiple unique offenses in one poll to one action | Under-counting offenses under burst violations | Record all offenses; apply escalation policy on the aggregated set |
| 10 | DB migrations via `_ensure_column` | `db.py` sprinkled `ALTER TABLE ... ADD COLUMN` on init | No versioning, no rollback, no dry-run | Adopt Alembic (even with raw SQL ops) or a hand-rolled numbered migrations table |
| 11 | Violation detection blob as Python string | `poller._build_probe_command` is a Python f-string of shell | Can't lint the shell, can't diff it, can't shellcheck it | Ship as a `.sh` file with checksum; transfer on deploy |
| 12 | Shell heredocs in `app.py` for host metrics | `app.py:625-677` | Tight coupling of web layer to shell syntax | Move to `probes/` directory, loaded at startup |
| 13 | No bulk insert for point events | `scheduler` calls `db.add_point_event` per team per cycle | Fine at 10 teams, breaks at 200 | `db.add_point_events(iterable)` batched in one transaction |
| 14 | No connection pool for Paramiko | `SSHClientPool` holds one client per host, re-created on error | Under bursty loads, serializes through one TCP connection | Pool of N clients per host with explicit checkout/checkin |
| 15 | Webhook fired on hot path | `fire_and_forget` called inside poll | Slow webhook receiver slows poll | Queue to a bounded in-memory deque; drain on a worker thread |

---

## 5. Documentation Gaps

### 5.1 What exists

- `README.md` (excellent prose; no diagrams).
- `docs/full-deployment-runbook.md`, `docs/deployment-validation-checklist.md`, `docs/haproxy-full-config.md`, `docs/manual-tester-checklist.md`, `docs/participant-hard-bound-rules.md`, `docs/referee-per-node-ssh-targets.md`, `docs/referee-rule-validation-checklist.md`, `docs/production-remediation-design.md`.
- `qa/README.md` and `qa/deployment/README.md`.

### 5.2 What is missing

- **ARCHITECTURE.md** — single source of truth for the component diagram, data flow, concurrency model, lifecycle FSM, and HA posture. The README's ASCII topology is too shallow.
- **VIOLATIONS.md** — a table mapping offense IDs 1–15 to meaning, detection source, severity, escalation rule, and exemption status. The operator currently has to read `poller.py:229-290` to understand what "offense 7" means.
- **LIFECYCLE.md** — formal state diagram for `stopped / starting / running / paused / rotating / faulted / stopping`, including every legal transition, the trigger, and the rollback path. Currently inferred from reading `scheduler.py`.
- **API.md** (or an auto-generated OpenAPI page) — the list in README.md is outdated vs. the actual route decorators in `app.py`. FastAPI gives you this for free; turn it on.
- **CONTRIBUTING.md** — branch model, commit style, DCO or CLA, how to run the tests, how to add a new series. README has a short paragraph; it's not sufficient.
- **Inline docstrings** — `scheduler.py`, `db.py`, `enforcer.py` have essentially none. At minimum every public method on `RefereeRuntime`, every repository method on `Database`, and every dataclass field in `poller.py` deserves a one-line docstring.
- **Type hints** — partial. Public method signatures on `RefereeRuntime` lack full annotations; `dict[str, Any]` is rampant in APIs that have knowable shape.

---

## 6. Correctness & Robustness Risks

| Risk | Where | Why it matters |
|---|---|---|
| Partial SSH failure on deploy | `scheduler._run_compose_parallel` records `(False, err)` but continues | Half-deployed series is worse than failed deploy. Need atomic deploy with rollback on any node failure. |
| Faulted state has no auto-recovery | `scheduler.py` sets `status="faulted"` and waits for operator | If the operator misses the webhook, the event stalls. Add bounded auto-retry with backoff, then page. |
| `ALLOW_UNSAFE_NO_ADMIN_API_KEY` bypasses auth entirely | `config.py:117-119`, `app.py:114-118` | Footgun. At minimum, log a loud banner on every request when this is enabled, and refuse to run if `APP_HOST=0.0.0.0`. |
| No CSRF on state-changing forms | admin templates | Today "it's fine" because admin is key-gated, but as soon as you add cookie-based login this bites. |
| SQLite rollback-journal default | `db.py` | Under concurrent read+write, readers block writers. Enable `PRAGMA journal_mode=WAL` and `synchronous=NORMAL`. |
| Clock drift 2s threshold | `config.py:76` | Too tight for WAN. Real clock drift on a VLAN with one slow NIC is 0.5–3s. Raise to 5s default. |
| Scorer tie-break by node priority | `scorer.py` | Correct, but undocumented. Write down the invariant: "on equal-mtime claims, priority list in `NODE_PRIORITY` decides." |
| Irreversible full_ban | `db.increment_team_offense` | Once banned, only admin SQL unbans. No time-boxed ban, no appeal, no expiry. |
| Baseline-capture race after rotate | `scheduler._capture_baselines` | If a poll fires between `compose up` and baseline capture, the first cycle's violations are false positives. Serialize capture before resuming the scorer. |
| Webhook includes raw team names / evidence | `webhook.py`, `enforcer.record_violation` | If `WEBHOOK_URL` is third-party, PII-ish strings leak. Allow evidence redaction. |
| No rate limiting on public `/api/public/*` | `app.py` | A noisy participant script could DDoS the referee with leaderboard polls. |

---

## 7. Testing Posture

- **Files:** `referee-server/tests/test_scoring_and_drift.py`, `referee-server/tests/test_ssh_targets.py`, plus fixtures.
- **Covered:** scorer quorum + tie-break, clock drift marking, SSH target override.
- **Not covered at all:** enforcer, db layer, scheduler lifecycle, rotate, recover, baseline capture, deploy, HAProxy parsing, admin routes, public routes, auth, webhook, config validation.
- **No integration tests** against a real SQLite file + real (local) SSH + real compose.
- **No chaos tests** (node-down, ssh-timeout, compose-hang, poll-during-rotate).
- **No contract tests** on the public API.
- **No load tests** beyond `qa/load_suite.py` which targets challenges, not the referee.

Target: 60% line coverage on `referee-server/` within two milestones, with scheduler lifecycle and enforcer at ≥80%.

---

## 8. Prioritized Improvement Roadmap

Ordered by (value × urgency) ÷ effort. S = 1–2 days. M = 1 week. L = 2–4 weeks.

| # | Work item | Why | Effort | Risk |
|---|---|---|---|---|
| 1 | Extract rule engine: YAML-declared violations, severities, escalation policy, exemptions with expiry. Hot-reload via admin endpoint. | User's #1 concern. Unlocks everything else in enforcement. | M | M — needs careful migration of the 15 existing violations |
| 2 | Split `scheduler.py` into `lifecycle.py`, `deployer.py`, `recovery.py`, `baselines.py`, `enforcement_pipeline.py`. Behavior-preserving refactor with tests added *before* each split. | God class blocks every other improvement. | M | L if done without tests first, S if done after #9 |
| 3 | Split `app.py` into `routes/admin.py`, `routes/public.py`, `routes/telemetry.py`, `routes/ops.py`; move HAProxy parsing to `haproxy_client.py`; move shell heredoc to `probes/host_metrics.sh`. | Unreadable today. | S | S |
| 4 | Add **integration test harness**: pytest fixture that spins a real SQLite, a fake SSH pool with scripted responses, and a real `RefereeRuntime`. Test full rotate + recover + deploy. | Blocks all refactoring confidence. | M | S |
| 5 | Migrate SQLite → Postgres behind a repository layer (`db/repositories/*.py`). Enable WAL on SQLite in the interim. | Removes lock contention + opens the door to HA. | L | M |
| 6 | Make scheduler a separate process (`python -m referee_server.scheduler`), communicating with the web process through the DB + a Redis pubsub. | Removes web ↔ scoring coupling. | M | M |
| 7 | Per-variant quorum scoring with graceful degradation. One sick node must not zero out the scoreboard. | Event-reliability bug. | S | S |
| 8 | Bounded auto-recovery from `faulted` with exponential backoff + webhook page after N failures. | Operator burden at event time. | S | S |
| 9 | Ship probe logic as a versioned shell file pushed to nodes on deploy. Same for host-metrics shell. Drop heredocs from Python. | Testability, shellcheck-ability, diff-ability. | S | S |
| 10 | Write **ARCHITECTURE.md, VIOLATIONS.md, LIFECYCLE.md**, enable FastAPI's `/docs`, fill type hints, add docstrings to every public method. | Documentation debt. | S | none |
| 11 | Upgrade auth: short-lived bearer tokens with scopes (`admin:read`, `admin:write`, `ops:deploy`) instead of one god key. Kill `ALLOW_UNSAFE_NO_ADMIN_API_KEY` in production. | Production-grade auth. | M | S |
| 12 | Structured JSON logging (`runtime_logging.py`) → stdout, one event per line, with request ID / series / variant / team correlation. Feed to Loki/Grafana in the runbook. | Observability gap at event time. | S | none |
| 13 | Per-endpoint rate limiting on public APIs via `slowapi` or Cloudflare. | Protect the referee from participant scripts. | S | none |
| 14 | Time-boxed bans, appeal workflow, audit log of admin overrides. | Rule engine's companion at the team-lifecycle layer. | M | M |
| 15 | Multi-event / multi-range mode: first-class concept of "event" in DB with configurable `TOTAL_SERIES`, `variants`, `rules` per event. | Future-proofs the platform for recurring use. | L | M |

---

## 9. Principal Engineer's Verdict

> **"It worked, and the quorum model is genuinely good. Ship the v1, then stop treating this like a hack."**

The quorum ownership model, the explicit lifecycle FSM states, and the deliberate recovery stance are better than most CTF infrastructures on the market. Those are the load-bearing ideas and they're worth defending.

Below those ideas, the implementation is three god-modules, a rule engine that is a nested `if`, a single-writer SQLite, and a deploy path that can half-succeed. Any one of those is fixable in a week. The user's intuition — "hardcoded, inefficient, gimmicky rule engine, under-documented" — is correct on all four counts and under-stated on the second and third.

The right next move is not to rewrite. It is:

1. Land tests around current behavior (#4).
2. Split the god modules (#2, #3).
3. Replace the enforcer with a real rule engine (#1).
4. Only then consider Postgres + HA (#5, #6).

Do not skip step 1.
