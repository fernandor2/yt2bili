"""
yt2bili - YouTube to Bilibili Automation Pipeline

Main orchestrator: monitors YouTube channels via RSS, downloads new videos,
translates subtitles to Chinese via Ollama, burns them in, and uploads to Bilibili.
"""

import logging
import sys
import os
import yaml
import time

from db import Database
from monitor import YouTubeMonitor
from downloader import VideoDownloader
from translator import SubtitleTranslator
from burner import SubtitleBurner
from uploader import BilibiliUploader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
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
    title = video["title"]
    channel_name = video["channel_name"]

    logger.info(f"{'='*60}")
    logger.info(f"Processing: {title} ({video_id})")
    logger.info(f"Channel: {channel_name}")
    logger.info(f"{'='*60}")

    dl_config = config["pipeline"]
    sub_config = config["subtitles"]
    trans_config = config["translation"]
    bili_config = config["bilibili"]

    download_dir = os.path.join(dl_config["download_dir"], video_id)
    os.makedirs(download_dir, exist_ok=True)

    try:
        # ── Step 1: Download video and subtitles ──
        logger.info("[1/5] Downloading video and subtitles...")
        downloader = VideoDownloader(config)
        dl_result = downloader.download(video_id, download_dir)

        if not dl_result:
            logger.error(f"Download failed for {video_id}")
            db.mark_failed(video_id, "download_failed")
            return False

        video_path = dl_result["video_path"]
        subtitle_path = dl_result.get("subtitle_path")
        thumbnail_path = dl_result.get("thumbnail_path")
        original_description = dl_result.get("description", "")
        yt_category = dl_result.get("category", "")

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

        # ── Step 2: Translate subtitles ──
        translator = SubtitleTranslator(trans_config)

        if subtitle_path and os.path.exists(subtitle_path):
            logger.info("[2/5] Translating subtitles to Chinese...")
            translated_sub_path = subtitle_path.rsplit(".", 1)[0] + ".zh.srt"
            translator.translate_file(subtitle_path, translated_sub_path)
            logger.info(f"Translated subtitles saved to: {translated_sub_path}")
        else:
            logger.warning(
                "[2/5] No subtitles found. Video will be uploaded without Chinese subtitles."
            )
            translated_sub_path = None

        # ── Step 3: Translate title and description ──
        logger.info("[3/5] Translating title and description...")
        zh_title = translator.translate_text(title, context="video title")
        # Bilibili title max 80 chars; keep original if translation too long
        if len(zh_title) > 75:
            zh_title = zh_title[:75] + "..."
        # Combine: Chinese title【Original title】
        final_title = f"{zh_title}【{title}】"
        if len(final_title) > 80:
            final_title = zh_title[:80]

        zh_desc = translator.translate_text(
            original_description[:500] if original_description else title,
            context="video description",
        )
        final_desc = (
            f"{zh_desc}\n\n"
            f"——————————————————\n"
            f"Original: {title}\n"
            f"Source: https://www.youtube.com/watch?v={video_id}\n"
            f"Channel: {channel_name}\n"
            f"Auto-translated Chinese subtitles (自动翻译中文字幕)"
        )

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
        )

        if upload_result:
            logger.info(f"✅ Upload successful for {video_id}")
            db.mark_done(video_id)
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


def main():
    """Main pipeline entry point."""
    logger.info("=" * 60)
    logger.info("yt2bili Pipeline Run Starting")
    logger.info(f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    config = load_config()
    db = Database(config["pipeline"]["db_path"])
    monitor = YouTubeMonitor(config)

    # Get new videos from all monitored channels
    new_videos = monitor.check_all_channels()
    logger.info(f"Found {len(new_videos)} new video(s) to process")

    if not new_videos:
        logger.info("No new videos. Pipeline complete.")
        return

    # Process each video
    success_count = 0
    for video in new_videos:
        video_id = video["video_id"]

        # Skip if already processed or recently failed
        if db.is_processed(video_id):
            logger.info(f"Skipping {video_id} (already processed)")
            continue

        if db.is_recently_failed(video_id, hours=24):
            logger.info(f"Skipping {video_id} (failed within 24h, will retry later)")
            continue

        if process_video(video, config, db):
            success_count += 1

        # Brief pause between videos to avoid rate limits
        time.sleep(5)

    logger.info(f"Pipeline complete: {success_count}/{len(new_videos)} processed successfully")


if __name__ == "__main__":
    main()
