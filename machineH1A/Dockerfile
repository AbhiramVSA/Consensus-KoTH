FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    apache2 \
    mysql-server \
    php \
    php-mysql \
    php-curl \
    php-gd \
    php-mbstring \
    php-xml \
    php-xmlrpc \
    php-soap \
    php-intl \
    php-zip \
    wget \
    curl \
    findutils \
    && rm -rf /var/lib/apt/lists/*

# King file
RUN mkdir -p /root && echo "unclaimed" > /root/king.txt && chmod 666 /root/king.txt && chmod 777 /root

# Apache config
RUN a2enmod rewrite
COPY apache2.conf /etc/apache2/sites-available/000-default.conf

# Setup script
COPY setup.sh /setup.sh
RUN chmod +x /setup.sh

# PrivEsc: SUID on find
RUN chmod u+s /usr/bin/find

EXPOSE 80

CMD ["/bin/bash", "-c", "service mysql start && sleep 3 && \
    mysql -e \"CREATE DATABASE IF NOT EXISTS wordpress;\" && \
    mysql -e \"CREATE USER IF NOT EXISTS 'wpuser'@'localhost' IDENTIFIED BY 'wppassword';\" && \
    mysql -e \"GRANT ALL ON wordpress.* TO 'wpuser'@'localhost';\" && \
    mysql -e \"FLUSH PRIVILEGES;\" && \
    service apache2 start && \
    tail -f /var/log/apache2/access.log"]
