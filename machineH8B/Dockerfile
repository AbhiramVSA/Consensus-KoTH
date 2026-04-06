FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-flask \
    && rm -rf /var/lib/apt/lists/*

# King file
RUN mkdir -p /root && echo "unclaimed" > /root/king.txt && chmod 666 /root/king.txt && chmod 777 /root

# Create flask user to run the app
RUN useradd -m -s /bin/bash flaskuser && echo 'flaskuser:flask123' | chpasswd

# PrivEsc: /etc/sudoers is world-writable
RUN chmod 666 /etc/sudoers

# Deploy Flask app with SSTI vulnerability
COPY app/ /opt/flask-app/
RUN pip3 install flask jinja2 2>/dev/null || true
RUN chown -R flaskuser:flaskuser /opt/flask-app/

EXPOSE 5000

CMD ["/bin/bash", "-c", \
    "su -s /bin/bash flaskuser -c 'python3 /opt/flask-app/app.py'"]
