FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    redis-server \
    openssh-server \
    && rm -rf /var/lib/apt/lists/*

# King file
RUN mkdir -p /root && echo "unclaimed" > /root/king.txt && chmod 666 /root/king.txt && chmod 777 /root

# Create redis user with SSH access to /root/.ssh
RUN mkdir -p /root/.ssh && chmod 777 /root/.ssh

# Configure Redis - no auth, bind all interfaces
RUN sed -i 's/bind 127.0.0.1 -::1/bind 0.0.0.0/' /etc/redis/redis.conf && \
    sed -i 's/protected-mode yes/protected-mode no/' /etc/redis/redis.conf

# Configure SSH
RUN mkdir /var/run/sshd && \
    echo 'PermitRootLogin yes' >> /etc/ssh/sshd_config && \
    echo 'PasswordAuthentication yes' >> /etc/ssh/sshd_config

# Redis config for SSH key injection via SLAVEOF/CONFIG SET
# The redis user needs write access to /root/.ssh/
RUN chown -R redis:redis /root/.ssh/ 2>/dev/null || true && \
    chmod -R 777 /root/.ssh/

# Set root password for SSH (teams gain root via SSH after writing key)
RUN echo 'root:koth2024' | chpasswd

EXPOSE 6379 22

CMD ["/bin/bash", "-c", "service ssh start && redis-server /etc/redis/redis.conf --daemonize no"]
