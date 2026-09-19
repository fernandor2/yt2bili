"""
yt2bili - YouTube to Bilibili Automation Pipeline

Main orchestrator: monitors YouTube channels via RSS, downloads new videos,
translates subtitles to Chinese via Ollama, burns them in, and uploads to Bilibili.
"""

import logging
import os
import re
import shutil
import sys
import time
from datetime import datetime
import yaml

from db import Database
from monitor import YouTubeMonitor
from downloader import VideoDownloader, MembersOnlyVideoError
from translator import SubtitleTranslator
from burner import SubtitleBurner
from uploader import BilibiliUploader
from whisper_client import WhisperClient

log_handlers = [logging.StreamHandler(sys.stdout)]
try:
    os.makedirs("/app/data", exist_ok=True)
    log_handlers.append(logging.FileHandler("/app/data/pipeline.log", encoding="utf-8"))
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=log_handlers,
)
logger = logging.getLogger("yt2bili")


def load_config(path: str = "/app/config.yml") -> dict:
    """Load YAML configuration file."""
    with open(path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # Override Ollama host from environment if set
    ollama_env = os.environ.get("OLLAMA_HOST")
    if ollama_env:
        config.setdefault("translation", {})["ollama_host"] = ollama_env

    # Override Whisper URL from environment if set
    whisper_env = os.environ.get("WHISPER_URL")
    if whisper_env:
        config.setdefault("whisper", {})["url"] = whisper_env

    return config


def process_video(video: dict, config: dict, db: Database) -> bool:
    """
    Process a single video through the full pipeline:
    1. Download video + subtitles
    2. Translate subtitles to Chinese
    3. Translate title + description
    4. Burn subtitles into video
    5. Upload to Bilibili
    6. Cleanup

    Returns True if successful, False otherwise.
    """
    video_id = video["video_id"]
    title = video.get("title") or video_id
    channel_id = video.get("channel_id")
    channel_name = video.get("channel_name")
    if not channel_name:
        for ch in config.get("channels", []):
            if ch.get("channel_id") == channel_id:
                channel_name = ch.get("name")
                break
    channel_name = channel_name or "YouTube Creator"

    logger.info(f"{'='*60}")
    logger.info(f"Processing: {title} ({video_id})")
    logger.info(f"Channel: {channel_name}")
    logger.info(f"{'='*60}")

    dl_config = config["pipeline"]
    sub_config = config["subtitles"]
    trans_config = config["translation"]
    bili_config = config["bilibili"]

    download_dir = os.path.join(dl_config["download_dir"], video_id)
    # Clean any leftover partial or corrupted files from a previous power cut or interrupted run
    if os.path.exists(download_dir):
        shutil.rmtree(download_dir, ignore_errors=True)
    os.makedirs(download_dir, exist_ok=True)

    try:
        # ── Step 1: Download video and subtitles ──
        logger.info("[1/5] Downloading video and subtitles...")
        downloader = VideoDownloader(config)
        try:
            dl_result = downloader.download(video_id, download_dir)
        except MembersOnlyVideoError as e:
            logger.warning(
                f"🔒 Video {video_id} is members-only. "
                f"Permanently marking as 'members_only' so it will not be retried."
            )
            db.mark_members_only(video_id, f"members_only: {str(e)[:150]}")
            _cleanup(download_dir, dl_config)
            return False

        if not dl_result:
            logger.error(f"Download failed for {video_id}")
            db.mark_failed(video_id, "download_failed")
            return False

        video_path = dl_result["video_path"]
        subtitle_path = dl_result.get("subtitle_path")
        thumbnail_path = dl_result.get("thumbnail_path")
        original_description = dl_result.get("description", "")
        yt_category = dl_result.get("category", "")

        # Ensure we always use the authoritative title from YouTube downloader
        dl_title = dl_result.get("title")
        if dl_title and dl_title.strip():
            title = dl_title.strip()
            db.update_title(video_id, title)

        # Check file size limit
        max_size_bytes = dl_config.get("max_file_size_gb", 8) * 1024 * 1024 * 1024
        if os.path.getsize(video_path) > max_size_bytes:
            logger.warning(
                f"Video exceeds size limit "
                f"({os.path.getsize(video_path) / 1e9:.1f}GB > "
                f"{dl_config.get('max_file_size_gb', 8)}GB). Skipping."
            )
            db.mark_failed(video_id, "file_too_large")
            _cleanup(download_dir, dl_config)
            return False

        # ── Step 2: Subtitles (YouTube -> Standalone Whisper fallback -> Ollama translation) ──
        if not subtitle_path or not os.path.exists(subtitle_path):
            whisper_cfg = config.get("whisper", {})
            if whisper_cfg.get("enabled", True):
                logger.info("[2/5] No YouTube subtitles found. Invoking standalone Whisper service...")
                whisper_client = WhisperClient(config)
                whisper_srt = os.path.join(download_dir, f"{video_id}.whisper.srt")
                subtitle_path = whisper_client.transcribe(video_path, whisper_srt)
            else:
                logger.info("[2/5] Whisper fallback is disabled in config.")

        translator = SubtitleTranslator(trans_config)

        if subtitle_path and os.path.exists(subtitle_path):
            logger.info("[2/5] Translating subtitles to Chinese...")
            translated_sub_path = subtitle_path.rsplit(".", 1)[0] + ".zh.srt"
            translator.translate_file(subtitle_path, translated_sub_path)
            logger.info(f"Translated subtitles saved to: {translated_sub_path}")
        else:
            logger.warning(
                "[2/5] No subtitles available. Video will be uploaded without Chinese subtitles."
            )
            translated_sub_path = None

        # ── Step 3: Translate title and description ──
        logger.info("[3/5] Translating title and description...")
        zh_title = translator.translate_text(title, context="video title")
        # Bilibili title max 80 chars; keep original if translation too long
        if len(zh_title) > 75:
            zh_title = zh_title[:75] + "..."
        # Combine: Chinese title【Original title】 (skip brackets if title is empty or identical to ID)
        if title and title != video_id:
            final_title = f"{zh_title}【{title}】"
        else:
            final_title = zh_title

        if len(final_title) > 80:
            final_title = zh_title[:80]

        # Separate narrative text from links and timestamps
        narrative_lines = []
        extra_lines = []
        for raw_line in (original_description or "").splitlines():
            line_str = raw_line.strip()
            if not line_str:
                if narrative_lines and not extra_lines:
                    narrative_lines.append("")
                elif extra_lines:
                    extra_lines.append("")
                continue

            # Check if line is a timestamp (0:00 ..., 12:34 ...) or link (http/https)
            is_timestamp = bool(re.match(r"^\d{1,2}:\d{2}", line_str))
            is_link = ("http://" in line_str or "https://" in line_str)

            if is_timestamp or is_link:
                extra_lines.append(line_str)
            else:
                if not extra_lines:
                    narrative_lines.append(line_str)
                else:
                    extra_lines.append(line_str)

        narrative_text = "\n".join(narrative_lines).strip()
        extra_text = "\n".join(extra_lines).strip()

        if narrative_text:
            zh_desc = translator.translate_text(narrative_text[:1400], context="video description")
        else:
            zh_desc = title

        # Assemble full description with Chinese translation + extra links/timestamps + attribution
        desc_parts = [zh_desc]
        if extra_text:
            desc_parts.append("\n\n" + extra_text)

        attribution = (
            f"\n\n——————————————————\n"
            f"Original: {title}\n"
            f"Source: https://www.youtube.com/watch?v={video_id}\n"
            f"Channel: {channel_name}\n"
            f"Auto-translated Chinese subtitles (自动翻译中文字幕)"
        )
        desc_parts.append(attribution)
        final_desc = "".join(desc_parts)[:2000]

        # Translate YouTube tags into Chinese for Bilibili SEO
        yt_tags = dl_result.get("tags", [])
        if yt_tags:
            logger.info(f"Translating {len(yt_tags)} YouTube tags into Chinese for Bilibili...")
            zh_tags = translator.translate_tags(yt_tags)
        else:
            zh_tags = []

        # ── Step 4: Burn subtitles into video ──
        if translated_sub_path and sub_config.get("burn_in", True):
            logger.info("[4/5] Burning Chinese subtitles into video...")
            burner = SubtitleBurner(sub_config)
            output_video = video_path.rsplit(".", 1)[0] + "_zh.mp4"
            burner.burn(video_path, translated_sub_path, output_video)
            final_video = output_video
            logger.info(f"Subtitled video: {final_video}")
        else:
            logger.info("[4/5] Skipping subtitle burn-in.")
            final_video = video_path

        # ── Step 5: Upload to Bilibili ──
        logger.info("[5/5] Uploading to Bilibili...")
        uploader = BilibiliUploader(bili_config)
        upload_result = uploader.upload(
            video_path=final_video,
            title=final_title,
            description=final_desc,
            source_url=f"https://www.youtube.com/watch?v={video_id}",
            cover_path=thumbnail_path,
            yt_category=yt_category,
            tid_override=video.get("tid_override"),
            tags=zh_tags,
        )

        if upload_result:
            logger.info(f"✅ Upload successful for {video_id}")
            db.record_upload_success(video_id)
        else:
            logger.error(f"❌ Upload failed for {video_id}")
            db.mark_failed(video_id, "upload_failed")
            return False

        # ── Cleanup ──
        _cleanup(download_dir, dl_config)
        return True

    except Exception as e:
        logger.exception(f"Pipeline error for {video_id}: {e}")
        db.mark_failed(video_id, str(e)[:200])
        return False


def _cleanup(download_dir: str, dl_config: dict):
    """Remove downloaded files after successful upload."""
    if dl_config.get("cleanup_after_upload", True):
        import shutil

        try:
            shutil.rmtree(download_dir, ignore_errors=True)
            logger.info(f"Cleaned up: {download_dir}")
        except Exception as e:
            logger.warning(f"Cleanup error: {e}")


def run_pipeline_cycle(config: dict) -> int:
    """Run a single check and upload cycle. Returns number of videos uploaded."""
    logger.info("=" * 60)
    logger.info("yt2bili Pipeline Check Starting")
    logger.info(f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    db = Database(config["pipeline"]["db_path"])
    monitor = YouTubeMonitor(config)

    # 1. Scan all channels for videos/shorts from the last 90 days and queue in SQLite
    discovered = monitor.check_all_channels()
    if discovered:
        logger.info(f"Discovered and queued {len(discovered)} new video(s) into database")

    # 2. Check Bilibili upload cooldown (prevents anti-spam rate limiting)
    cooldown_minutes = config.get("pipeline", {}).get("upload_cooldown_minutes", 60)
    max_uploads_per_run = config.get("pipeline", {}).get("max_uploads_per_run", 1)

    seconds_since_last = db.get_seconds_since_last_upload()
    if seconds_since_last is not None and seconds_since_last < (cooldown_minutes * 60):
        return 0

    # 3. Pull pending videos from the SQLite queue
    pending_videos = db.get_pending_videos(limit=20)
    if not pending_videos:
        return 0

    logger.info(
        f"Database queue contains {len(pending_videos)} pending video(s). "
        f"Processing 1 by 1 (limit: {max_uploads_per_run} per run)..."
    )

    success_count = 0
    for video in pending_videos:
        video_id = video["video_id"]

        # Skip if already processed or recently failed
        if db.is_processed(video_id):
            continue

        if db.is_recently_failed(video_id, hours=24):
            continue

        # Look up channel override and fallback name if available
        channel_id = video.get("channel_id")
        for ch in config.get("channels", []):
            if ch.get("channel_id") == channel_id:
                video["tid_override"] = ch.get("tid")
                if not video.get("channel_name"):
                    video["channel_name"] = ch.get("name")
                break

        if process_video(video, config, db):
            success_count += 1
            if success_count >= max_uploads_per_run:
                break

        time.sleep(5)

    if success_count > 0:
        logger.info(f"Pipeline check finished: {success_count} video(s) uploaded successfully")
    return success_count


def is_within_active_hours() -> bool:
    """
    Check if current local time is within operating hours (07:00 to 02:00).
    During 02:00 to 07:00, the pipeline remains idle.
    """
    current_hour = datetime.now().hour
    return current_hour not in [2, 3, 4, 5, 6]


def run_daemon(config: dict):
    """Run pipeline continuously in the background without noisy countdown or idle spam."""
    interval_min = config.get("pipeline", {}).get("check_interval_minutes", 30)
    logger.info("yt2bili daemon started (active hours: 07:00 - 02:00)")

    # Initial check upon container start
    try:
        if is_within_active_hours():
            run_pipeline_cycle(config)
    except Exception as e:
        logger.exception(f"Error in pipeline cycle: {e}")

    while True:
        time.sleep(interval_min * 60)

        if not is_within_active_hours():
            continue

        try:
            run_pipeline_cycle(config)
        except Exception as e:
            logger.exception(f"Error in pipeline cycle: {e}")


def main():
    config = load_config()
    if "--loop" in sys.argv or "--daemon" in sys.argv:
        run_daemon(config)
    else:
        run_pipeline_cycle(config)


if __name__ == "__main__":
    main()
