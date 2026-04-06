FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    snmpd \
    tmux \
    openssh-server \
    sudo \
    && rm -rf /var/lib/apt/lists/*

# King file
RUN mkdir -p /root && echo "unclaimed" > /root/king.txt && chmod 666 /root/king.txt && chmod 777 /root

# Configure SNMPd - public community string, leaks process list
COPY snmpd.conf /etc/snmp/snmpd.conf

# Create a root tmux session that stays alive (hijackable)
# Attacker can attach to the root tmux session if they get a shell on the box
RUN mkdir -p /root && touch /root/.tmux_active

COPY start.sh /start.sh
RUN chmod +x /start.sh

# SSH setup
RUN mkdir /var/run/sshd && \
    echo 'PermitRootLogin yes' >> /etc/ssh/sshd_config && \
    echo 'PasswordAuthentication yes' >> /etc/ssh/sshd_config && \
    echo 'root:koth2024' | chpasswd

EXPOSE 161/udp 22

CMD ["/start.sh"]
