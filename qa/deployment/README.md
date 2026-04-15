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
