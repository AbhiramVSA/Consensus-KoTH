FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    wget \
    curl \
    adduser \
    libfontconfig1 \
    python3 \
    && rm -rf /var/lib/apt/lists/*

# King file
RUN mkdir -p /root && echo "unclaimed" > /root/king.txt && chmod 666 /root/king.txt && chmod 777 /root

# Install Grafana 8.3.0 (vulnerable to CVE-2021-43798 path traversal)
RUN wget -q https://dl.grafana.com/oss/release/grafana_8.3.0_amd64.deb -O /tmp/grafana.deb && \
    dpkg -i /tmp/grafana.deb && rm /tmp/grafana.deb || \
    (echo "Grafana download failed, using stub" && mkdir -p /usr/share/grafana)

# Create grafana user to run the service
RUN useradd -m -s /bin/bash grafana 2>/dev/null || true

# PrivEsc: /etc/shadow is writable by all (insecure permissions)
RUN chmod 666 /etc/shadow

# Set a known root password so teams can switch to root once they crack/write shadow
RUN echo 'root:$6$saltsalt$4lBJRE8Y6uLRFmFhOBMb/yBNvv.g.MCYQhL8mKDi853hABbq0EvZ.K3TSTt6sGbmRjW1.q13.KBdqJLWTZXa0:19000:0:99999:7:::' > /tmp/root_shadow_line.txt

# Grafana stub if real install fails
COPY grafana-stub.py /opt/grafana-stub.py
COPY start.sh /start.sh
RUN chmod +x /start.sh

EXPOSE 3000

CMD ["/start.sh"]
