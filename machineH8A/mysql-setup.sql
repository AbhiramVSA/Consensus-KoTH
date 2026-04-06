-- MySQL H8A setup: root with no password + UDF support
-- Root with no password
ALTER USER 'root'@'localhost' IDENTIFIED BY '';
UPDATE mysql.user SET authentication_string='' WHERE User='root';
FLUSH PRIVILEGES;

-- Allow FILE privilege
GRANT FILE ON *.* TO 'root'@'localhost';

-- Set plugin dir (needed for UDF)
SET GLOBAL plugin_dir='/usr/lib/mysql/plugin/';

-- Insecure: allow writing to plugin dir from SQL
SET GLOBAL secure_file_priv='';

-- Create a database
CREATE DATABASE IF NOT EXISTS kothdb;
USE kothdb;

CREATE TABLE IF NOT EXISTS users (
    id INT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(50),
    password VARCHAR(255),
    role VARCHAR(20)
);

INSERT INTO users VALUES (1, 'admin', 'admin123', 'admin'),
                         (2, 'user1', 'pass1', 'user');

-- UDF installation (attackers can do this after gaining MySQL root):
-- SELECT unhex('...') INTO DUMPFILE '/usr/lib/mysql/plugin/udf_sys.so';
-- CREATE FUNCTION sys_exec RETURNS INT SONAME 'udf_sys.so';
-- SELECT sys_exec('cp /bin/bash /tmp/bash; chmod +s /tmp/bash');

FLUSH PRIVILEGES;
