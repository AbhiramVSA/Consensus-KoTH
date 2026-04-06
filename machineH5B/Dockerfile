FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    wget \
    curl \
    gnupg \
    apt-transport-https \
    python3 \
    && rm -rf /var/lib/apt/lists/*

# Install Elasticsearch 6.8.x (pre-auth scripting RCE via dynamic scripting)
# Elasticsearch with dynamic scripting enabled allows arbitrary code execution
RUN wget -qO - https://artifacts.elastic.co/GPG-KEY-elasticsearch | apt-key add - 2>/dev/null || true
RUN echo "deb https://artifacts.elastic.co/packages/6.x/apt stable main" > /etc/apt/sources.list.d/elastic-6.x.list
RUN apt-get update && apt-get install -y elasticsearch 2>/dev/null || \
    (echo "ES install failed, using stub" && mkdir -p /usr/share/elasticsearch)

# King file
RUN mkdir -p /root && echo "unclaimed" > /root/king.txt && chmod 666 /root/king.txt && chmod 777 /root

# PrivEsc: www-data can write to /etc/passwd
RUN chown www-data:www-data /etc/passwd && chmod 666 /etc/passwd

# Create elasticsearch user for running the service
RUN useradd -m -s /bin/bash elasticsearch 2>/dev/null || true

# Stub ES server if real install fails
COPY es-stub.py /opt/es-stub.py
COPY start.sh /start.sh
RUN chmod +x /start.sh

EXPOSE 9200

CMD ["/start.sh"]
