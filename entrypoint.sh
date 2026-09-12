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
    exec "$@"
fi

echo "========================================="
echo "=== yt2bili Container Starting ==="
echo "=== Time: $(date) ==="
echo "========================================="

echo "Checking for yt-dlp updates..."
pip install --no-cache-dir --upgrade yt-dlp >/dev/null 2>&1 || true

# Run the pipeline once on container start
echo "Running initial pipeline check now..."
python -u /app/src/main.py 2>&1 | tee -a /app/data/pipeline.log

echo ""
echo "========================================="
echo "Initial check completed."
echo "Setting up cron schedule..."

# Set up cron: run every 30 minutes between 7:00 and 01:30
CRON_SCHEDULE="*/30 7-23,0-1 * * *"
echo "${CRON_SCHEDULE} cd /app && python -u /app/src/main.py >> /app/data/pipeline.log 2>&1" > /etc/cron.d/yt2bili
echo "" >> /etc/cron.d/yt2bili
chmod 0644 /etc/cron.d/yt2bili
crontab /etc/cron.d/yt2bili

echo "Cron schedule active: ${CRON_SCHEDULE}"
echo "Container is active in the background. It will wake up every 30 minutes to check for new videos."
echo "You can follow execution logs with: docker compose logs -f yt2bili"
echo "========================================="

# Pass environment variables to cron
env >> /etc/environment

# Start cron in the foreground to keep the container running
cron -f
