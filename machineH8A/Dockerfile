FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    apache2 \
    mysql-server \
    php \
    php-mysql \
    wget \
    gcc \
    make \
    && rm -rf /var/lib/apt/lists/*

# King file
RUN mkdir -p /root && echo "unclaimed" > /root/king.txt && chmod 666 /root/king.txt && chmod 777 /root

# Install phpMyAdmin (default creds root:blank)
RUN mkdir -p /var/www/html/phpmyadmin
COPY phpmyadmin-config.inc.php /var/www/html/phpmyadmin/config.inc.php
COPY phpmyadmin-stub/ /var/www/html/phpmyadmin/

# MySQL setup - UDF plugin directory, allow FILE operations, root with no password
COPY mysql-setup.sql /tmp/mysql-setup.sql

# Apache config
COPY apache2.conf /etc/apache2/sites-available/000-default.conf
RUN a2enmod rewrite

# PrivEsc: MySQL configured for UDF execution
# Compile a simple UDF helper
COPY lib_mysqludf_sys.c /tmp/lib_mysqludf_sys.c

COPY start.sh /start.sh
RUN chmod +x /start.sh

EXPOSE 80

CMD ["/start.sh"]
