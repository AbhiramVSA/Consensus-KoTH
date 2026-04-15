# KOTH Orchestrator Full Deployment Runbook

This document is a complete, end-to-end setup guide for a 3-node distributed KOTH competition with a centralized referee server.

It covers:

1. Infrastructure prerequisites
2. Network and host preparation
3. Challenge node setup (`node1/node2/node3`)
4. Referee server setup (`referee`)
5. Load balancer setup (`lb`)
6. Security hardening
7. Competition operations
8. Validation checklist
9. Troubleshooting

## 1. Reference Architecture

Use this baseline topology (replace IPs/hostnames as needed):

1. `lb` (load balancer): `10.0.0.10`
2. `node1` (challenge host): `10.0.0.11`
3. `node2` (challenge host): `10.0.0.12`
4. `node3` (challenge host): `10.0.0.13`
5. `referee` (FastAPI + scheduler + SQLite): `10.0.0.20`

Competition model:

1. 8 series (`H1..H8`)
2. 3 variants per series (`A/B/C`)
3. Referee polls every 30s
4. Scoring = earliest `king.txt` mtime across healthy nodes, per variant
5. Tie-break = `NODE_PRIORITY` order

## 2. Prerequisites (All Hosts)

## 2.1 OS and access

1. Ubuntu 22.04+ recommended
2. `sudo` access on all hosts
3. SSH connectivity across private VLAN/subnet
4. Internet access for package installs and git clone

## 2.2 Time sync (required)

Earliest-change scoring depends on accurate clocks.

Install and enable chrony on all hosts (`lb`, `node1..3`, `referee`):

```bash
sudo apt update
sudo apt install -y chrony
sudo systemctl enable --now chrony
chronyc tracking
```

Do not proceed until all hosts report synchronized time.

## 2.3 DNS/hostnames (recommended)

Add stable hostnames in `/etc/hosts` on all machines:

```text
10.0.0.10  lb
10.0.0.11  node1
10.0.0.12  node2
10.0.0.13  node3
10.0.0.20  referee
```

## 3. Challenge Node Setup (Repeat on `node1`, `node2`, `node3`)

## 3.1 Install base dependencies

```bash
sudo apt update
sudo apt install -y git curl ca-certificates gnupg lsb-release jq
```

## 3.2 Install Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
newgrp docker
docker --version
docker compose version
```

If your environment needs the `docker-compose` binary name:

```bash
sudo apt install -y docker-compose-plugin
docker-compose --version || true
```

## 3.3 Prepare deployment directory

```bash
sudo mkdir -p /opt/koth
sudo chown -R "$USER:$USER" /opt/koth
cd /opt/koth
git clone https://github.com/Izhaan-Raza/KOTH_orchestrator.git repo
```

## 3.4 Build referee-compatible series layout

The referee expects:

1. `/opt/koth/h1/docker-compose.yml`
2. `/opt/koth/h2/docker-compose.yml`
3. ...
4. `/opt/koth/h8/docker-compose.yml`

Create it:

```bash
cd /opt/koth
for i in 1 2 3 4 5 6 7 8; do
  mkdir -p "h$i"
  cp -r "/opt/koth/repo/Series H$i/"* "/opt/koth/h$i/"
done
```

Validate:

```bash
for i in 1 2 3 4 5 6 7 8; do
  test -f "/opt/koth/h$i/docker-compose.yml" && echo "h$i OK" || echo "h$i MISSING"
done
```

## 3.5 Confirm per-series compose files define containers expected by referee

Container naming must match template:

1. `machineH{series}{variant}`
2. Example: `machineH1A`, `machineH1B`, `machineH1C`

Quick checks:

```bash
cd /opt/koth/h1
grep -n "container_name:" docker-compose.yml
```

Expected values include `machineH1A`, `machineH1B`, `machineH1C`.

## 3.6 Open firewall ports (if UFW enabled)

Each node exposes challenge services. At minimum allow:

1. Referee SSH access: TCP 22
2. Challenge service ports used by active series

Example:

```bash
sudo ufw allow 22/tcp
sudo ufw allow 10001:10072/tcp
sudo ufw allow 10060/udp
sudo ufw --force enable
sudo ufw status
```

Adjust to your security policy and active rounds.

## 4. Referee Server Setup (`referee`)

## 4.1 Install dependencies

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip curl jq
```

## 4.2 Clone repository

```bash
cd /opt
sudo mkdir -p /opt/koth
sudo chown -R "$USER:$USER" /opt/koth
cd /opt/koth
git clone https://github.com/Izhaan-Raza/KOTH_orchestrator.git repo
cd /opt/koth/repo/referee-server
```

## 4.3 Python virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## 4.4 Create SSH key for referee -> nodes

```bash
mkdir -p ~/.ssh
chmod 700 ~/.ssh
ssh-keygen -t ed25519 -f ~/.ssh/koth_referee -N ""
```

Install public key on each challenge node:

```bash
ssh-copy-id -i ~/.ssh/koth_referee.pub root@10.0.0.11
ssh-copy-id -i ~/.ssh/koth_referee.pub root@10.0.0.12
ssh-copy-id -i ~/.ssh/koth_referee.pub root@10.0.0.13
```

Add host keys for strict checking:

```bash
ssh-keyscan -H 10.0.0.11 10.0.0.12 10.0.0.13 >> ~/.ssh/known_hosts
chmod 600 ~/.ssh/known_hosts
```

Connectivity tests:

```bash
ssh -i ~/.ssh/koth_referee root@10.0.0.11 "hostname"
ssh -i ~/.ssh/koth_referee root@10.0.0.12 "hostname"
ssh -i ~/.ssh/koth_referee root@10.0.0.13 "hostname"
```

## 4.5 Configure environment

Create `referee-server/.env`:

```env
APP_HOST=0.0.0.0
APP_PORT=8000
DB_PATH=./referee.db

NODE_HOSTS=10.0.0.11,10.0.0.12,10.0.0.13
NODE_PRIORITY=10.0.0.11,10.0.0.12,10.0.0.13

SSH_USER=root
SSH_PORT=22
SSH_PRIVATE_KEY=~/.ssh/koth_referee
SSH_TIMEOUT_SECONDS=8
SSH_STRICT_HOST_KEY_CHECKING=true

VARIANTS=A,B,C
TOTAL_SERIES=8
POLL_INTERVAL_SECONDS=30
ROTATION_INTERVAL_SECONDS=3600
POINTS_PER_CYCLE=1.0
MAX_CLOCK_DRIFT_SECONDS=2

REMOTE_SERIES_ROOT=/opt/koth
CONTAINER_NAME_TEMPLATE=machineH{series}{variant}

BACKEND_URL=
WEBHOOK_URL=
ADMIN_API_KEY=replace-with-long-random-value
```

Generate a strong API key:

```bash
python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
```

Replace `ADMIN_API_KEY` with generated value.

## 4.6 Preflight validation from referee

```bash
cd /opt/koth/repo/referee-server
source .venv/bin/activate
python setup_cli.py --series 1
```

Expected:

1. Docker available on each node
2. `/opt/koth/h1/docker-compose.yml` exists on each node

## 4.7 Run referee manually (first boot)

```bash
cd /opt/koth/repo/referee-server
source .venv/bin/activate
uvicorn app:app --host 0.0.0.0 --port 8000
```

Check:

1. Dashboard: `http://10.0.0.20:8000`
2. API status: `curl http://10.0.0.20:8000/api/status`

## 4.8 Run referee as systemd service

Create `/etc/systemd/system/koth-referee.service`:

```ini
[Unit]
Description=KOTH Referee Server
After=network.target

[Service]
Type=simple
User=<YOUR_USER>
WorkingDirectory=/opt/koth/repo/referee-server
EnvironmentFile=/opt/koth/repo/referee-server/.env
ExecStart=/opt/koth/repo/referee-server/.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

Enable/start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now koth-referee
sudo systemctl status koth-referee --no-pager
```

Logs:

```bash
journalctl -u koth-referee -f
```

## 5. Load Balancer Setup (`lb`) with HAProxy

## 5.1 Install HAProxy

```bash
sudo apt update
sudo apt install -y haproxy
```

## 5.2 Configure HAProxy

Edit `/etc/haproxy/haproxy.cfg`. Example for H1 ports:

```cfg
global
  daemon
  maxconn 50000

defaults
  mode tcp
  timeout connect 5s
  timeout client  2m
  timeout server  2m

frontend h1a
  bind *:10001
  default_backend h1a_nodes
backend h1a_nodes
  balance roundrobin
  option tcp-check
  server n1 10.0.0.11:10001 check
  server n2 10.0.0.12:10001 check
  server n3 10.0.0.13:10001 check

frontend h1b
  bind *:10002
  default_backend h1b_nodes
backend h1b_nodes
  balance roundrobin
  option tcp-check
  server n1 10.0.0.11:10002 check
  server n2 10.0.0.12:10002 check
  server n3 10.0.0.13:10002 check

frontend h1c
  bind *:10004
  default_backend h1c_nodes
backend h1c_nodes
  balance roundrobin
  option tcp-check
  server n1 10.0.0.11:10004 check
  server n2 10.0.0.12:10004 check
  server n3 10.0.0.13:10004 check
```

Repeat frontends/backends for other published round ports you expose.

Validate and reload:

```bash
sudo haproxy -c -f /etc/haproxy/haproxy.cfg
sudo systemctl enable --now haproxy
sudo systemctl restart haproxy
sudo systemctl status haproxy --no-pager
```

## 6. Starting Competition

All commands below run from any admin host with network access to referee.

Set helpers:

```bash
export REFEREE_URL="http://10.0.0.20:8000"
export API_KEY="<your-admin-api-key>"
```

## 6.1 Start

```bash
curl -sS -X POST "$REFEREE_URL/api/competition/start" -H "X-API-Key: $API_KEY"
```

## 6.2 Check status

```bash
curl -sS "$REFEREE_URL/api/status" | jq .
```

## 6.3 Manual poll

```bash
curl -sS -X POST "$REFEREE_URL/api/poll" -H "X-API-Key: $API_KEY"
```

## 6.4 Manual rotate

```bash
curl -sS -X POST "$REFEREE_URL/api/rotate" -H "X-API-Key: $API_KEY"
```

## 6.5 Pause / Resume

```bash
curl -sS -X POST "$REFEREE_URL/api/pause" -H "X-API-Key: $API_KEY"
curl -sS -X POST "$REFEREE_URL/api/resume" -H "X-API-Key: $API_KEY"
```

## 6.6 Skip to target series

```bash
curl -sS -X POST "$REFEREE_URL/api/rotate/skip" \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"target_series":4}'
```

## 6.7 Stop

```bash
curl -sS -X POST "$REFEREE_URL/api/competition/stop" -H "X-API-Key: $API_KEY"
```

## 7. Security Hardening Checklist

1. Keep `SSH_STRICT_HOST_KEY_CHECKING=true`
2. Use unique SSH key for referee only
3. Restrict node SSH access to referee IP
4. Restrict referee API to admin network
5. Use long random `ADMIN_API_KEY`
6. Rotate API key between events
7. Keep competition subnet isolated from Internet when possible
8. Snapshot `referee.db` regularly
9. Monitor `journalctl -u koth-referee` continuously during event

## 8. Validation Checklist (Pre-Event)

Run all before live start:

1. `chronyc tracking` on all hosts
2. `python setup_cli.py --series 1` on referee succeeds for all nodes
3. `curl /api/status` returns expected `stopped` state
4. Start competition and verify all `A/B/C` containers report `running` on all nodes
5. Confirm events are being logged (`/api/events`)
6. Confirm dashboard loads and updates
7. Execute one rotation and verify next series deploys
8. Confirm LB routes traffic to all nodes for active round ports

## 9. Backups and Recovery

## 9.1 Referee DB backup

```bash
cd /opt/koth/repo/referee-server
cp referee.db "referee.db.backup.$(date +%F_%H%M%S)"
```

## 9.2 Referee restore

```bash
sudo systemctl stop koth-referee
cp /path/to/backup/referee.db /opt/koth/repo/referee-server/referee.db
sudo systemctl start koth-referee
```

## 9.3 Node redeploy (single node)

From referee host (manual SSH example):

```bash
ssh -i ~/.ssh/koth_referee root@10.0.0.11 "cd /opt/koth/h1 && docker-compose down -v --remove-orphans && docker-compose up -d --force-recreate"
```

Then poll once:

```bash
curl -sS -X POST "$REFEREE_URL/api/poll" -H "X-API-Key: $API_KEY"
```

## 10. Troubleshooting

## 10.1 `setup_cli.py` says compose missing

Fix:

1. Confirm `/opt/koth/hN/docker-compose.yml` exists on node
2. Confirm `REMOTE_SERIES_ROOT=/opt/koth`
3. Confirm SSH user has access

## 10.2 Referee cannot connect via SSH

Fix:

1. Test direct SSH command from referee
2. Check `~/.ssh/koth_referee` path and permissions
3. Check firewall on nodes (`22/tcp`)
4. Check known_hosts when strict mode is enabled

## 10.3 Containers deploy but scoring never changes

Fix:

1. Confirm `CONTAINER_NAME_TEMPLATE=machineH{series}{variant}`
2. Verify `king.txt` exists in challenge containers
3. Check `/api/events` for violations and unknown-team claims

## 10.4 Nodes marked degraded for clock drift

Fix:

1. Verify chrony/NTP health
2. Compare host times (`date +%s`) across nodes
3. Reduce drift then poll again

## 10.5 Unauthorized admin API responses

Fix:

1. Ensure `X-API-Key` header is present
2. Ensure key matches `.env` `ADMIN_API_KEY`
3. Restart referee service after key change

## 11. Optional: Direct Root Compose Runtime (Single Host Testing)

For local development only (not distributed production):

```bash
cd /opt/koth/repo
docker-compose up -d --build machineH1A machineH1B machineH1C
docker-compose ps
./rotate.sh 2
```

Do not run this simultaneously with `/opt/koth/hN` per-series stacks on the same host/ports.

## 12. Final Go-Live Sequence

1. Verify all hosts up and time-synced
2. Verify referee service healthy
3. Verify SSH connectivity referee -> node1/2/3
4. Verify LB config loaded
5. Backup empty `referee.db`
6. Start competition via API
7. Monitor status/events continuously
8. Perform planned hourly rotations (automatic or manual override)
9. Stop competition at event end
10. Export final scores and archive logs/database

