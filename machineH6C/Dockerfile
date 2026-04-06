FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# Install vulnerable OpenSSL version (for Heartbleed) - Ubuntu 12.04 era SSL
RUN apt-get update && apt-get install -y \
    openssl \
    libssl-dev \
    apache2 \
    wget \
    sudo \
    systemd \
    python3 \
    && rm -rf /var/lib/apt/lists/*

# King file
RUN mkdir -p /root && echo "unclaimed" > /root/king.txt && chmod 666 /root/king.txt && chmod 777 /root

# Create a low-priv HTTP user
RUN useradd -m -s /bin/bash webuser && echo 'webuser:web123' | chpasswd

# PrivEsc: sudo allows systemctl (can be abused to run arbitrary commands via ExecStart)
RUN echo "webuser ALL=(ALL) NOPASSWD: /bin/systemctl" >> /etc/sudoers

# Use a Heartbleed-simulating stub since we can't easily downgrade OpenSSL
# The stub serves HTTPS and responds to heartbeat requests with leaked memory
COPY heartbleed-stub.py /opt/heartbleed-stub.py
COPY generate-cert.sh /generate-cert.sh
RUN chmod +x /generate-cert.sh && /generate-cert.sh

COPY start.sh /start.sh
RUN chmod +x /start.sh

EXPOSE 443

CMD ["/start.sh"]
