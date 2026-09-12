#!/bin/bash
set -e

echo "=== yt2bili Pipeline Starting ==="
echo "Timezone: $(date)"

# Run the pipeline once on container start, then set up cron
echo "Running initial pipeline check..."
python -u /app/src/main.py 2>&1 | tee -a /app/data/pipeline.log

# Set up cron: run every 30 minutes between 7:00 and 01:30
# Server is on from 7am to 2am, so last run at 01:30
CRON_SCHEDULE="*/30 7-23,0-1 * * *"
echo "${CRON_SCHEDULE} cd /app && python -u /app/src/main.py >> /app/data/pipeline.log 2>&1" > /etc/cron.d/yt2bili
echo "" >> /etc/cron.d/yt2bili
chmod 0644 /etc/cron.d/yt2bili
crontab /etc/cron.d/yt2bili

echo "Cron schedule set: ${CRON_SCHEDULE}"
echo "Starting cron daemon..."

# Pass environment variables to cron
env >> /etc/environment

# Start cron in the foreground
cron -f
