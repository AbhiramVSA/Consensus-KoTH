#!/bin/bash
set -euo pipefail

# Start the NFS stack explicitly so the no_root_squash path is actually reachable.
mkdir -p /run/rpcbind /var/lib/nfs/rpc_pipefs /proc/fs/nfsd
mountpoint -q /proc/fs/nfsd || mount -t nfsd nfsd /proc/fs/nfsd
mountpoint -q /var/lib/nfs/rpc_pipefs || mount -t rpc_pipefs sunrpc /var/lib/nfs/rpc_pipefs

rpcbind -w
exportfs -ra
rpc.mountd --foreground --no-udp --manage-gids &
rpc.nfsd --no-udp --nfs-version 4 8

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

exec tail -f /dev/null
