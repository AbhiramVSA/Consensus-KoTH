#!/bin/bash
# Start SSH
service ssh start

# Start SNMPd with public community
snmpd -C -c /etc/snmp/snmpd.conf -Lo -f &

# Start a root tmux session (detached, persistently running)
# Attackers who gain shell access can attach to this session
tmux new-session -d -s root_session -x 220 -y 50
tmux send-keys -t root_session "echo 'Root tmux session active. King file: /root/king.txt'" Enter
tmux send-keys -t root_session "while true; do echo '[root@koth] # '; sleep 30; done" Enter

echo "[H7A] SNMP running on :161/udp (public community)"
echo "[H7A] Root tmux session active: tmux attach -t root_session"
echo "[H7A] SSH running on :22"

# Keep alive
wait
