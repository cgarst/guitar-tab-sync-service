#!/bin/sh
set -eu

# 1. Pull the latest upstream release and install its dependencies.
python3 /app/update.py

# 2. Serve. server.py fails jobs a previous container left half-done, sweeps expired files, and keeps sweeping.
exec python3 /app/server.py
