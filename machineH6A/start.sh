#!/bin/bash
# Start rpcbind and NFS
rpcbind &
sleep 1
exportfs -a 2>/dev/null || true

# Start distccd - listen on all interfaces, no whitelist (vulnerable)
distccd --daemon \
    --allow 0.0.0.0/0 \
    --listen 0.0.0.0 \
    --port 3632 \
    --log-stderr \
    --verbose \
    --no-detach &

echo "[H6A] distccd running on :3632 (CVE-2004-2687)"
echo "[H6A] NFS share at /srv/nfs/share with no_root_squash"

wait
