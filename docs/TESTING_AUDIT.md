# Consensus KoTH — Testing & QA Audit

> Scope: every test and probe in the repo — `referee-server/tests/`, `qa/`, `qa/deployment/`.
> Lens: determinism, complexity, logic soundness, coverage gaps.
> Verdict first, details after. Tested against the live suite (pytest: **64 passed in 21.67s** on Python 3.12.4, pytest 8.4.2).

---

## 0. TL;DR

1. **The unit-test surface is 64 tests in 2 files totaling 1,805 LOC.** `test_scoring_and_drift.py` alone is 1,704 LOC — a test god-file mirroring `scheduler.py`. No `conftest.py`, no `pytest.ini`/`pyproject.toml`, no coverage config, no shared fixtures.
2. **The tests pass — but they fake nearly everything they depend on.** `DummySSH`, `DummyScheduler`, `DummyTemplates`, fake `paramiko` module injected at import, frozen-dataclass `Settings` mutated via `object.__setattr__` 40+ times, `scheduler.time.monotonic` patched with hardcoded 4-element sequences. Green does not mean the scheduler works on real infrastructure — it means the scheduler works against a specific synthetic trace.
3. **The QA suite (outside `tests/`) is duplicated at industrial scale.** `qa/vuln_suite.py` has 24 copy-pasted check functions (`check_h1a` ... `check_h8c`) that differ only in endpoint/payload/marker — ~350 lines of boilerplate that would collapse into a 150-line config-driven dispatcher. `qa/deployment/validate_rule_matrix_live.py` has seven near-identical test harnesses.
4. **`validate_rule_matrix_live.py` can corrupt a live competition by accident.** It runs `docker exec -u 0`, rewrites `/root/king.txt`, flushes `iptables`, kills processes by PID, and stops/starts the referee `systemd` unit. There is no guard that refuses to run when pointed at a production deployment. This is the single highest-risk file in the repo.
5. **Determinism is leaky.** Unseeded `random`, millisecond-timestamp boundaries, `time.sleep` as a synchronization primitive, `side_effect=[0, 0, 2, 2]` for `time.monotonic` (5th call → `StopIteration`), frozen `SETTINGS` mutation that races with any test that imports `config`, and real disk writes to the source-tree `.env` by `test_dotenv_is_loaded_from_module_directory`.
6. **Coverage gaps matter.** Zero tests for `enforcer.py` beyond its side effects, zero for `db.py` beyond via integration, zero for `webhook.py`, zero for `runtime_logging.py`, zero for the `setup_cli.py` bootstrap, zero for the `qa/` suite itself, no chaos tests, no multi-team concurrency, no replica divergence harness, no rotation-under-load.

Tests pass. The test *architecture* does not.

---

## 1. Test Surface Inventory

| Path | LOC | Classes | Tests | Notes |
|---|---:|---:|---:|---|
| `referee-server/tests/test_scoring_and_drift.py` | 1,704 | 5 | 61 | Scoring + drift + lifecycle + API + config + poller. All in one file. |
| `referee-server/tests/test_ssh_targets.py` | 101 | 2 | 3 | SSH target parsing + SETTINGS validation. |
| `qa/common.py` | 205 | 0 | 0 | Shared HTTP/TCP/UDP helpers for probes. |
| `qa/targets.py` | 53 | 0 | 0 | Target roster (24 machines). |
| `qa/load_suite.py` | 150 | 0 | 0 | Concurrent load prober (runs; not a pytest suite). |
| `qa/vuln_suite.py` | 528 | 0 | 0 | 24 exploit probes. Not a pytest suite. |
| `qa/koth_load_sim.py` | 305 | 0 | 0 | Async traffic simulator. |
| `qa/deployment/emulate_referee_paths.py` | 104 | 0 | 0 | Referee bootstrap with **fake SSH** — the only safe one. |
| `qa/deployment/validate_rule_matrix_live.py` | 646 | 0 | 0 | **Live-mutation validator. Extreme-risk.** |
| `qa/deployment/validate_koth_node.sh` | 137 | — | — | Node health check (read-only). |
| `qa/deployment/validate_referee_lb.sh` | 247 | — | — | Referee + LB check (mostly read-only). |
| `qa/deployment/configure_koth_ufw.sh` | 170 | — | — | UFW rule generator. |
| `qa/deployment/prebuild_series_cache.sh` | 175 | — | — | Remote `docker compose build` warmup. |
| **Total** | **4,525** | — | **64** | — |

**No config files found:** no `pytest.ini`, no `pyproject.toml`, no `setup.cfg`, no `tox.ini`, no `conftest.py`. Pytest runs via implicit discovery + the `sys.path.insert` hack at the top of each test file.

---

## 2. `test_scoring_and_drift.py` — Scrutiny

### 2.1 Class breakdown

| Class | Line | Tests | What it actually tests |
|---|---:|---:|---|
| `ScoringAndDriftTests` | 122 | 5 | `resolve_earliest_winners` + `_mark_clock_drift_degraded`. **Genuinely unit tests.** |
| `RuntimeSafetyTests` | 188 | 19 | Full lifecycle: start/rotate/recover/pause + baselines + reconciliation. **Effectively integration tests** — real SQLite tempfile, `RefereeRuntime` instance. |
| `PollerCompletenessTests` | 962 | 5 | Poller shell-output parsing. |
| `ConfigLoadingTests` | 1,058 | 1 | `.env` loading via `importlib.reload(config)`. **Mutates source tree.** |
| `ApiEndpointTests` | 1,078 | 31 | FastAPI routes via `TestClient`. Swaps `app_module.db/runtime/ssh_pool` globals. |

Mixed granularities (pure-unit, integration, and API) jammed into one file under one naming convention. Impossible to run "just the scorer tests" without knowing the class name — and `pytest -k scoring` picks up class-level matches across unrelated concerns.

### 2.2 Determinism violations (cite-by-line)

| # | Issue | Location | Blast radius |
|---|---|---|---|
| D1 | Fake `paramiko` injected at import-time only if absent. If installed, it's used; if not, a `SimpleNamespace` stub is used. The test behaves differently on different developer laptops. | `test_scoring_and_drift.py:17-23` | Silent behavior drift |
| D2 | `object.__setattr__(SETTINGS, name, value)` on a `frozen=True` dataclass. Used ~40 times across the suite. `SETTINGS` is module-level and shared — any parallel test run or any test that reads `SETTINGS` mid-mutation observes partial state. | `test_scoring_and_drift.py:82,90,94` plus every use of `_override_runtime_settings` | Breaks `pytest-xdist`; ordering-dependent |
| D3 | `patch("scheduler.time.monotonic", side_effect=[0, 0, 2, 2])` — 4-element list. A code change that calls `monotonic()` one more time breaks the test with `StopIteration`, not an assertion. | `:262, :305, :352, :428, :953` | Brittle; hides real timing changes |
| D4 | `patch("scheduler.time.sleep", return_value=None)` — masks *every* sleep, making retries effectively free. Retry-exhaustion scenarios are untestable because sleep is never honored. | Same | Incomplete retry coverage |
| D5 | `test_dotenv_is_loaded_from_module_directory` writes to the real `referee-server/.env` file, reloads `config` via `importlib.reload`, then restores the original text. If the test is killed mid-execution (Ctrl-C, OOM), the developer's actual `.env` is overwritten by the test one. | `test_scoring_and_drift.py:1058-1075` | Source-tree mutation on crash |
| D6 | `importlib.reload(config)` after `patch.dict(os.environ, {}, clear=True)` — any other test that already imported `config` holds a reference to the pre-reload `SETTINGS` object. Test ordering matters. | `:1070-1074` | Order-dependent |
| D7 | `sys.modules.pop("app", None)` + `importlib.import_module("app")` — re-executes `app.py` at test time, which performs DB migrations and instantiates a `RefereeRuntime` against the *module-level default* DB before the test swap. Works today, fragile forever. | `:1098-1100` | Ordering; hidden I/O |
| D8 | `datetime.now(UTC)` baked into every synthetic snapshot via `_snapshot()`. Not frozen. Tests pass today because scoring ignores `checked_at`, but any future assertion on `checked_at` would silently flake. | `:118`, and every test using `_snapshot()` | Time-coupled latent flake |
| D9 | `tempfile.mkstemp(suffix=".db")` → SQLite tempfile per test + `addCleanup(path.unlink)` — 31 API tests each create + migrate a fresh DB. Total disk I/O ≈ 31 × ~86KB schema init = ~3MB, but more importantly it's ~340ms/test wall-clock dominated by DB setup. | Every `make_runtime`/API `setUp` | Slow; not deterministic across FS types |
| D10 | `test_team_admin_endpoints_create_ban_and_unban` URL-encodes `Team%20Alpha` by hand; depends on the server's URL-decode exactly matching. A minor middleware addition (redirect, trailing-slash normalization) breaks it silently. | `:1516-1521` | Encoding-fragile |

### 2.3 Complexity / over-engineering

| # | Smell | Where | What should happen |
|---|---|---|---|
| C1 | The 9-snapshot "all variants × all nodes unclaimed" litany is copy-pasted **17+ times** — ~15 lines each. 250+ lines of visual noise. | e.g. `:221-231, :336-346, :402-423, :912-922, :1666-1676` | Extract `_healthy_matrix()` helper. |
| C2 | `_override_runtime_settings()` is the only reason every class has a 3-line `setUp`. Should be a pytest `autouse` fixture with teardown. | `:81-96`, every class | One fixture replaces 4 repeated `setUp`s. |
| C3 | `DummySSH`, `DummyScheduler`, `DummyTemplates` are each used by multiple test classes but defined inline. | `:34-78` | Move to `conftest.py` as fixtures. |
| C4 | `unittest.TestCase` + `self.addCleanup(lambda: ...)` + `Mock(side_effect=...)` — three different "cleanup / injection" styles in one file. | Throughout | Pick one: pytest-style with fixtures. Drop `unittest`. |
| C5 | `ApiEndpointTests.setUp` does 8 things: override settings, override admin key, mkstemp DB, init DB, build runtime, monkeypatch `start_scheduler`/`shutdown`, reload `app` module with `DummyTemplates`, rebind `app_module.db/runtime/ssh_pool`. Every API test pays this cost. | `:1079-1113` | Split into module-scoped fixture for the app + function-scoped fixture for the DB. Cost amortized 31×. |
| C6 | `side_effect=itertools.repeat(([], {}))` and `side_effect=[(violating, {}), (violating, {}), (clean, {}), (violating, {})]` — hardcoded mock call sequences. If a code refactor reorders probe calls, the mapping silently shifts. | `:300, :644` | Replace with a named-response map keyed on `(series, variant, call_index)` or a small fake. |
| C7 | 61 tests at avg 340ms each = slow-feedback loop. Most of the cost is in `ApiEndpointTests.setUp` (module reload + DB init). | — | Targeted fixtures would cut this to <5s for the whole suite. |
| C8 | `object.__setattr__(SETTINGS, ...)` + `addCleanup(restore)` is used for: `node_hosts`, `node_priority`, `variants`, `min_healthy_nodes`, `admin_api_key`, `deploy_health_timeout_seconds`, `referee_log_path`, `haproxy_log_path`. Eight settings, one pattern, four variants of the same 3-line mutation block. | Throughout | One pytest fixture `settings_override(**kwargs)` — done once. |
| C9 | `test_poll_endpoint_requires_admin_key_and_cannot_award_points` tests two things (auth + no-side-effect). Name telegraphs the smell. | `:1202-1231` | Split into two tests. |
| C10 | `test_admin_public_config_and_notifications_flow` is a 40-line scenario test that exercises 4 endpoints sequentially. Tests workflow, not units. | `:1618-1659` | Keep as an integration test but move to `tests/integration/` so unit-test failures don't mix with workflow regressions. |

### 2.4 Logic issues (assertion quality)

| # | Concern | Where |
|---|---|---|
| L1 | `test_baseline_snapshots_without_hits_do_not_escalate_team` asserts `team["offense_count"] == 0` — but with no violations, that's the default. The test would pass even if `escalate_team` were never called. Should assert the escalation path was not invoked (spy on `enforcer.escalate_team`). | `:557-582` |
| L2 | `test_repeated_violation_only_escalates_once_until_cleared` uses hand-built ss(8)-style `PORTS` output with a single extra listen line. The parser is fragile to whitespace/column widths; if the production ss output format shifts one space, this test keeps passing while production breaks. | `:584-659` |
| L3 | `test_h1b_authkeys_change_is_exempt_from_baseline_violation` and `test_h7b_shadow_change_is_exempt_from_baseline_violation` verify the exemption dict literal at `scheduler.py:39-42`. They pin the current behavior but don't detect regressions introduced by *other* exemptions added to the same dict. Parametrize. | `:475-555` |
| L4 | `test_authoritative_owner_is_reconciled_to_divergent_healthy_replica` asserts `len(ssh.commands) == 1` and `host == "192.168.0.106"` — that's the only divergent node in the fixture. Relies on implementation detail that reconciliation issues one SSH per divergence. Two divergent replicas would issue two commands; the test wouldn't notice if the reconciler stopped at one. | `:788-829` |
| L5 | `test_resume_requires_validated_current_series` mocks `poller.run_cycle` to return `([], {})` — no snapshots at all. The test asserts `RuntimeGuardError` and `status == "faulted"`. Correct today, but "no snapshots" and "wrong snapshots" go through different code paths; only the first is tested. | `:831-842` |
| L6 | `test_dashboard_route_renders_template` and the participant variants patch `Jinja2Templates` with a `DummyTemplates` that always returns `"<html><body>ok</body></html>"`. So it asserts `status_code == 200`. This tests routing, not the template. The template's rendered HTML is entirely untested. | `:1233-1246` |
| L7 | `test_runtime_endpoint_returns_extended_state` checks that `payload["active_jobs"]` contains `"poll"` and `"rotate"` — but the scheduler is `DummyScheduler`, which stores jobs as a plain dict. The test is checking that the test double is wired correctly; production jobs still might not show up right. | `:1132-1159` |
| L8 | `test_increment_poll_cycle_updates_last_poll_at` asserts `last_poll_at >= before` using `datetime.now(UTC)` both sides. On Windows clock resolution (16ms), a fast test can have `before == last_poll_at` to the microsecond. `>=` saves it today. Fragile on clock sources. | `:1608-1616` |
| L9 | In `test_poll_endpoint_requires_admin_key_and_cannot_award_points`, `with self.app_module.db._lock:` acquires the DB lock *in the test* to verify no inserts. A bug where `_lock` becomes a no-op lock would defeat the assertion silently. | `:1229-1231` |

### 2.5 Coverage gaps inside `referee-server/`

- **Enforcer (`enforcer.py`)**: exercised only as a side-effect of scheduler integration tests. No direct unit test of the offense → (warning / series_ban / full_ban) cascade.
- **`db.py`** (932 LOC): no direct unit tests. Schema migrations via `_ensure_column` are untested; a dropped column would be noticed only if a downstream integration test happens to read it.
- **`webhook.py`**: not imported by any test.
- **`runtime_logging.py`**: not imported.
- **`setup_cli.py`**: not imported.
- **`ssh_client.py`**: happy path only. No timeout, no connection-refused, no host-key rejection, no paramiko exception propagation.
- **`models.py`**: Pydantic models only implicitly validated via `ApiEndpointTests`.
- **`scheduler.py`**: tested for lifecycle FSM happy paths; *not* tested for: concurrent `poll_once` vs `rotate`, SSH partial failure mid-deploy, baseline capture race after rotate, webhook failure on hot path, `faulted → running` auto-recovery, scheduler job collision with `max_instances=1` under manual `/api/poll`.

---

## 3. `test_ssh_targets.py` — Scrutiny

Three tests, 101 LOC. Honest in scope; over-clever in execution.

| # | Finding | Line |
|---|---|---|
| S1 | Fake `paramiko` injected at import — same determinism issue as D1 above. | `:10-16` |
| S2 | `_FakeSSHClient.exec_command` returns `None` for `stdin` — works because the caller uses `_, stdout, stderr = client.exec_command(...)`. Relies on tuple-unpack order. | `:49-51` |
| S3 | `SettingsTargetValidationTests` mutates the frozen `SETTINGS` via `object.__setattr__` and restores via `addCleanup`. Same concern as D2. | `:92-101` |
| S4 | No test for: key file missing, `strict_host_key_checking=True` rejection, `AutoAddPolicy` path, Paramiko `AuthenticationException` propagation, exec timeout, reconnection after reset. These are the real SSH failure modes. | — |

---

## 4. `qa/` — Scrutiny (condensed from parallel agent pass; cross-checked)

### 4.1 What each file is

| File | One-line purpose |
|---|---|
| `qa/common.py` | HTTP/TCP/UDP helpers + multipart + table printing. |
| `qa/targets.py` | Roster of 24 machines with ports and protocols. |
| `qa/load_suite.py` | Concurrent load probe; p95 latency table. |
| `qa/vuln_suite.py` | 24 exploit probes, one per machine. |
| `qa/koth_load_sim.py` | Long-running asyncio traffic simulator. |
| `qa/deployment/emulate_referee_paths.py` | **Safe** — fake SSH, referee bootstrap rehearsal. |
| `qa/deployment/validate_rule_matrix_live.py` | **Dangerous** — live-mutation rule validator. |
| `qa/deployment/validate_koth_node.sh` | Read-only node health. |
| `qa/deployment/validate_referee_lb.sh` | Read-mostly referee + LB validation. |
| `qa/deployment/configure_koth_ufw.sh` | UFW rule preview / apply. |
| `qa/deployment/prebuild_series_cache.sh` | Parallel `docker compose build` over nodes. |

### 4.2 Side-effect risk (by file)

| Risk | File | Evidence |
|---|---|---|
| **Critical** | `validate_rule_matrix_live.py` | `docker exec -u 0`, rewrites `/root/king.txt`, flushes iptables, kills processes by PID, `sudo systemctl stop koth-referee`, backs up and restores live SQLite, creates and bans real teams. No "are you sure you're on staging?" gate. |
| **High** | `validate_referee_lb.sh --dry-run` | Creates test teams in live DB; restarts `koth-referee` via sudo. |
| **High** | `configure_koth_ufw.sh --apply` | `eval "$cmd"`; mis-applied rules can lock out operators. |
| **Medium** | `prebuild_series_cache.sh` | SSH into 3 nodes, parallel builds; blocks for many minutes; no SSH timeout. |
| **Low** | `load_suite.py`, `vuln_suite.py`, `koth_load_sim.py` | Non-mutating probes; but note they still generate real traffic and triggers, which can cause legitimate banned-team or rate-limit effects. |
| **None** | `emulate_referee_paths.py`, `validate_koth_node.sh` | Fake SSH / read-only. |

### 4.3 Determinism issues in `qa/`

| # | Location | Issue |
|---|---|---|
| Q1 | `common.py` boundary generation via `int(time.time() * 1000)` | Non-reproducible multipart body across runs. |
| Q2 | `koth_load_sim.py` — `random.randint`, `random.choice` unseeded | Think-time and probe selection vary across runs; no `--seed`. |
| Q3 | `validate_rule_matrix_live.py` — `service_probe_port = 55000 + int(time.time()) % 1000` | Port collision if two runs within ~1000s on the same box. |
| Q4 | `validate_rule_matrix_live.py` — `time.sleep(4)`, `time.sleep(5)` as post-restart barriers | Fails silently on slow boots. Poll-until-ready instead. |
| Q5 | `load_suite.py` — `as_completed()` iteration, results sorted later | Order-dependent p95 math only if the summarizer misuses order, which it doesn't — but the dead `len(body) >= 0` check below hides real empty-body bugs. |
| Q6 | Shell scripts use fixed paths: `/tmp/ref_*.out` never cleaned; `$HOME/.ssh/id_rsa` default without existence check. | Accumulation + cryptic failure modes. |

### 4.4 Complexity in `qa/`

| # | Place | Simplification |
|---|---|---|
| QC1 | `vuln_suite.py` — 24 check functions that differ only in `{path, method, payload, marker}` | Table-driven dispatcher with per-target config dict. Est. **−350 LOC**. |
| QC2 | `validate_rule_matrix_live.py` — 7 near-identical test harnesses (`safe_capture`, `dangerous_root_dir`, four `special_*`, `_dangerous_probe`) | Single `run_probe_test(...)` taking an `ExpectedEvent` and a `Restore` callable. Est. **−200 LOC**. |
| QC3 | `validate_rule_matrix_live.py:189-210` — SQL built via `textwrap.dedent(f"...{repr(...)}...")` 3-level escaping | Use `sqlite3` API directly. Est. **−25 LOC** and **+1 crash category removed**. |
| QC4 | `load_suite.py` computes p50/p99 but only prints p95 | Either print all percentiles or compute only p95. |
| QC5 | `koth_load_sim.py` port bucketing with start>end swap handling and filtering invalid ports | `range()` + comprehension. |

### 4.5 Logic issues in `qa/`

| # | Concern | Where |
|---|---|---|
| QL1 | `load_suite.py:34` — `ok = 200 <= status < 500 and len(body) >= 0` — second clause is always true. Likely meant `> 0`. Currently any empty 200 passes. | `load_suite.py:34` |
| QL2 | `vuln_suite.py:315` — Heartbleed check looks for exact byte strings `b"ssh_password=web123"`, `b"username=webuser"`. If the seed data ever changes, test *passes without the leak* because the `WARN` branch is quiet. | `vuln_suite.py:315` |
| QL3 | `vuln_suite.py` silently returns `WARN` when `mongosh`/`smbclient`/`snmpwalk` are absent, and the default exit code treats WARN as pass. Degraded coverage → green CI. | `vuln_suite.py:520-524` |
| QL4 | `validate_rule_matrix_live.py:282` — `float(team_after["total_points"]) >= baseline_points + 1.0` can be satisfied by unrelated scoring events during the poll window. False positive. | `validate_rule_matrix_live.py:282` |
| QL5 | `validate_rule_matrix_live.py:370` — `violation_events AND ban_events`; if only one lands, test fails silently with "no match". Intent unclear — specify. | `validate_rule_matrix_live.py:370` |
| QL6 | Shell scripts have `set -euo pipefail` (good) but no `trap` for cleanup. Crashes leave `/tmp/ref_*.out`, partial builds, half-applied UFW. | `validate_referee_lb.sh:97`, others |
| QL7 | `prebuild_series_cache.sh` has no `ConnectTimeout`; one unreachable node hangs the whole run. | `prebuild_series_cache.sh:150,158` |
| QL8 | `validate_referee_lb.sh` hardcodes `API_URL=http://127.0.0.1:8000`; assumes `REMOTE_REPO=/opt/KOTH_orchestrator/repo`. Fails unhelpfully elsewhere. | `validate_referee_lb.sh:6,56,144` |

### 4.6 Gaps

- No multi-team concurrent-claim stress test.
- No replica-divergence harness (H1A image digest on node1 vs node2).
- No rotation-under-load (concurrent `/api/rotate/skip` while 100 teams are polling).
- No SSH partial-failure probe (node down, node slow, node rejecting key).
- No HAProxy backend-failover test.
- No auth rotation test (rotate `ADMIN_API_KEY` mid-run).
- No DB corruption / integrity-check drill.
- No chaos (tc / iptables / packet loss) at all.
- No test for `qa/` itself — the validators are untested code running with root-equivalent privileges.

---

## 5. What I'd change before the next event

Ranked by leverage per unit of effort. S = <1 day, M = 1–3 days, L = week+.

| # | Change | Why | Effort |
|---|---|---|---|
| **1** | Add `pyproject.toml` with `[tool.pytest.ini_options]` (testpaths, `-ra`, strict markers, `xfail_strict=true`), `[tool.coverage.run]`, and a `[tool.ruff]` section. Pin `pytest==8.x`, `coverage`, `pytest-xdist`, `pytest-freezer`. | Today there is literally no test config. | S |
| **2** | Create `referee-server/tests/conftest.py` with fixtures: `settings_override(**kw)`, `runtime_and_db` (module-scoped), `dummy_ssh`, `dummy_scheduler`, `healthy_matrix(series)`, `violating_matrix(series, variant, ...)`. Kill every `object.__setattr__(SETTINGS, ...)` call at the test level. | Removes D2, C2, C3, C5, ~200 LOC of repetition. Makes xdist safe. | M |
| **3** | Split the 1,704-line file: `tests/unit/test_scorer.py`, `tests/unit/test_poller.py`, `tests/unit/test_enforcer.py` (new — test directly), `tests/integration/test_lifecycle.py`, `tests/integration/test_api.py`, `tests/integration/test_recovery.py`. Mirror the production split. | Cuts feedback loops; enables `pytest tests/unit` in <2s. | M |
| **4** | Add direct unit tests for `enforcer.py`, `db.py` (schema + migrations), `webhook.py`, `ssh_client.py` failure paths. Target 80% line coverage on `referee-server/` and fail CI below that. | Today these are untested despite being authoritative. | M |
| **5** | Replace `patch("scheduler.time.monotonic", side_effect=[...])` with `pytest-freezer` / `freezegun`. Replace `patch("scheduler.time.sleep", return_value=None)` with a fixture that records sleeps for later assertion. | Fixes D3, D4. Makes retry/timeout tests meaningful. | S |
| **6** | Make `DummySSH` / `DummyScheduler` real fakes in `conftest.py`: record every call with args and a settable response map keyed by input. Then test that the scheduler issues the *right* command, not just that it issues *a* command. | Fixes L2, L4, L7. | S |
| **7** | Collapse `qa/vuln_suite.py`'s 24 functions into one config-driven dispatcher: `PROBES: dict[str, ProbeConfig]`. Each probe entry declares path/method/payload/marker and any required helper tool. If tool missing → `FAIL`, not `WARN`. Add `--allow-degraded` for the current WARN behavior. | QC1 + QL3. −350 LOC; CI stops lying. | M |
| **8** | Collapse `qa/deployment/validate_rule_matrix_live.py`'s seven harnesses into `run_probe_test(...)`. Add a top-of-file runtime guard: `assert os.environ.get("KOTH_ALLOW_LIVE_MUTATION") == "yes-I-really-mean-it"` or abort. Add a dry-run mode. | QC2 + the #1 safety hazard. | M |
| **9** | Shell scripts: add `trap 'rm -rf "$tmpdir"' EXIT`, `ssh -o ConnectTimeout=30`, an existence check for `SSH_PRIVATE_KEY`, and a preflight that refuses to run `configure_koth_ufw.sh --apply` when already connected via SSH (UFW lockout class). | QL6, QL7; one real outage averted. | S |
| **10** | Add a `tests/chaos/` dir with: multi-team race for one variant, replica divergence reconciliation, rotate-under-load, SSH partial failure, webhook slow receiver. Mark `@pytest.mark.chaos`; off by default, on in a nightly job. | Fills the biggest coverage gap; turns § of AUDIT.md "single flaky node = zero scoring" into a test. | L |
| **11** | Seed the QA RNG: every random-driven script accepts `--seed`, calls `random.seed(seed)` and logs it. Drop millisecond-time boundaries in `common.py` in favor of a counter or `uuid4`. | Q1, Q2. Reproducible test runs. | S |
| **12** | Move `test_dotenv_is_loaded_from_module_directory` into a subprocess-launched test that writes to a `tmp_path`-provided `.env`, not the source tree. | D5 — the one test that can damage the developer's checkout. | S |

---

## 6. Principal engineer's one-paragraph verdict

The suite is green and meaningful on the happy paths — the quorum and drift logic is properly exercised, and the lifecycle FSM has real integration coverage. What's missing is structure: no shared fixtures, no test configuration, no layering between unit and integration, no direct tests for the enforcer or database, and a test file almost as large as the code it tests. The QA probes outside `tests/` are operationally dangerous and duplicated at industrial scale; the rule-matrix validator in particular can silently corrupt a live competition and must get a safety guard before anything else. Ship #1, #2, and #8 from the plan above before the next event; everything else is refactoring that the #2 fixture base will make trivial.
