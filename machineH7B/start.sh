#!/bin/bash
# Start real Grafana if installed, else stub
if [ -f /usr/sbin/grafana-server ]; then
    /usr/sbin/grafana-server \
        --config=/etc/grafana/grafana.ini \
        --pidfile=/var/run/grafana/grafana-server.pid \
        --packaging=deb \
        cfg:default.paths.logs=/var/log/grafana \
        cfg:default.paths.data=/var/lib/grafana \
        cfg:default.paths.plugins=/var/lib/grafana/plugins \
        cfg:default.paths.provisioning=/etc/grafana/provisioning &
    tail -f /var/log/grafana/grafana.log
else
    echo "[H7B] Using stub Grafana (CVE-2021-43798)"
    python3 /opt/grafana-stub.py
fi
