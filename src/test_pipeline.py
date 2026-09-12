"""
Test pipeline script for yt2bili.
Allows testing download, subtitle translation, and hardsub burning on a single video,
and optionally uploading it to Bilibili as an unlisted draft (scheduled release).

Completely isolated from the production pipeline:
- Does NOT record into processed.db (no SQLite locks or skip flags)
- Does NOT trigger or check upload cooldowns
- Saves final subtitled video to /app/data/test/ for easy local inspection
"""

import argparse
import logging
import os
import re
import shutil
import sys
import yaml

from burner import SubtitleBurner
from downloader import VideoDownloader
from translator import SubtitleTranslator
from uploader import BilibiliUploader
from whisper_client import WhisperClient

# Configure clear logging for test runs
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] yt2bili.test: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("yt2bili.test")

DEFAULT_TEST_VIDEO_ID = "UhsOJwHTDzM"


def extract_video_id(url_or_id: str) -> str:
    """Extract YouTube 11-character video ID from a URL or raw ID."""
    url_or_id = url_or_id.strip()
    if not url_or_id:
        return DEFAULT_TEST_VIDEO_ID

    # Check for shorts URL: youtube.com/shorts/<id>
    shorts_match = re.search(r"shorts/([a-zA-Z0-9_-]{11})", url_or_id)
    if shorts_match:
        return shorts_match.group(1)

    # Check for watch URL: v=<id>
    watch_match = re.search(r"[?&]v=([a-zA-Z0-9_-]{11})", url_or_id)
    if watch_match:
        return watch_match.group(1)

    # Check for youtu.be/<id>
    short_link_match = re.search(r"youtu\.be/([a-zA-Z0-9_-]{11})", url_or_id)
    if short_link_match:
        return short_link_match.group(1)

    # If it's already an 11-char ID
    id_match = re.search(r"([a-zA-Z0-9_-]{11})", url_or_id)
    if id_match:
        return id_match.group(1)

    return url_or_id


def load_config() -> dict:
    """Load config.yml from standard search paths."""
    config_paths = [
        "/app/config.yml",
        os.path.join(os.path.dirname(__file__), "..", "config.yml"),
        "config.yml",
    ]
    for path in config_paths:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f)
    raise FileNotFoundError("config.yml not found in standard paths")


def run_test(video_id: str, no_upload: bool = False, keep_temp: bool = False):
    """Run the complete pipeline for a single test video."""
    logger.info("=" * 60)
    logger.info(f"🚀 Starting Test Pipeline for Video ID: {video_id}")
    logger.info(f"   YouTube URL: https://youtube.com/shorts/{video_id}")
    logger.info(f"   Upload Mode: {'NO UPLOAD (Local only)' if no_upload else 'Bilibili Draft (Scheduled in 7 days)'}")
    logger.info("=" * 60)

    config = load_config()
    test_base_dir = "/app/data/test"
    os.makedirs(test_base_dir, exist_ok=True)

    test_download_dir = os.path.join(test_base_dir, f"dl_{video_id}")
    os.makedirs(test_download_dir, exist_ok=True)

    # ── Step 1: Download Video & Subtitles ──
    logger.info("[1/5] Downloading video stream and subtitles...")
    downloader = VideoDownloader(config)
    dl_result = downloader.download(video_id, test_download_dir)

    if not dl_result or not dl_result.get("video_path"):
        logger.error(f"❌ Failed to download video: {video_id}")
        return False

    video_path = dl_result["video_path"]
    subtitle_path = dl_result.get("subtitle_path")
    thumbnail_path = dl_result.get("thumbnail_path")
    title = dl_result.get("title", f"Test Video {video_id}")
    description = dl_result.get("description", "")
    category = dl_result.get("category", "")

    logger.info(f"✅ Video downloaded: {video_path} ({os.path.getsize(video_path) / 1e6:.1f}MB)")
    logger.info(f"   Original title: {title}")
    logger.info(f"   Subtitle track: {subtitle_path or 'None found'}")

    # ── Step 2: Subtitle Processing & Translation ──
    if not subtitle_path or not os.path.exists(subtitle_path):
        whisper_cfg = config.get("whisper", {})
        if whisper_cfg.get("enabled", True):
            logger.info("[2/5] No YouTube subtitles. Calling Whisper fallback...")
            whisper_client = WhisperClient(config)
            whisper_srt = os.path.join(test_download_dir, f"{video_id}.whisper.srt")
            subtitle_path = whisper_client.transcribe(video_path, whisper_srt)
        else:
            logger.info("[2/5] Whisper disabled in config. No subtitles available.")

    translator = SubtitleTranslator(config.get("translation", {}))
    translated_sub_path = None

    if subtitle_path and os.path.exists(subtitle_path):
        logger.info("[2/5] Cleaning and translating subtitles with Ollama (qwen2.5:14b)...")
        translated_sub_path = os.path.join(test_download_dir, f"{video_id}.zh.srt")
        translator.translate_file(subtitle_path, translated_sub_path)
        logger.info(f"✅ Translated subtitles saved to: {translated_sub_path}")
    else:
        logger.warning("[2/5] No subtitle file generated. Video will have no Chinese subtitles.")

    # ── Step 3: Translate Title & Description ──
    logger.info("[3/5] Translating title and description...")
    zh_title = translator.translate_text(title, context="video title")
    final_title = f"{zh_title}【{title}】"[:80]

    narrative_lines = []
    for line in description.splitlines():
        l_str = line.strip()
        if l_str and not l_str.startswith("http") and not re.match(r"^\d{1,2}:\d{2}", l_str):
            narrative_lines.append(l_str)
    narrative_text = " ".join(narrative_lines)[:800]

    zh_desc = translator.translate_text(narrative_text, context="video description") if narrative_text else title
    final_desc = f"{zh_desc}\n\nOriginal: {title}\nSource: https://www.youtube.com/watch?v={video_id}"

    logger.info(f"✅ Chinese Title: {final_title}")

    # ── Step 4: Burn Subtitles ──
    final_video_path = os.path.join(test_base_dir, f"test_{video_id}_zh.mp4")

    if translated_sub_path and os.path.exists(translated_sub_path):
        logger.info("[4/5] Burning Chinese subtitles into video with FFmpeg...")
        burner = SubtitleBurner(config.get("subtitles", {}))
        burner.burn(video_path, translated_sub_path, final_video_path)
        logger.info(f"✅ Subtitled video ready: {final_video_path} ({os.path.getsize(final_video_path) / 1e6:.1f}MB)")
    else:
        logger.info("[4/5] No subtitles to burn. Copying original video to test output...")
        shutil.copy2(video_path, final_video_path)

    # ── Step 5: Upload as Draft or Complete ──
    if no_upload:
        logger.info("=" * 60)
        logger.info(f"🎉 TEST COMPLETE! (Upload was skipped via --no-upload)")
        logger.info(f"📁 Subtitled video saved to: {final_video_path}")
        logger.info(f"   You can inspect this file directly in: ./data/test/test_{video_id}_zh.mp4")
        logger.info("=" * 60)
    else:
        logger.info("[5/5] Uploading to Bilibili as DRAFT (scheduled release in 7 days)...")
        uploader = BilibiliUploader(config.get("bilibili", {}))
        success = uploader.upload(
            video_path=final_video_path,
            title=final_title,
            description=final_desc,
            source_url=f"https://www.youtube.com/watch?v={video_id}",
            cover_path=thumbnail_path,
            yt_category=category,
            draft=True,
        )
        if success:
            logger.info("=" * 60)
            logger.info("🎉 TEST DRAFT UPLOAD SUCCESSFUL!")
            logger.info("🔒 The video is stored as an UNLISTED DRAFT scheduled in 7 days.")
            logger.info("👉 Check it out privately in Bilibili Creator Studio:")
            logger.info("   https://member.bilibili.com/platform/upload-manager/article")
            logger.info(f"📁 Local subtitled video is also saved at: {final_video_path}")
            logger.info("=" * 60)
        else:
            logger.error("❌ Bilibili draft upload failed.")

    # Cleanup temporary download folder unless requested to keep
    if not keep_temp and os.path.exists(test_download_dir):
        shutil.rmtree(test_download_dir, ignore_errors=True)

    return True


def main():
    parser = argparse.ArgumentParser(description="yt2bili Single Video Test Pipeline")
    parser.add_argument(
        "video",
        nargs="?",
        default=DEFAULT_TEST_VIDEO_ID,
        help=f"YouTube video ID or URL to test (default: {DEFAULT_TEST_VIDEO_ID})",
    )
    parser.add_argument(
        "--no-upload",
        action="store_true",
        help="Run download, translation, and burning locally without uploading to Bilibili",
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="Keep temporary files in data/test/dl_<video_id>",
    )

    args = parser.parse_args()
    video_id = extract_video_id(args.video)
    run_test(video_id=video_id, no_upload=args.no_upload, keep_temp=args.keep_temp)


if __name__ == "__main__":
    main()
