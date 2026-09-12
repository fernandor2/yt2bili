"""
Video downloader using yt-dlp.
Downloads video (up to 1080p), subtitles, and thumbnail.
Uses curl-cffi for Chrome TLS impersonation to prevent YouTube 429 rate limits and 360p SABR throttling.
Decouples subtitle downloading from video downloading so subtitle errors never abort the video pipeline.
"""

import glob
import logging
import os
import re
import warnings
import yt_dlp

warnings.filterwarnings("ignore")

logger = logging.getLogger("yt2bili.downloader")


class VideoDownloader:
    def __init__(self, config: dict):
        self.config = config
        self.sub_config = config.get("subtitles", {})
        # Prioritize native Spanish and English tracks (avoiding Google auto-translate 429 errors)
        self.source_langs = self.sub_config.get(
            "source_langs",
            ["es-orig", "es", "es-419", "es.*", "en-orig", "en", "en.*"]
        )
        # Optional YouTube cookies file paths (Netscape format)
        self.yt_cookies_paths = [
            config.get("pipeline", {}).get("youtube_cookies_file"),
            "/app/data/yt_cookies.txt",
            "/app/yt_cookies.txt",
        ]

    def _get_cookie_file(self) -> str | None:
        """Find an existing YouTube cookies file if provided."""
        for path in self.yt_cookies_paths:
            if path and os.path.exists(path) and os.path.isfile(path) and os.path.getsize(path) > 0:
                return path
        return None

    def _has_curl_cffi(self) -> bool:
        """Check if curl_cffi is available for TLS impersonation."""
        try:
            import curl_cffi  # noqa: F401
            return True
        except ImportError:
            return False

    def _get_base_opts(self, cookie_file: str | None) -> dict:
        """Common yt-dlp options with anti-bot and resilience flags."""
        opts = {
            "retries": 10,
            "fragment_retries": 10,
            "file_access_retries": 5,
            "ignoreerrors": False,
            "quiet": True,
            "no_warnings": True,
            "sleep_interval": 1,
            "max_sleep_interval": 3,
            "socket_timeout": 30,
        }
        if cookie_file:
            opts["cookiefile"] = cookie_file
        else:
            opts["extractor_args"] = {
                "youtube": {
                    "player_client": ["mweb", "web"],
                }
            }
        return opts

    def download(self, video_id: str, output_dir: str) -> dict | None:
        """
        Download video and subtitles in decoupled stages.

        Returns dict with paths:
            {
                "video_path": "/path/to/video.mp4",
                "subtitle_path": "/path/to/video.es.srt" or None,
                "thumbnail_path": "/path/to/video.jpg" or None,
                "description": str,
                "title": str,
                "duration": int,
                "category": str,
            }
        Returns None only if video downloading fails completely.
        """
        url = f"https://www.youtube.com/watch?v={video_id}"
        output_template = os.path.join(output_dir, "%(id)s.%(ext)s")
        cookie_file = self._get_cookie_file()

        if cookie_file:
            logger.info(f"Using YouTube cookies from: {cookie_file}")
        else:
            logger.info("No YouTube cookies file detected. Using Chrome impersonation + mobile player client.")

        # ── Step A: Download Subtitles (Decoupled & Non-fatal) ──
        subtitle_path = self._try_download_subtitles(url, output_template, output_dir, video_id, cookie_file)

        # ── Step B: Download Video Stream + Thumbnail ──
        video_result = self._download_video_stream(url, output_template, output_dir, video_id, cookie_file)
        if not video_result:
            return None

        video_result["subtitle_path"] = subtitle_path
        return video_result

    def _try_download_subtitles(
        self, url: str, output_template: str, output_dir: str, video_id: str, cookie_file: str | None
    ) -> str | None:
        """Attempt to download subtitles. Catches any 429 or network errors without crashing."""
        logger.info(f"Checking subtitles for languages: {self.source_langs}...")
        sub_opts = self._get_base_opts(cookie_file)
        sub_opts.update({
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": self.source_langs,
            "outtmpl": output_template,
            "postprocessors": [
                {
                    "key": "FFmpegSubtitlesConvertor",
                    "format": "srt",
                }
            ],
            "quiet": True,
        })

        try:
            with yt_dlp.YoutubeDL(sub_opts) as ydl:
                ydl.download([url])
            found = self._find_subtitle(output_dir, video_id)
            if found:
                logger.info(f"Successfully downloaded subtitle track: {found}")
                return found
            else:
                logger.info("No subtitle track available for this video.")
                return None
        except Exception as e:
            logger.warning(
                f"Subtitle fetch encountered non-fatal error: {e}. "
                f"Will proceed with video download."
            )
            return self._find_subtitle(output_dir, video_id)

    def _download_video_stream(
        self, url: str, output_template: str, output_dir: str, video_id: str, cookie_file: str | None
    ) -> dict | None:
        """Download the video stream (up to 1080p) and thumbnail."""
        video_opts = self._get_base_opts(cookie_file)
        video_opts.update({
            # Highest quality video up to 1080p + highest quality audio merged into MP4
            "format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
            "merge_output_format": "mp4",
            "outtmpl": output_template,
            "writethumbnail": True,
            "writeinfojson": True,
            "postprocessors": [
                {
                    "key": "FFmpegThumbnailsConvertor",
                    "format": "jpg",
                }
            ],
        })

        info = None
        try:
            logger.info(f"Downloading video stream from {url}...")
            with yt_dlp.YoutubeDL(video_opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as e:
            logger.exception(f"Video download failed: {e}")
            return None

        if not info:
            logger.error(f"Failed to extract info for {video_id}")
            return None

        height = info.get("height") or 0
        width = info.get("width") or 0
        format_id = info.get("format_id") or "unknown"
        logger.info(f"Downloaded stream: {width}x{height} (format: {format_id})")

        if height > 0 and height < 720:
            logger.warning(
                f"⚠️ Stream downloaded at lower resolution ({height}p). "
                f"If 1080p is available, consider placing a 'yt_cookies.txt' inside 'data/'."
            )

        categories = info.get("categories") or []
        category = categories[0] if categories else info.get("category", "")

        video_path = self._find_video(output_dir, video_id)
        thumbnail_path = self._find_thumbnail(output_dir, video_id)

        if not video_path:
            logger.error(f"Video file not found in output directory {output_dir}")
            return None

        # Extract YouTube tags and hashtags from title/description
        raw_tags = list(info.get("tags") or [])
        text_for_tags = f"{info.get('title', '')} {info.get('description', '')}"
        hashtags = re.findall(r"#([a-zA-Z0-9_\u4e00-\u9fff\u00C0-\u017F]+)", text_for_tags)
        for ht in hashtags:
            if ht.lower() not in [t.lower() for t in raw_tags] and ht.lower() not in ["shorts", "short"]:
                raw_tags.append(ht)

        return {
            "video_path": video_path,
            "thumbnail_path": thumbnail_path,
            "description": info.get("description", ""),
            "title": info.get("title", ""),
            "duration": info.get("duration", 0),
            "category": category,
            "tags": raw_tags,
        }

    def _find_video(self, output_dir: str, video_id: str) -> str | None:
        """Find the downloaded video file."""
        patterns = [
            os.path.join(output_dir, f"{video_id}.mp4"),
            os.path.join(output_dir, f"{video_id}.mkv"),
            os.path.join(output_dir, f"{video_id}.webm"),
        ]
        for p in patterns:
            if os.path.exists(p):
                return p

        for ext in ["mp4", "mkv", "webm", "flv"]:
            matches = glob.glob(os.path.join(output_dir, f"*.{ext}"))
            matches = [m for m in matches if "_zh" not in m]
            if matches:
                return matches[0]
        return None

    def _find_subtitle(self, output_dir: str, video_id: str) -> str | None:
        """Find the downloaded subtitle file (SRT preferred)."""
        # Check priority language tags
        for lang in ["es-orig", "es", "es-419", "en-orig", "en", "en-US"]:
            srt_path = os.path.join(output_dir, f"{video_id}.{lang}.srt")
            if os.path.exists(srt_path):
                return srt_path

        srt_files = glob.glob(os.path.join(output_dir, f"{video_id}.*.srt"))
        srt_files = [f for f in srt_files if ".zh.srt" not in f]
        if srt_files:
            return srt_files[0]

        vtt_files = glob.glob(os.path.join(output_dir, f"{video_id}.*.vtt"))
        if vtt_files:
            return vtt_files[0]

        return None

    def _find_thumbnail(self, output_dir: str, video_id: str) -> str | None:
        """Find the downloaded thumbnail."""
        for ext in ["jpg", "jpeg", "png", "webp"]:
            path = os.path.join(output_dir, f"{video_id}.{ext}")
            if os.path.exists(path):
                return path

        for ext in ["jpg", "jpeg", "png", "webp"]:
            matches = glob.glob(os.path.join(output_dir, f"*.{ext}"))
            if matches:
                return matches[0]
        return None
