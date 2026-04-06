FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    apache2 \
    php \
    php-mysql \
    php-gd \
    php-xml \
    mysql-server \
    cron \
    tar \
    wget \
    && rm -rf /var/lib/apt/lists/*

# King file
RUN mkdir -p /root && echo "unclaimed" > /root/king.txt && chmod 666 /root/king.txt && chmod 777 /root

# Install Drupal 7.57 (vulnerable to Drupalgeddon2 CVE-2018-7600)
RUN wget -q https://ftp.drupal.org/files/projects/drupal-7.57.tar.gz -P /tmp/ && \
    tar -xzf /tmp/drupal-7.57.tar.gz -C /var/www/html/ --strip-components=1 && \
    rm /tmp/drupal-7.57.tar.gz

# Configure Drupal DB
RUN service mysql start && sleep 3 && \
    mysql -e "CREATE DATABASE drupal;" && \
    mysql -e "CREATE USER 'drupal'@'localhost' IDENTIFIED BY 'drupal123';" && \
    mysql -e "GRANT ALL ON drupal.* TO 'drupal'@'localhost';" && \
    mysql -e "FLUSH PRIVILEGES;"

# Copy Drupal settings
COPY settings.php /var/www/html/sites/default/settings.php
RUN mkdir -p /var/www/html/sites/default/files && \
    chown -R www-data:www-data /var/www/html/ && \
    chmod -R 755 /var/www/html/

# Apache config
COPY apache2.conf /etc/apache2/sites-available/000-default.conf
RUN a2enmod rewrite php8.1

# PrivEsc: Writable directory where root runs `tar *` via cron
RUN mkdir -p /opt/backups && chmod 777 /opt/backups
RUN echo "* * * * * root cd /opt/backups && tar czf /tmp/backup.tar.gz *" >> /etc/crontab

EXPOSE 80

CMD ["/bin/bash", "-c", \
    "service mysql start && sleep 3 && \
     service cron start && \
     service apache2 start && \
     tail -f /var/log/apache2/error.log"]
