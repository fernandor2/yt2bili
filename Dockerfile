FROM python:3.12-slim

# Install system dependencies: ffmpeg for subtitle burning, cron for scheduling
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    cron \
    curl \
    unzip \
    fonts-noto-cjk \
    libgomp1 \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Install Deno (required by yt-dlp as JavaScript runtime for YouTube anti-bot challenges)
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh

WORKDIR /app

# Install Python dependencies (includes biliup: Rust UPOS engine with PyO3 bindings)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY src/ ./src/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# Create data directories
RUN mkdir -p /app/data/downloads /app/data/db

ENTRYPOINT ["/bin/bash", "/app/entrypoint.sh"]
