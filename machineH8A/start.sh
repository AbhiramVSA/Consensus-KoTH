#!/bin/bash
service mysql start
sleep 5
mysql < /tmp/mysql-setup.sql 2>/dev/null || true

# Compile UDF if gcc available
if command -v gcc &>/dev/null && [ -f /tmp/lib_mysqludf_sys.c ]; then
    gcc -shared -fPIC -o /usr/lib/mysql/plugin/udf_sys.so /tmp/lib_mysqludf_sys.c -I/usr/include/mysql 2>/dev/null || true
fi

service apache2 start
echo "[H8A] MySQL running (root: no password)"
echo "[H8A] phpMyAdmin at http://localhost/phpmyadmin"
tail -f /var/log/apache2/access.log
