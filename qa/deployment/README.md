# Deployment Validation Scripts

These scripts validate host setup after deployment.

## 1) On each KOTH node (`node1`, `node2`, `node3`)

```bash
bash qa/deployment/validate_koth_node.sh --series-root /opt/KOTH_orchestrator
```

If using home layout:

```bash
bash qa/deployment/validate_koth_node.sh --series-root "$HOME/KOTH_orchestrator"
```

## 2) On referee+LB host

```bash
bash qa/deployment/validate_referee_lb.sh \
  --series-root /opt/KOTH_orchestrator \
  --referee-dir /opt/KOTH_orchestrator/repo/referee-server \
  --api-url http://127.0.0.1:8000
```

If using home layout:

```bash
bash qa/deployment/validate_referee_lb.sh \
  --series-root "$HOME/KOTH_orchestrator" \
  --referee-dir "$HOME/KOTH_orchestrator/repo/referee-server" \
  --api-url http://127.0.0.1:8000
```

Both scripts exit non-zero if validation fails.

## 2.25) Lock down host ingress with UFW

Use the firewall helper to make the deployed network policy reproducible instead of leaving
LB/node exposure as an ad hoc host-side change.

Preview the referee/LB rules:

```bash
bash qa/deployment/configure_koth_ufw.sh --role referee
```

Preview node rules so only the LB host can hit published challenge ports while SSH stays on the
internal management LAN:

```bash
bash qa/deployment/configure_koth_ufw.sh \
  --role node \
  --internal-cidr 192.168.0.0/24 \
  --challenge-source-cidr 192.168.0.12/32
```

Apply after review:

```bash
bash qa/deployment/configure_koth_ufw.sh --role referee --apply
bash qa/deployment/configure_koth_ufw.sh \
  --role node \
  --internal-cidr 192.168.0.0/24 \
  --challenge-source-cidr 192.168.0.12/32 \
  --apply
```

## 2.5) Emulate referee SSH and team creation locally

Use this to prove the referee bootstrap paths without touching live nodes or a live backend:

```bash
python qa/deployment/emulate_referee_paths.py \
  --series 1 \
  --hosts 192.168.0.70,192.168.0.103,192.168.0.106 \
  --teams "Team Alpha,Team Beta"
```

This emulates:

1. the SSH/docker checks from `referee-server/setup_cli.py`
2. local team-roster creation in a temporary SQLite `referee.db`

## 3) Pre-build Docker image cache on challenge nodes

Use this before the first competition start so `docker compose up -d` does not
time out while building images on the nodes:

```bash
bash qa/deployment/prebuild_series_cache.sh \
  --referee-dir /opt/KOTH_orchestrator/repo/referee-server
```

Useful variants:

```bash
# Build only H1 on all nodes
bash qa/deployment/prebuild_series_cache.sh --series 1

# Build H1 and H2 on a single node
bash qa/deployment/prebuild_series_cache.sh --series 1,2 --hosts 192.168.0.70

# Force base-image refresh during build
bash qa/deployment/prebuild_series_cache.sh --pull
```
