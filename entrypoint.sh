#!/bin/bash
set -e

# If custom arguments are provided (e.g., 'biliup login' or 'bash'), execute them directly
if [ "$#" -gt 0 ]; then
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
