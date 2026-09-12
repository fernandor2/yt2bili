# yt2bili — YouTube to Bilibili Automation Pipeline

An autonomous, containerized automation pipeline running in Docker that monitors YouTube channels (both standard videos and Shorts without duration filtering), downloads media and subtitles, translates subtitles, titles, and descriptions into Simplified Chinese using a local Ollama instance (preserving original audio without TTS), burns translated subtitles with an opaque background box (cleanly replacing pre-existing hardsubs), and publishes content to Bilibili under creator-original copyright with automatic category matching.

Engineered specifically for local mini-PC servers (such as AMD Ryzen 5700G, 32GB RAM) running daily schedules.

---

## Table of Contents

- [Key Features](#-key-features)
- [System Architecture](#-system-architecture)
- [Deployment Tutorial (Step-by-Step)](#-deployment-tutorial-step-by-step)
  - [1. Prerequisites](#1-prerequisites)
  - [2. Clone the Repository](#2-clone-the-repository)
  - [3. Ensure Ollama Model is Downloaded](#3-ensure-ollama-model-is-downloaded)
  - [4. Configure YouTube Channels & Settings](#4-configure-youtube-channels--settings)
  - [5. One-Time Bilibili QR Code Login](#5-one-time-bilibili-qr-code-login)
  - [6. Start the Unattended Service](#6-start-the-unattended-service)
  - [7. Verification & Log Inspection](#7-verification--log-inspection)
- [Automatic Category Mapping (YouTube → Bilibili)](#-automatic-category-mapping-youtube--bilibili)
- [Subtitle Box Styling (Covering Source Hardsubs)](#-subtitle-box-styling-covering-source-hardsubs)
- [Rate Limiting & Cooldown Protection](#-rate-limiting--cooldown-protection)
- [Configuration Reference (`config.yml`)](#-configuration-reference-configyml)
- [Troubleshooting & Maintenance](#-troubleshooting--maintenance)

---

## 🌟 Key Features

1. **Unfiltered YouTube Monitoring:**
   - Polls official YouTube Atom/RSS feeds every 30 minutes without API keys or quota consumption.
   - Ingests both standard widescreen videos and vertical Shorts without duration constraints.
2. **Local LLM Subtitle Translation (Ollama):**
   - Batches subtitles (30 lines per call) with a sliding context window for natural Chinese phrasing.
   - Timestamps are completely decoupled and preserved in Python via `pysubs2` to eliminate audio desync.
   - Uses `qwen2.5:7b`, the premier open-weights model for English/Spanish to Chinese translation.
3. **Hardcoded Subtitles with Opaque Box (`BorderStyle=3`):**
   - Automatically overlays a solid black background box (`&H00000000`) behind the Chinese characters.
   - Completely covers and replaces any pre-existing burned-in subtitles present in the source video.
   - Audio is copied losslessly (`-c:a copy`) to preserve original audio fidelity.
4. **Dynamic Category Detection:**
   - Detects the video's native category on YouTube and maps it to the corresponding Bilibili partition TID (Gaming, Tech, Science, Entertainment, etc.).
5. **Anti-Spam Upload Cooldown (60 Minutes):**
   - Enforces a minimum 60-minute interval between consecutive uploads to prevent Bilibili risk control triggers (error code `21070`).
   - Limits processing to 1 video per run, gracefully queueing surplus videos in SQLite.
6. **Docker Network Integration (`ai-net`):**
   - Directly attaches to the existing external Docker network `ai-net` to reach Ollama at `http://ollama:11434` without host port routing.
7. **Server Operating Schedule (7:00 AM – 2:00 AM):**
   - Cron daemon scheduled to run every 30 minutes between 07:00 and 01:30.
   - Persistent SQLite tracking prevents duplicate processing across daily reboots.

---

## 🏗️ System Architecture

```mermaid
flowchart LR
    subgraph Host["Mini PC Server (Ryzen 5700G · 32GB RAM)"]
        subgraph DockerNet["Docker Network: ai-net"]
            Ollama["🤖 Ollama Container\nhttp://ollama:11434\nModel: qwen2.5:7b"]
            
            subgraph YT2BILI["Docker Container: yt2bili"]
                Cron["⏰ Cron Daemon\n(07:00 to 01:30)"]
                Monitor["📡 YouTube Monitor\n(Atom RSS Feed)"]
                DB[("🗄️ SQLite Database\nprocessed.db")]
                DL["📥 yt-dlp Downloader\nVideo + Subtitles + Metadata"]
                Trans["🌐 Translator\npysubs2 + Ollama API"]
                Burn["🔥 FFmpeg Burner\nHardsubs with Opaque Box"]
                Upload["📤 Bilibili Uploader\nbiliup Python Engine"]
            end
        end
    end

    YT["🎬 YouTube Channels\n(Videos & Shorts)"] -->|"RSS XML"| Monitor
    Monitor --> DB
    DB -->|"Pending Video Queue"| DL
    DL --> Trans
    Trans <-->|"REST API (:11434)"| Ollama
    Trans --> Burn
    Burn --> Upload
    Upload -->|"Web UPOS Protocol"| Bili["📺 Bilibili (Creator Original)"]
```

---

## 🚀 Deployment Tutorial (Step-by-Step)

Follow these instructions to deploy the pipeline on your mini-PC server.

### 1. Prerequisites

Ensure your mini-PC server has:
- Docker and Docker Compose installed.
- The `ai-net` Docker network already created.
- The `ollama` container running and attached to `ai-net`.

### 2. Clone the Repository

On your mini-PC terminal:
```bash
git clone https://github.com/fernandor2/yt2bili.git
cd yt2bili
```

### 3. Ensure Ollama Model is Downloaded

Verify that your Ollama container has the `qwen2.5:7b` model available:
```bash
docker exec -it ollama ollama pull qwen2.5:7b
```

### 4. Configure YouTube Channels & Settings

Open `config.yml` in your preferred editor:
```bash
nano config.yml
```

Add the YouTube channel IDs (`UC...`) you want to monitor:
```yaml
channels:
  - name: "My Main Channel"
    channel_id: "UCxxxxxxxxxxxxxxxxxxxxxxxxx"
  - name: "Second Channel"
    channel_id: "UCyyyyyyyyyyyyyyyyyyyyyyyyy"
```

> **How to find a YouTube Channel ID:**
> 1. Visit `https://www.youtube.com/@ChannelHandle`.
> 2. Right-click anywhere on the page and select **View Page Source**.
> 3. Search (`Ctrl+F`) for `"channelId":"UC` or use a free channel ID lookup tool.

### 5. One-Time Bilibili QR Code Login

Before running in the background, generate your persistent Bilibili credentials:

```bash
docker compose run --rm yt2bili biliup login
```

- A QR code will be rendered in your terminal.
- Open the **Bilibili Mobile App** on your smartphone.
- Tap the **Scan (扫一扫)** icon in the top right corner and confirm the login.
- Once confirmed, `cookies.json` will be saved in the project root directory. This file is mounted as a persistent volume and automatically renewed upon each upload.

### 6. Start the Unattended Service

Build the Docker image and start the container in detached mode:

```bash
docker compose up -d --build
```

### 7. Verification & Log Inspection

Watch the execution logs in real time:
```bash
docker compose logs -f yt2bili
```

To manually trigger an immediate check outside the cron schedule:
```bash
docker compose exec yt2bili python -u /app/src/main.py
```

---

## 🏷️ Automatic Category Mapping (YouTube → Bilibili)

The pipeline automatically inspects each video's native YouTube category and translates it into the appropriate Bilibili partition TID:

| YouTube Category | Bilibili Partition Name | Bilibili TID |
| :--- | :--- | :---: |
| **Gaming** | Single-player Games (单机游戏) | `17` |
| **Science & Technology** | Technology & Digital (数码) | `188` |
| **Education** | Science & Knowledge (科学科普) | `201` |
| **Film & Animation** | Animation Comprehensive (综合动画) | `27` |
| **Entertainment** | Entertainment Comprehensive (娱乐综合) | `71` |
| **Comedy** | Comedy & Humor (搞笑) | `138` |
| **Music** | Music Comprehensive (音乐综合) | `130` |
| **Sports** | Sports Comprehensive (运动综合) | `234` |
| **Autos & Vehicles** | Automobiles Comprehensive (汽车综合) | `176` |
| **Pets & Animals** | Pets & Animals (动物圈) | `217` |
| **Travel & Events** | Daily Life & Travel (日常) | `21` |
| **People & Blogs** | Daily Life (日常) | `21` |
| **Howto & Style** | Crafts & Lifestyle (手工) | `161` |
| **News & Politics** | Hot Topics & News (热点) | `204` |

You can customize or override any of these mappings inside `config.yml`:
```yaml
bilibili:
  category_mapping:
    "Gaming": 171  # Redirect gaming videos to eSports (电子竞技)
```

---

## 📦 Subtitle Box Styling (Covering Source Hardsubs)

If your source YouTube videos already contain burned-in English or Spanish subtitles, rendering plain text on top creates an unreadable overlap.

`yt2bili` uses **FFmpeg ASS `BorderStyle=3`**, creating an opaque bounding box:
- **Box Fill (`box_color`):** `&H00000000` (100% solid black).
- **Text (`text_color`):** `&H00FFFFFF` (crisp pure white).
- **Font:** `Noto Sans CJK SC` (installed inside the container image).
- **Padding (`box_padding`):** `4px` padding around characters for clean margins.
- **Vertical Margin (`margin_v`):** `30px` from the bottom edge.

This produces the clean, professional subtitle bar standard used by Chinese localization groups (*字幕组*).

---

## ⏱️ Rate Limiting & Cooldown Protection

Bilibili enforces anti-spam risk control (*风控*). If an automated tool submits several videos in rapid succession, Bilibili rejects requests with error code `21070` (*Submissions too frequent*).

To prevent this:
1. **Persistent Timestamp Tracking:** The exact UTC timestamp of each successful upload is stored in SQLite (`metadata` table).
2. **60-Minute Cooldown (`upload_cooldown_minutes: 60`):** If a cron execution runs while an upload occurred less than 60 minutes ago, the pipeline logs the remaining wait time and exits safely.
3. **Queue Processing (`max_uploads_per_run: 1`):** Only 1 video is uploaded per cycle. If a channel posts multiple videos at once, they are safely queued in SQLite and published one by one across subsequent hourly runs.

---

## ⚙️ Configuration Reference (`config.yml`)

```yaml
# Monitored YouTube channels
channels:
  - name: "Channel Name"
    channel_id: "UCxxxxxxxxxxxxxxxxxxxxxxxxx"
    # tid: 17 # Optional: force a specific Bilibili category for this channel

bilibili:
  tid: 17 # Default fallback category
  category_mapping: # YouTube -> Bilibili category mappings
    "Gaming": 17
    "Science & Technology": 188
    # ...
  copyright: 1 # 1 = Creator Original (自制 - Recommended), 2 = Reprint (转载)
  tags: "原创,翻译,中文字幕" # Video tags (comma-separated, max 12)
  cookie_file: "/app/cookies.json" # Path to credentials file

translation:
  ollama_host: "http://ollama:11434" # Ollama endpoint in ai-net
  model: "qwen2.5:7b" # Model identifier
  batch_size: 30 # Lines translated per LLM prompt
  temperature: 0.3 # Generation temperature

subtitles:
  source_langs: ["en.*", "es.*", "en", "es"]
  burn_in: true # Burn hardsubs into video
  border_style: 3 # 3 = Opaque background box (covers existing subs)
  box_padding: 4 # Box padding in pixels
  margin_v: 30 # Vertical margin from bottom
  font_name: "Noto Sans CJK SC"
  font_size: 20
  ffmpeg_preset: "faster" # Encoding speed preset
  ffmpeg_crf: 20 # Constant Rate Factor (18-23)

pipeline:
  upload_cooldown_minutes: 60 # Cooldown between uploads (minutes)
  max_uploads_per_run: 1 # Maximum uploads per cycle
  download_dir: "/app/data/downloads"
  db_path: "/app/data/db/processed.db"
  cleanup_after_upload: true # Delete downloaded video after upload
  max_file_size_gb: 8 # Maximum video size allowed
```

---

## 🔧 Troubleshooting & Maintenance

### How to re-authenticate if cookies expire
Run the interactive login command again:
```bash
docker compose run --rm yt2bili biliup login
```

### Inspecting processing history and database
To view processed video statuses directly from SQLite:
```bash
docker compose exec yt2bili sqlite3 /app/data/db/processed.db "SELECT video_id, title, status, updated_at FROM processed_videos;"
```

### Handling daily 7:00 AM – 2:00 AM server power cycles
No manual intervention is required.
- The container is configured with `restart: unless-stopped`. When your mini-PC powers on at 7:00 AM, Docker brings `yt2bili` up automatically.
- State is preserved in `./data/db/processed.db`, guaranteeing that videos are neither duplicated nor forgotten.
