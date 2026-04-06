#!/bin/bash
set -e

# Setup king file
mkdir -p /root
echo "unclaimed" > /root/king.txt
chmod 644 /root/king.txt

# Give www-data access to write king.txt (needed for teams to claim via RCE)
chmod 777 /root
chmod 666 /root/king.txt

# Configure MySQL
service mysql start
sleep 3
mysql -e "CREATE DATABASE wordpress;"
mysql -e "CREATE USER 'wpuser'@'localhost' IDENTIFIED BY 'wppassword';"
mysql -e "GRANT ALL PRIVILEGES ON wordpress.* TO 'wpuser'@'localhost';"
mysql -e "FLUSH PRIVILEGES;"

# Configure Apache
a2enmod rewrite
service apache2 start

# Install WordPress
cd /var/www/html
rm -f index.html
wget -q https://wordpress.org/wordpress-5.7.tar.gz -O /tmp/wp.tar.gz || \
  curl -sL https://wordpress.org/wordpress-5.7.tar.gz -o /tmp/wp.tar.gz
tar -xzf /tmp/wp.tar.gz -C /var/www/html --strip-components=1
rm /tmp/wp.tar.gz

cp wp-config-sample.php wp-config.php
sed -i "s/database_name_here/wordpress/" wp-config.php
sed -i "s/username_here/wpuser/" wp-config.php
sed -i "s/password_here/wppassword/" wp-config.php

chown -R www-data:www-data /var/www/html/

# Install a known-vulnerable plugin (Reflex Gallery 3.1.3 - arbitrary file upload)
mkdir -p /var/www/html/wp-content/plugins/reflex-gallery
cat > /var/www/html/wp-content/plugins/reflex-gallery/reflex-gallery.php << 'PLUGIN'
<?php
/**
 * Plugin Name: Reflex Gallery
 * Version: 3.1.3
 */
// Intentionally vulnerable upload handler (no nonce/auth check)
if (isset($_POST['action']) && $_POST['action'] === 'UploadHandler') {
    $upload_dir = WP_CONTENT_DIR . '/uploads/';
    if (!is_dir($upload_dir)) mkdir($upload_dir, 0755, true);
    $file = $_FILES['file'];
    move_uploaded_file($file['tmp_name'], $upload_dir . basename($file['name']));
    echo json_encode(['status' => 'success', 'file' => $upload_dir . basename($file['name'])]);
    exit;
}
PLUGIN

chown -R www-data:www-data /var/www/html/wp-content/

# Auto-install WordPress via WP-CLI
curl -sO https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar/wp-cli.phar || true
if [ -f wp-cli.phar ]; then
    php wp-cli.phar core install \
        --url=http://localhost \
        --title="KoTH Blog" \
        --admin_user=admin \
        --admin_password=admin123 \
        --admin_email=admin@koth.local \
        --allow-root 2>/dev/null || true
    php wp-cli.phar plugin activate reflex-gallery --allow-root 2>/dev/null || true
fi

# === PRIVILEGE ESCALATION: SUID bit on /usr/bin/find ===
chmod u+s /usr/bin/find

echo "[H1A] Setup complete."
