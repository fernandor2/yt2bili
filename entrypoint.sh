#!/bin/bash
set -e

# Ensure data directory exists
mkdir -p /app/data/downloads /app/data/db

# If /app/cookies.json is an empty directory created accidentally by Docker bind mount, remove it
if [ -d "/app/cookies.json" ]; then
    rmdir /app/cookies.json 2>/dev/null || true
fi

# Sync cookie file locations between /app/cookies.json and /app/data/cookies.json
if [ -f "/app/cookies.json" ] && [ ! -f "/app/data/cookies.json" ]; then
    cp /app/cookies.json /app/data/cookies.json 2>/dev/null || true
fi
if [ -f "/app/data/cookies.json" ] && [ ! -e "/app/cookies.json" ]; then
    ln -sf /app/data/cookies.json /app/cookies.json 2>/dev/null || true
fi

# If custom arguments are provided (e.g. 'test', 'biliup login', 'bash', or custom commands)
if [ "$#" -gt 0 ]; then
    if [ "$1" = "test" ]; then
        shift
        exec python -u /app/src/test_pipeline.py "$@"
    fi
    if [ "$1" = "reset-db" ] || [ "$1" = "clean-db" ] || [ "$1" = "reset" ]; then
        shift
        exec python -u /app/src/reset_db.py "$@"
    fi
    if [ "$1" = "biliup" ] && [ "$2" = "login" ]; then
        echo "========================================="
        echo "=== Bilibili Interactive QR Login ==="
        echo "========================================="
        echo "Please scan the QR code that appears below with your Bilibili mobile app."
        echo "Session cookies will be stored persistently in: /app/data/cookies.json"
        echo "========================================="
        exec biliup -u /app/data/cookies.json login
    fi
    if [ "$1" = "run-once" ]; then
        shift
        exec python -u /app/src/main.py "$@"
    fi
    exec "$@"
fi

echo "========================================="
echo "=== yt2bili Container Starting ==="
echo "=== Time: $(date) ==="
echo "========================================="

echo "Checking for yt-dlp updates..."
pip install --no-cache-dir --upgrade yt-dlp >/dev/null 2>&1 || true

echo "Starting continuous pipeline daemon..."
echo "Check interval: 30 minutes"
echo "Active hours: 07:00 - 02:00"
echo "Live logs streamed directly to docker output."
echo "========================================="

exec python -u /app/src/main.py --loop
