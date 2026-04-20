# Consensus KoTH

Consensus KoTH is a distributed King of the Hill cyber range with a centralized referee, quorum-based ownership validation, replicated challenge nodes, and operator tooling for lifecycle control, scoring, health validation, and recovery.

It is built for events where a single write on a single box is not good enough to declare ownership. Players still compromise services and write a team name into `/root/king.txt`, but points are awarded only when the referee has enough healthy evidence across replicas to trust the claim.
- Production runtime is the distributed referee-managed model under `referee-server/` plus node-local `h1..h8` directories on `node1/node2/node3`.
- `Series HN/docker-compose.yml` files are the per-series deploy artifacts copied into node-local `hN/` directories and validated by the referee before a series becomes active.
- Root-level `docker-compose.yml` and `rotate.sh` are local/dev-only artifacts and must not be treated as the production control plane.
- Operators should use `/api/runtime`, `/api/recover/validate`, and `/api/recover/redeploy` for runtime inspection and recovery.
- The dashboard now includes admin team controls: create teams, manually ban a team, and manually unban a team. New team names must satisfy the same claim rules as `king.txt` ownership, so reserved or malformed names such as `unclaimed` are rejected.
- The public participant board on `:9000` now shows the current access window, organizer notices, hard-bound rules, a live leaderboard, and a cumulative score graph for the leading teams.
- The admin dashboard on `:8000` now renders the full team table instead of truncating to the first 25 teams.
- Manual test execution guide: [docs/manual-tester-checklist.md](docs/manual-tester-checklist.md)
- Referee rule validation guide: [docs/referee-rule-validation-checklist.md](docs/referee-rule-validation-checklist.md)
- Separate attacker-style Codex prompt: [docs/codex-h1a-player-prompt.md](docs/codex-h1a-player-prompt.md)

## Why This Exists

Classic KoTH infrastructure usually assumes one target, one owner, and a simple "last write wins" or "first stable owner wins" model. That breaks down once you add:

- multiple challenge nodes
- a load balancer in front of them
- replication lag or divergent local state
- host or container health drift
- operator recovery during a live event

Consensus KoTH solves that by making the referee authoritative. The nodes expose the services; the referee decides when ownership is real.

## Core Capabilities

- Distributed challenge runtime across three challenge nodes
- Central referee that owns scoring, lifecycle, validation, and recovery
- Quorum-based ownership acceptance instead of single-node trust
- Automated series rotation across `H1` through `H8`
- HAProxy-aware routing and backend state visibility
- Admin dashboard for operators and separate participant board for players
- Team management with ban/unban controls and claim-name validation
- Recovery APIs for validation and controlled redeploys
- Protocol-aware QA harness for load checks and safe exploit proofing
- Detailed deployment runbooks and validation checklists

## Safety

This repository contains intentionally vulnerable services and tooling that performs real exploit probes against them.

Use it only for:

- owned lab environments
- authorized training exercises
- private or sanctioned competitions

Do not deploy this against infrastructure you do not control.

## How Ownership Works

Consensus KoTH is not a naive file-write race. The scoring path is:

1. Players attack the currently exposed public challenge port.
2. A successful player reaches root and writes their exact team name into `/root/king.txt`.
3. The referee polls all healthy replicas for each active variant on a fixed interval.
4. The referee filters out invalid, stale, malformed, or unhealthy observations.
5. A new owner is accepted only when that claim reaches quorum across healthy nodes.
6. Once accepted, that owner becomes the authoritative owner for the variant.
7. As long as the accepted owner keeps quorum, the team receives points each poll cycle.
8. If replicas diverge, the referee can reconcile the accepted owner back onto healthy nodes.

This design makes ownership durable under partial failure and defensible in logs.

## Architecture

Typical production topology:

```text
Players
  |
  v
HAProxy + Referee Host
  |- FastAPI admin dashboard (:8000)
  |- Participant board (:9000)
  |- Scheduler / scorer / recovery logic
  |- SQLite runtime state
  |
  +----> node1
  +----> node2
  +----> node3
           |- active series containers
           |- per-series docker-compose.yml
           |- local king.txt observations
```

Important runtime properties:

- One referee process owns the control plane and scoreboard.
- Three challenge nodes host the currently active series.
- HAProxy exposes only the active public services.
- The referee talks to nodes over SSH.
- State persists in SQLite so runtime intent survives restarts.
- Recovery is explicit: validate first, redeploy deliberately, resume only after health is re-established.

## Event Structure

The event is split into eight series:

- `H1`
- `H2`
- `H3`
- `H4`
- `H5`
- `H6`
- `H7`
- `H8`

Each series contains three machine variants:

- `A`
- `B`
- `C`

That gives a total of 24 challenge targets across a full event.

## Challenge Matrix

Each machine has an initial access vector and a privilege escalation path.

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

## Runtime State Model

The referee uses explicit competition states:

- `stopped`
- `starting`
- `running`
- `paused`
- `rotating`
- `faulted`
- `stopping`

Operational guarantees:

- No scoring while paused
- No silent progression through failed deploys or failed rotations
- No accepted ownership without healthy evidence
- Missing probe data is treated as failure, not success
- Recovery keeps the event paused until operators validate the current series

## Repository Layout

```text
.
|-- README.md
|-- docker-compose.yml              # local aggregate stack, not production control
|-- rotate.sh                       # local legacy helper, not production rotation logic
|-- Series H1/ ... Series H8/       # challenge content by series
|-- referee-server/
|   |-- app.py                      # FastAPI apps and API routes
|   |-- scheduler.py                # lifecycle, deploy, rotate, recover
|   |-- scorer.py                   # quorum-based winner selection
|   |-- poller.py                   # node and container observations
|   |-- enforcer.py                 # rule and violation enforcement
|   |-- db.py                       # SQLite persistence
|   |-- ssh_client.py               # SSH execution layer
|   |-- templates/                  # admin and participant UI templates
|   `-- tests/                      # referee runtime tests
|-- qa/
|   |-- load_suite.py               # protocol-aware load probes
|   |-- vuln_suite.py               # safe exploit validation probes
|   `-- deployment/                 # deployment and recovery validation tooling
`-- docs/                           # runbooks, design docs, validation guides
```

## Two Ways To Run It

### 1. Production Mode

Production uses the distributed referee-managed model:

- node-local `h1` through `h8` directories on each challenge node
- copied per-series `docker-compose.yml` artifacts on those nodes
- a central referee host running FastAPI, scheduler, and HAProxy
- HAProxy serving only the active series

This is the intended event topology.

### 2. Local / Dev Mode

The repository root also includes:

- [`docker-compose.yml`](docker-compose.yml) for bringing up the full challenge set locally
- [`rotate.sh`](rotate.sh) as a local helper

These are useful for development and content testing, but they are not the authoritative production orchestration path.

## Quick Start

### Run the Referee Locally

```bash
cd referee-server
python -m venv .venv
```

Activate the virtual environment:

- macOS/Linux: `source .venv/bin/activate`
- PowerShell: `.venv\Scripts\Activate.ps1`

Install dependencies and start the referee:

```bash
pip install -r requirements.txt pytest
uvicorn app:app --host 0.0.0.0 --port 8000
```

Open:

- admin dashboard: `http://127.0.0.1:8000`
- participant board: `http://127.0.0.1:9000`
- participant leaderboard: `http://127.0.0.1:9000/leaderboard`

### Run Tests

```bash
python -m pytest referee-server/tests -q
```

## Configuration

The referee loads settings from process environment first, then from `referee-server/.env` if present.

### Important Environment Variables

| Variable | Purpose |
|---|---|
| `ADMIN_API_KEY` | Required by default for admin access |
| `DB_PATH` | SQLite path for runtime state |
| `NODE_HOSTS` | Comma-separated challenge node hosts |
| `NODE_PRIORITY` | Tie-break order for equivalent observations |
| `NODE_SSH_TARGETS` | Optional per-node `user@host` SSH targets |
| `SSH_USER` | Default SSH username |
| `SSH_PORT` | Default SSH port |
| `SSH_PRIVATE_KEY` | SSH private key path |
| `SSH_TIMEOUT_SECONDS` | SSH command timeout |
| `SSH_STRICT_HOST_KEY_CHECKING` | Host key enforcement toggle |
| `VARIANTS` | Expected variants, usually `A,B,C` |
| `TOTAL_SERIES` | Number of series, default `8` |
| `MIN_HEALTHY_NODES` | Quorum threshold for ownership |
| `MAX_CLOCK_DRIFT_SECONDS` | Clock drift tolerance before degradation |
| `POLL_INTERVAL_SECONDS` | Scoring poll cadence |
| `ROTATION_INTERVAL_SECONDS` | Series rotation cadence |
| `POINTS_PER_CYCLE` | Points awarded per retained cycle |
| `DEPLOY_HEALTH_TIMEOUT_SECONDS` | Health gate timeout during deploy/rotate |
| `DEPLOY_HEALTH_POLL_SECONDS` | Health gate poll interval |
| `REMOTE_SERIES_ROOT` | Root containing `h1` through `h8` on nodes |
| `DOCKER_COMPOSE_CMD` | Compose command used remotely |
| `CONTAINER_NAME_TEMPLATE` | Container name format for node actions |
| `BACKEND_URL` | Optional external team roster / score sink |
| `WEBHOOK_URL` | Optional webhook for event notifications |
| `REFEREE_LOG_PATH` | Referee log path |
| `HAPROXY_LOG_PATH` | HAProxy log path |
| `HAPROXY_CONFIG_PATH` | HAProxy config path |
| `HAPROXY_ADMIN_SOCKET_PATH` | HAProxy admin socket path |

### Example `.env`

```env
ADMIN_API_KEY=replace-me
DB_PATH=./referee.db

NODE_HOSTS=192.168.0.70,192.168.0.103,192.168.0.106
NODE_PRIORITY=192.168.0.70,192.168.0.103,192.168.0.106
NODE_SSH_TARGETS=node1@192.168.0.70,node2@192.168.0.103,node3@192.168.0.106

SSH_USER=root
SSH_PORT=22
SSH_PRIVATE_KEY=~/.ssh/koth_referee
SSH_TIMEOUT_SECONDS=8
SSH_STRICT_HOST_KEY_CHECKING=true

VARIANTS=A,B,C
TOTAL_SERIES=8
MIN_HEALTHY_NODES=2
MAX_CLOCK_DRIFT_SECONDS=2
POLL_INTERVAL_SECONDS=30
ROTATION_INTERVAL_SECONDS=3600
POINTS_PER_CYCLE=1.0

REMOTE_SERIES_ROOT=/opt/KOTH_orchestrator
DOCKER_COMPOSE_CMD=docker compose
CONTAINER_NAME_TEMPLATE=machineH{series}{variant}
```

## Admin and Participant Surfaces

### Admin Dashboard

The admin dashboard is the operator control surface. It exposes:

- competition lifecycle controls
- rotation and restart controls
- recovery validation and redeploy actions
- leaderboard and team moderation
- routing visibility
- host and container telemetry
- claims timeline and recent events
- participant board configuration and manual notifications

### Participant Board

The participant board is intended for players. It exposes:

- current series
- public host and port information
- organizer headline and subheadline
- current participant notifications
- a separate live leaderboard page with public standings

## API Overview

### Public / Participant

- `GET /`
- `GET /leaderboard`
- `GET /api/public/dashboard`
- `GET /api/public/leaderboard`

### Admin / Protected

- `GET /api/status`
- `GET /api/runtime`
- `GET /api/lb`
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

All admin endpoints require `X-API-Key` unless unsafe startup is explicitly enabled.

## Deployment and Operations

For the real deployment flow, start with these documents:

- [docs/full-deployment-runbook.md](docs/full-deployment-runbook.md)
- [docs/deployment-validation-checklist.md](docs/deployment-validation-checklist.md)
- [docs/referee-per-node-ssh-targets.md](docs/referee-per-node-ssh-targets.md)
- [docs/haproxy-full-config.md](docs/haproxy-full-config.md)

For event execution and validation:

- [docs/manual-tester-checklist.md](docs/manual-tester-checklist.md)
- [docs/referee-rule-validation-checklist.md](docs/referee-rule-validation-checklist.md)
- [docs/production-remediation-design.md](docs/production-remediation-design.md)

## QA and Validation Tooling

### Referee Test Suite

[`referee-server/tests/`](referee-server/tests/) covers:

- quorum winner selection
- clock drift handling
- deploy and rotation rollback behavior
- resume and recovery guardrails
- API authorization
- routing and telemetry endpoints
- public dashboard flows

### Stack QA

[`qa/README.md`](qa/README.md) covers service-level probes:

- `qa/load_suite.py` for protocol-aware load checks
- `qa/vuln_suite.py` for non-destructive exploit validation

### Deployment QA

[`qa/deployment/README.md`](qa/deployment/README.md) covers host and deployment validation:

- `validate_koth_node.sh`
- `validate_referee_lb.sh`
- `emulate_referee_paths.py`
- `prebuild_series_cache.sh`
- `configure_koth_ufw.sh`

## Documentation Map

If you are new to the project, read in this order:

1. This README
2. [docs/full-deployment-runbook.md](docs/full-deployment-runbook.md)
3. [docs/deployment-validation-checklist.md](docs/deployment-validation-checklist.md)
4. [docs/manual-tester-checklist.md](docs/manual-tester-checklist.md)
5. [docs/referee-rule-validation-checklist.md](docs/referee-rule-validation-checklist.md)

If you are validating or extending the control plane, also read:

- [docs/production-remediation-design.md](docs/production-remediation-design.md)

If you want an attacker-style one-box benchmark:

- [docs/codex-h1a-player-prompt.md](docs/codex-h1a-player-prompt.md)

## Contributing

Contributions are easiest to review when they follow the actual runtime model:

- treat `referee-server/` as the production control plane
- treat root `docker-compose.yml` and `rotate.sh` as local/dev helpers
- keep generated state, logs, and local databases out of source control
- prefer changes that preserve explicit lifecycle and recovery behavior
- update the relevant runbook or checklist when changing deployment assumptions

For non-trivial changes, include:

- the runtime behavior being changed
- the operator impact
- the recovery or rollback implications
- test coverage or manual validation notes

## Open-Source Hygiene

This repository intentionally ignores local runtime artifacts such as:

- `.env` files
- local SQLite databases
- local logs
- live backup directories
- generated QA result files

That keeps the repository publishable without mixing it with event-local state.

## Current Status

Consensus KoTH is already usable as a private event platform, but it is opinionated:

- it assumes a three-node challenge topology
- it assumes a central referee host
- it assumes quorum-based ownership instead of independent per-node scoring

If that matches the event design, this repository gives you the core range, control plane, and validation tooling in one place.
