# KOTH Orchestrator: Reimplement Hours 5-8 Following Series H1 Pattern

## Role & Context

You are a senior DevOps engineer building vulnerable machines for a King of the Hill (KOTH) cybersecurity competition. This is a LIVE EVENT where 3 machines rotate every hour for 8 hours. Players exploit an initial RCE vector, then escalate privileges to root to write their team name to `/root/king.txt`. The team holding the king file at each scoring interval gets points.

**THIS IS A COMPETITIVE EVENT. Any circumstance where a player can bypass a vulnerability, wipe an exploitation path, or write king.txt without proper privilege escalation is a CATASTROPHIC error that ruins the competition.**

## Your Task

Restructure and fix Hours 5, 6, 7, and 8 to match the Series H1 reference implementation exactly. Currently H5-H8 machines exist as flat directories at the repo root with broken permissions and no per-series orchestration. You must fix this.

## Reference Implementation: Series H1

The canonical pattern lives in `Series H1/`. Read these files first and treat them as ground truth:

```
Series H1/
├── docker-compose.yml          # Per-series compose — isolated network, tagged images
├── orchestrator_h1.sh          # Per-series orchestrator — build/start/stop/status
├── machineH1A/
│   ├── Dockerfile              # ubuntu:22.04 base, vulns baked at build time
│   ├── setup.sh                # Build-time-only setup (NOT runtime)
│   └── apache2.conf
├── machineH1B/
│   ├── Dockerfile
│   └── readme.md
└── machineH1C/
    ├── Dockerfile
    ├── index.php
    └── readme.md
```

### H1 Patterns You MUST Replicate

**1. King file + /root directory permissions (MANDATORY — this is the #1 rule):**
```dockerfile
RUN mkdir -p /root && echo "unclaimed" > /root/king.txt && chmod 644 /root/king.txt && chmod 700 /root
```
- `king.txt` is `644` (root-writable, world-readable for SLA bot)
- `/root/` is `700` (root-only access)
- **NEVER use `chmod 666` or `chmod 777`** — this lets any user write king.txt without privesc, destroying the game

**2. Per-series docker-compose.yml:**
```yaml
version: '3.8'
services:
  machine_h{N}a:
    build:
      context: ./machineH{N}A
      dockerfile: Dockerfile
    image: koth-h{N}a:prod
    container_name: koth_h{N}a
    restart: unless-stopped
    ports:
      - "{PORT}:{INTERNAL_PORT}"
    networks:
      - h{N}_network
  # ... machines B, C ...
networks:
  h{N}_network:
    driver: bridge
```
- Each series gets its own isolated bridge network
- Images tagged as `koth-h{N}{x}:prod`
- Container names as `koth_h{N}{x}`
- Build context points to subdirectory

**3. Per-series orchestrator script (`orchestrator_h{N}.sh`):**
```bash
#!/bin/bash
COMPOSE_FILE="docker-compose.yml"
case "$1" in
    cache|build)
        docker-compose -f $COMPOSE_FILE build --pull --no-cache
        ;;
    start)
        docker-compose -f $COMPOSE_FILE up -d --force-recreate || exit 1
        ;;
    stop)
        docker-compose -f $COMPOSE_FILE down -v || exit 1    # -v is CRITICAL
        ;;
    status)
        docker-compose -f $COMPOSE_FILE ps
        ;;
esac
```
- `down -v` MUST include `-v` to wipe volumes between rounds
- `build --pull --no-cache` ensures setup scripts run fresh

**4. Vulnerability baking (anti-wipe/anti-fuzzing):**
- ALL vulnerabilities are embedded at Docker BUILD TIME, not runtime
- Vulnerable configs go in Dockerfile `RUN` commands or `COPY` directives
- Vulnerable service flags are hardcoded in `CMD`, not in editable config files
- SUID binaries, world-writable files, and vulnerable code are set in image layers
- If a player deletes a vulnerability at runtime, container restart restores it from the image

**5. Dockerfile CMD pattern:**
- Services start via CMD, not ENTRYPOINT
- Use `tail -f /dev/null` or foreground process to keep container alive
- Start dependent services (mysql, ssh) via `service X start` in CMD

## Port Matrix (AUTHORITATIVE — do not deviate)

| Machine | External Port(s) | Internal Port(s) | Initial Vector | PrivEsc Vector |
|---------|-----------------|-------------------|----------------|----------------|
| H5A | 10040 | 10000 | Webmin RCE CVE-2019-15107 | Drops directly to root |
| H5B | 10041 | 9200 | ElasticSearch Dynamic Scripting RCE | `www-data` can write `/etc/passwd` |
| H5C | 10042 | 8080 | Apache Struts CVE-2017-5638 | `sudo` allows `LD_PRELOAD` |
| H6A | 10050, 10051 | 3632, 2049 | distcc CVE-2004-2687 | NFS `no_root_squash` — write SUID binary |
| H6B | 10052 | 27017 | MongoDB No Auth (crack hashes) | `mongouser` in `docker` group |
| H6C | 10053 | 443 | Heartbleed CVE-2014-0160 (session leak) | `sudo systemctl` -> spawn shell |
| H7A | 10060 (udp), 10061 | 161/udp, 22 | SNMP Public community leaks processes | Hijack active root `tmux` session |
| H7B | 10062 | 3000 | Grafana Path Traversal CVE-2021-43798 | `/etc/shadow` is world-writable |
| H7C | 10063 | 873 | RSync anonymous write | PATH hijacking in root cron (`ls`) |
| H8A | 10070 | 80 | PHPMyAdmin root:blank | MySQL UDF for command execution |
| H8B | 10071 | 5000 | Flask/Jinja2 SSTI | `/etc/sudoers` is world-writable |
| H8C | 10072 | 8000 | Laravel Debug CVE-2021-3129 | SUID `/bin/bash_suid` (`bash_suid -p`) |

### Special docker-compose requirements from port matrix:
- H6A: `cap_add: [SYS_ADMIN, NET_ADMIN]`
- H6B: `volumes: ["/var/run/docker.sock:/var/run/docker.sock"]`
- H6C: `cap_add: [NET_ADMIN]`

## Execution Steps

### Phase 1: Create Series Directories
For each hour N in {5, 6, 7, 8}:
1. Create `Series H{N}/` directory
2. Move `machineH{N}A/`, `machineH{N}B/`, `machineH{N}C/` into `Series H{N}/`
3. Create `Series H{N}/orchestrator_h{N}.sh` following the H1 pattern exactly
4. Create `Series H{N}/docker-compose.yml` following the H1 pattern with correct ports from the matrix

### Phase 2: Fix King File Permissions (CRITICAL)
In EVERY Dockerfile across H5A through H8C, find and replace:
```dockerfile
# WRONG (current state — game-breaking):
RUN mkdir -p /root && echo "unclaimed" > /root/king.txt && chmod 666 /root/king.txt && chmod 777 /root

# CORRECT (matches H1 reference):
RUN mkdir -p /root && echo "unclaimed" > /root/king.txt && chmod 644 /root/king.txt && chmod 700 /root
```

### Phase 3: Update Master docker-compose.yml
Update the root `docker-compose.yml` build paths for H5-H8 from:
```yaml
machineH5A:
    build: ./machineH5A
```
to:
```yaml
machineH5A:
    build: ./Series H5/machineH5A
```

### Phase 4: Verify Anti-Wipe Integrity
For EACH machine, verify that the vulnerability CANNOT be wiped by a player at runtime:

**Check each machine against this checklist:**
- [ ] Vulnerable code/config is COPYed or created in a RUN layer (not mounted as a volume)
- [ ] Vulnerable service flags are in CMD or Dockerfile, not in an editable config file on disk
- [ ] SUID binaries are set in RUN layers
- [ ] World-writable files needed for privesc (e.g., `/etc/shadow`, `/etc/sudoers`) are set in RUN layers
- [ ] No volumes are defined that would let players persist changes across container restarts
- [ ] The initial RCE vector cannot be "patched" by the player from their access level

**Specific anti-wipe concerns to verify:**
- H5A: Webmin password reset feature (`passwd_mode=2`) must be in image, not editable config
- H5B: ElasticSearch dynamic scripting enabled in image, not runtime config
- H5C: Struts stub must be in image; `sudo` config for LD_PRELOAD must be in `/etc/sudoers` (set at build, not runtime)
- H6A: distcc config and NFS `no_root_squash` exports must be in image layers
- H6B: MongoDB runs without auth via CMD flags, not config file
- H6C: Heartbleed stub in image; `sudo systemctl` configured in sudoers at build time
- H7A: SNMP community string in image; tmux session auto-starts in CMD
- H7B: Grafana stub in image; `/etc/shadow` chmod 666 in RUN layer
- H7C: rsyncd.conf in image with anonymous module; root cron with PATH-vulnerable `ls` call in RUN layer
- H8A: PHPMyAdmin with root:blank in image; MySQL UDF `.so` compiled at build time
- H8B: Flask SSTI app in image; `/etc/sudoers` chmod 666 in RUN layer
- H8C: Laravel debug stub in image; `/bin/bash_suid` SUID copy created in RUN layer

### Phase 5: Update rotate.sh
Verify that `rotate.sh` still works with the new directory structure. The master `docker-compose.yml` at the repo root must reference the correct build paths.

## Hard Rules (Violations = Broken Competition)

1. **King file MUST be immutable to non-root users.** `chmod 644 /root/king.txt && chmod 700 /root`. No exceptions.
2. **Port matrix is authoritative.** External ports, internal ports, and vulnerability vectors must match exactly.
3. **Every hour has exactly one `docker-compose.yml` and one `orchestrator_h{N}.sh`** inside its `Series H{N}/` directory.
4. **Vulnerabilities are baked into Docker image layers.** Players must NOT be able to permanently patch a vulnerability. Container restart must restore the exploitable state.
5. **`/root/` directory is `700`.** This forces players to actually escalate privileges rather than directly accessing the king file.
6. **No volumes for application data.** Volumes allow persistence across restarts, which lets players patch vulns permanently. The only exception is H6B's docker.sock mount (required for the PrivEsc vector).
7. **`down -v` in every orchestrator stop command.** This ensures clean state between rounds.

## Validation Checklist (Run After All Changes)

After completing all phases, verify:
```bash
# 1. Directory structure exists
ls -la "Series H5/" "Series H6/" "Series H7/" "Series H8/"

# 2. Each series has its own compose + orchestrator
for N in 5 6 7 8; do
  test -f "Series H${N}/docker-compose.yml" && echo "H${N} compose: OK"
  test -f "Series H${N}/orchestrator_h${N}.sh" && echo "H${N} orchestrator: OK"
done

# 3. King file permissions are correct in ALL Dockerfiles
grep -r "chmod 666.*king\|chmod 777.*root" "Series H5/" "Series H6/" "Series H7/" "Series H8/"
# ^^^ This MUST return ZERO results. Any match = broken game.

# 4. King file permissions ARE present correctly
grep -r "chmod 644.*king.txt.*chmod 700.*/root" "Series H5/" "Series H6/" "Series H7/" "Series H8/"
# ^^^ This MUST return one result per Dockerfile (12 total).

# 5. Master docker-compose.yml paths are updated
grep "Series H[5-8]" docker-compose.yml
# ^^^ Should show build paths pointing to Series directories.

# 6. Orchestrator scripts are executable
for N in 5 6 7 8; do
  test -x "Series H${N}/orchestrator_h${N}.sh" && echo "H${N} orchestrator executable: OK"
done
```

## Output Format

For each hour (H5, H6, H7, H8), produce:
1. The `Series H{N}/docker-compose.yml` file
2. The `Series H{N}/orchestrator_h{N}.sh` file
3. Updated Dockerfiles with corrected king.txt/root permissions
4. Updated master `docker-compose.yml` with correct build paths
5. A brief summary of anti-wipe verification per machine

Work through one hour at a time. After each hour, run the validation checks for that hour before moving to the next.
