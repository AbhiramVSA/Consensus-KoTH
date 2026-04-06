FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    mongodb \
    docker.io \
    curl \
    && rm -rf /var/lib/apt/lists/* 2>/dev/null || true

# Try alternative MongoDB install
RUN apt-get update && (apt-get install -y mongodb || \
    (wget -qO - https://www.mongodb.org/static/pgp/server-4.4.asc | apt-key add - && \
     echo "deb [ arch=amd64,arm64 ] https://repo.mongodb.org/apt/ubuntu focal/mongodb-org/4.4 multiverse" > /etc/apt/sources.list.d/mongodb.list && \
     apt-get update && apt-get install -y mongodb-org)) 2>/dev/null || true

# King file
RUN mkdir -p /root && echo "unclaimed" > /root/king.txt && chmod 666 /root/king.txt && chmod 777 /root

# Create low-priv user and add to docker group (PrivEsc via docker.sock)
RUN useradd -m -s /bin/bash mongouser && echo 'mongouser:mongo123' | chpasswd
RUN groupadd docker 2>/dev/null || true
RUN usermod -aG docker mongouser

# Expose docker.sock (assumes it's mounted at runtime or simulate access)
# The real docker.sock breakout works when /var/run/docker.sock is mounted into container

# Seed MongoDB with password hashes to crack
COPY seed.js /seed.js

COPY start.sh /start.sh
RUN chmod +x /start.sh

EXPOSE 27017

CMD ["/start.sh"]
