#!/bin/bash
set -e
cd "$(dirname "$0")"
# Pull latest from repo before running
git pull --ff-only origin main 2>/dev/null || echo "Git pull skipped (not critical)"
exec python3 lidarr_queue_maintenance.py
