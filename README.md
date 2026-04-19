# Consensus KoTH

Consensus KoTH is a distributed cyber range and referee system for King of the Hill competitions. It runs intentionally vulnerable challenge services across multiple nodes, exposes them through a central traffic surface, and awards ownership only when the referee has quorum-backed evidence instead of trusting a single-box write.

This repository contains both the control plane and the challenge content:

- `referee-server/` is the production control plane.
- `Series H1` through `Series H8` define the challenge sets.
- `qa/` contains load, exploit-safety, and deployment validation tooling.
- The root `docker-compose.yml` and `rotate.sh` are local/dev conveniences, not the authoritative production runtime.

## Safety

This project is for owned infrastructure, lab environments, and authorized competition use only.

- The challenge boxes are intentionally vulnerable.
- The QA harness performs real exploit probes against those services.
- The deployment tooling assumes you control the hosts, network policy, and SSH access.

Do not point this stack at third-party infrastructure.

## What Makes This Different

Consensus KoTH is not a naive "first team to write a file on one host wins" KoTH.

The scoring model is referee-driven:

1. Players attack the currently active public challenge ports.
2. A successful team writes its exact team name into `/root/king.txt`.
3. The referee polls all healthy replicas for each variant on a fixed cadence.
4. A new owner is accepted only when the claim reaches quorum across healthy nodes.
5. Once accepted, that owner becomes the authoritative owner for the variant.
6. The referee can reconcile the authoritative owner back onto healthy replicas.
7. Points are awarded per poll cycle while the accepted owner retains quorum.

That model exists to make a load-balanced, replicated event defensible and observable under real runtime drift, partial node failure, and ownership contention.

## Architecture

Typical deployment topology:

```text
Players
  |
  v
HAProxy + Referee Host
  |- FastAPI admin dashboard (:8000)
  |- Participant board (:9000)
  |- Scheduler / scorer / recovery logic
  |- SQLite event + team state
  |
  +----> node1
  +----> node2
  +----> node3
           |- active series containers
           |- per-series docker-compose.yml
           |- local king.txt observations
```

Core runtime characteristics:

- One central referee owns lifecycle, scoring, validation, and recovery.
- Three challenge nodes host the active series.
- HAProxy fronts player traffic to the currently active series.
- The referee reaches nodes over SSH.
- Runtime state persists in SQLite.
- Recovery is explicit: validate first, redeploy only when the current series is known and safe.

## Competition Model

The event is organized as eight series (`H1` through `H8`). Each series exposes three variants (`A`, `B`, `C`), giving a total of 24 challenge targets over a full event.

Runtime lifecycle states:

- `stopped`
- `starting`
- `running`
- `paused`
- `rotating`
- `faulted`
- `stopping`

Operational invariants enforced by the referee:

- No scoring while paused.
- No silent progression through failed rotations.
- No ownership change without sufficient healthy evidence.
- Missing or degraded probe data is treated as failure, not success.
- Recovery keeps the event paused until operators validate the current series.

## Challenge Matrix

Each series contains three challenge variants with an initial access vector and a privilege escalation path.

| Machine | Port Config | Initial Vector | PrivEsc Vector |
|---|---|---|---|
| `machineH1A` | `10001` | WordPress Reflex Gallery RCE | SUID `/usr/bin/find` |
| `machineH1B` | `10002` | Redis unauthenticated access | Write SSH key to `/root/.ssh/` |
| `machineH1C` | `10004` | PHP ping command injection | SUID `/usr/local/bin/net-search` |
| `machineH2A` | `10010` | Jenkins Script Console | `sudo python3` without password |
| `machineH2B` | `10011` | PHP SQL injection | MySQL FILE/UDF privileges |
| `machineH2C` | `10012` | Tomcat default credentials | PwnKit |
| `machineH3A` | `10020` | SMB anonymous share leaks SSH key | `lxd` container breakout |
| `machineH3B` | `10022` | Drupalgeddon2 | Writable root cron `tar *` path |
| `machineH3C` | `10023` | Exposed `.git` leaks app credentials | `/usr/bin/perl` with `cap_setuid` |
| `machineH4A` | `10030` | Node.js deserialization | Cleartext root password in backups |
| `machineH4B` | `10031` | Spring4Shell | Root password in `.bash_history` |
| `machineH4C` | `10032` | SSRF into internal API | Internal root API command execution |
| `machineH5A` | `10040` | Webmin RCE | Direct root execution |
| `machineH5B` | `10041` | ElasticSearch dynamic scripting RCE | Writable `/etc/passwd` |
| `machineH5C` | `10042` | Apache Struts RCE | `sudo` with `LD_PRELOAD` |
| `machineH6A` | `10050` | distcc RCE | NFS `no_root_squash` |
| `machineH6B` | `10052`, `10054` | MongoDB without auth, then reused SSH creds | `docker` group breakout |
| `machineH6C` | `10053`, `10055` | Heartbleed leaks SSH creds | `sudo systemctl start <unit>` |
| `machineH7A` | `10060`, `10061` | SNMP public community | Shared root `tmux` socket hijack |
| `machineH7B` | `10062` | Grafana traversal to admin execution | World-writable `/etc/shadow` |
| `machineH7C` | `10063` | Anonymous rsync write | PATH hijack in root cron |
| `machineH8A` | `10070` | PHPMyAdmin root with blank password | MySQL UDF shell |
| `machineH8B` | `10071` | Flask/Jinja2 SSTI | World-writable sudo policy trick |
| `machineH8C` | `10072` | Laravel Ignition RCE | SUID `bash_suid -p` |

## Repository Layout

```text
.
|-- README.md
|-- docker-compose.yml              # local/dev-only aggregate stack
|-- rotate.sh                       # local/dev-only series switch helper
|-- Series H1/ ... Series H8/       # challenge content by series
|-- referee-server/
|   |-- app.py                      # FastAPI admin and participant apps
|   |-- scheduler.py                # lifecycle, deploy, rotate, recover
|   |-- scorer.py                   # quorum-based winner selection
|   |-- poller.py                   # node/container probe collection
|   |-- enforcer.py                 # violation and policy enforcement
|   |-- db.py                       # SQLite persistence
|   |-- templates/                  # admin and participant dashboards
|   `-- tests/                      # referee runtime coverage
|-- qa/
|   |-- load_suite.py               # protocol-aware load probes
|   |-- vuln_suite.py               # safe exploit validation probes
|   `-- deployment/                 # deployment and recovery validation tooling
`-- docs/                           # operator runbooks and design notes
```

## Referee Server

The control plane lives in [`referee-server/`](referee-server/). It is a FastAPI application with two UIs:

- Admin dashboard on port `8000`
- Participant board on port `9000`

Primary responsibilities:

- competition lifecycle management
- series deployment and teardown
- quorum-based scoring
- health validation before and after rotations
- event and claims timeline persistence
- manual team creation, bans, and unbans
- participant-facing board configuration and announcements
- HAProxy listener and backend visibility
- host and container telemetry
- current-series redeploy and recovery workflows

### Admin API Surface

Important admin endpoints:

- `GET /api/status`
- `GET /api/runtime`
- `GET /api/routing`
- `GET /api/telemetry`
- `GET /api/logs/referee`
- `GET /api/logs/haproxy`
- `GET /api/claims`
- `GET /api/teams`
- `GET /api/events`
- `POST /api/competition/start`
- `POST /api/competition/stop`
- `POST /api/pause`
- `POST /api/resume`
- `POST /api/rotate`
- `POST /api/rotate/restart`
- `POST /api/rotate/skip`
- `POST /api/poll`
- `POST /api/recover/validate`
- `POST /api/recover/redeploy`
- `POST /api/admin/teams`
- `POST /api/admin/teams/{name}/ban`
- `POST /api/admin/teams/{name}/unban`
- `GET /api/admin/public/config`
- `PUT /api/admin/public/config`
- `GET /api/admin/public/notifications`
- `POST /api/admin/public/notifications`
- `DELETE /api/admin/public/notifications/{id}`

All admin endpoints require `X-API-Key` unless the runtime is explicitly configured to allow unsafe startup without one.

### Participant API Surface

Public participant endpoint:

- `GET /api/public/dashboard`

It exposes the current series, public host/port display, headline/subheadline text, and organizer notifications.

## Configuration

The referee loads configuration from process environment first, then from `referee-server/.env` if present.

Key environment variables:

| Variable | Purpose |
|---|---|
| `ADMIN_API_KEY` | Required by default for admin API access |
| `DB_PATH` | SQLite database path for runtime state |
| `NODE_HOSTS` | Comma-separated challenge node hosts |
| `NODE_PRIORITY` | Tie-break order for equivalent observations |
| `NODE_SSH_TARGETS` | Optional per-node `user@host` SSH targets |
| `SSH_USER` / `SSH_PORT` / `SSH_PRIVATE_KEY` | Default SSH connection settings |
| `SSH_TIMEOUT_SECONDS` | Per-command SSH timeout |
| `SSH_STRICT_HOST_KEY_CHECKING` | Host key enforcement toggle |
| `VARIANTS` | Expected variants, usually `A,B,C` |
| `TOTAL_SERIES` | Total number of series, default `8` |
| `MIN_HEALTHY_NODES` | Quorum requirement for acceptance |
| `MAX_CLOCK_DRIFT_SECONDS` | Drift tolerance before degrading nodes |
| `POLL_INTERVAL_SECONDS` | Ownership poll cadence |
| `ROTATION_INTERVAL_SECONDS` | Automatic series rotation cadence |
| `POINTS_PER_CYCLE` | Points awarded for retained ownership |
| `DEPLOY_HEALTH_TIMEOUT_SECONDS` | Rotation/deploy health gate timeout |
| `DEPLOY_HEALTH_POLL_SECONDS` | Health gate polling cadence |
| `REMOTE_SERIES_ROOT` | Root directory containing `h1` through `h8` on nodes |
| `DOCKER_COMPOSE_CMD` | Compose command used remotely |
| `CONTAINER_NAME_TEMPLATE` | Container naming format for probe and repair operations |
| `BACKEND_URL` | Optional external team roster / score sink |
| `WEBHOOK_URL` | Optional webhook for event posting |
| `REFEREE_LOG_PATH` | Structured log file path |
| `HAPROXY_LOG_PATH` | HAProxy log path |
| `HAPROXY_CONFIG_PATH` | HAProxy config location |
| `HAPROXY_ADMIN_SOCKET_PATH` | HAProxy admin socket location |

## Local Development

### 1. Run the referee locally

```bash
cd referee-server
python -m venv .venv
```

Activate the virtual environment before running the server:

- macOS/Linux: `source .venv/bin/activate`
- PowerShell: `.venv\Scripts\Activate.ps1`

Then install dependencies and start the server:

```bash
pip install -r requirements.txt pytest
uvicorn app:app --host 0.0.0.0 --port 8000
```

Open:

- admin dashboard: `http://127.0.0.1:8000`
- participant board: `http://127.0.0.1:9000`

### 2. Understand the local stack entry points

- Use `referee-server/` for the real runtime.
- Use root `docker-compose.yml` only for local/dev bring-up of all challenge services.
- Use `rotate.sh` only as a legacy local helper; it is not the production rotation mechanism.

### 3. Run tests

```bash
python -m pytest referee-server/tests -q
```

## Production Deployment

Production uses the distributed referee-managed model:

- every challenge node gets node-local `h1` through `h8` directories
- each `hN/` contains the copied series artifacts for that hour
- the referee validates a series before marking it live
- HAProxy exposes only the current series

Start with:

- [docs/full-deployment-runbook.md](docs/full-deployment-runbook.md)
- [docs/deployment-validation-checklist.md](docs/deployment-validation-checklist.md)
- [docs/referee-per-node-ssh-targets.md](docs/referee-per-node-ssh-targets.md)

## QA and Validation

The project includes both unit-style referee tests and live-stack validation tooling.

### Referee tests

Coverage in `referee-server/tests/` includes:

- quorum scoring behavior
- clock drift handling
- series deploy and rollback behavior
- recovery guardrails
- authorization checks
- routing and telemetry endpoints
- public participant board flows

### Stack QA

Use [`qa/README.md`](qa/README.md) for the service-level probes:

- `qa/load_suite.py` for protocol-aware concurrent load
- `qa/vuln_suite.py` for safe exploit proofs

### Deployment QA

Use [`qa/deployment/README.md`](qa/deployment/README.md) for deployment verification:

- `validate_koth_node.sh`
- `validate_referee_lb.sh`
- `emulate_referee_paths.py`
- `prebuild_series_cache.sh`
- `configure_koth_ufw.sh`

## Operational Docs

The most important runbooks are:

- [docs/manual-tester-checklist.md](docs/manual-tester-checklist.md)
- [docs/referee-rule-validation-checklist.md](docs/referee-rule-validation-checklist.md)
- [docs/production-remediation-design.md](docs/production-remediation-design.md)
- [docs/haproxy-full-config.md](docs/haproxy-full-config.md)

There is also an attacker-style benchmark prompt for one-box external solving:

- [docs/codex-h1a-player-prompt.md](docs/codex-h1a-player-prompt.md)

## Open-Source Hygiene

This repository intentionally excludes local runtime state and generated validation artifacts such as:

- local `.env` files
- SQLite runtime databases
- live backup directories
- generated QA result JSON

That keeps the repository publishable while allowing real event operations locally.
