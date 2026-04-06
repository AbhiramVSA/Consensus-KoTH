#!/bin/bash
# Try real ES, fall back to stub
if [ -d /usr/share/elasticsearch/bin ]; then
    chown -R elasticsearch:elasticsearch /usr/share/elasticsearch /var/lib/elasticsearch /var/log/elasticsearch 2>/dev/null
    su -s /bin/bash elasticsearch -c "/usr/share/elasticsearch/bin/elasticsearch -d" 2>/dev/null
    sleep 10
    tail -f /var/log/elasticsearch/elasticsearch.log 2>/dev/null || sleep infinity
else
    echo "[H5B] Using ES stub (Dynamic Scripting RCE simulation)"
    python3 /opt/es-stub.py
fi
