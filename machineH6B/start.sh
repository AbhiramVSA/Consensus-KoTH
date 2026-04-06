#!/bin/bash
# Start MongoDB without auth
mongod --bind_ip 0.0.0.0 --port 27017 --logpath /var/log/mongodb/mongod.log --fork 2>/dev/null || \
mongod --bind_ip_all --port 27017 --logpath /var/log/mongodb/mongod.log --fork 2>/dev/null || true

sleep 5
# Seed the database
mongo kothdb /seed.js 2>/dev/null || mongosh kothdb /seed.js 2>/dev/null || true

echo "[H6B] MongoDB running on :27017 (no auth)"
echo "[H6B] PrivEsc: mongouser is in docker group"

tail -f /var/log/mongodb/mongod.log 2>/dev/null || sleep infinity
