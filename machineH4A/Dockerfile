FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

# King file
RUN mkdir -p /root && echo "unclaimed" > /root/king.txt && chmod 666 /root/king.txt && chmod 777 /root

# PrivEsc: Cleartext root password left in /var/backups/shadow.bak
# Root password is 'r00tme' (hashed below is the bcrypt hash)
RUN mkdir -p /var/backups && \
    echo 'root:$6$koth$V5R8Y2bKJ9L1mN3P4qQ0wXsUoIaEzFhGcDtRvWnMeKpBjHuZxCyA7l6d5f4g3h2i1.:19000:0:99999:7:::' \
    > /var/backups/shadow.bak && \
    chmod 644 /var/backups/shadow.bak

# Even clearer hint: a note file nearby
RUN echo "Root password backup: r00tme" > /var/backups/README.txt && \
    chmod 644 /var/backups/README.txt

# Deploy Node.js deserialization app
RUN mkdir -p /app
COPY app/ /app/
WORKDIR /app
RUN npm install

# Create low-priv user to run the app
RUN useradd -m -s /bin/bash nodeuser && echo 'nodeuser:nodepass' | chpasswd
RUN chown -R nodeuser:nodeuser /app

EXPOSE 3000

CMD ["node", "/app/server.js"]
