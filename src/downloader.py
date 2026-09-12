"""
Video downloader using yt-dlp.
Downloads video, auto-generated/manual subtitles, and thumbnail.
No duration filtering - downloads both regular videos and Shorts.
"""

import logging
import os
import glob
import yt_dlp

logger = logging.getLogger("yt2bili.downloader")


class VideoDownloader:
    def __init__(self, config: dict):
        self.config = config
        self.sub_config = config.get("subtitles", {})
        self.source_langs = self.sub_config.get("source_langs", ["en.*", "es.*", "en", "es"])
        # Optional YouTube cookies file (Netscape format)
        self.yt_cookies_paths = [
            config.get("pipeline", {}).get("youtube_cookies_file", "/app/data/yt_cookies.txt"),
            "/app/data/yt_cookies.txt",
            "/app/yt_cookies.txt",
        ]

    def _get_cookie_file(self) -> str | None:
        """Find an existing YouTube cookies file if provided."""
        for path in self.yt_cookies_paths:
            if path and os.path.exists(path) and os.path.isfile(path) and os.path.getsize(path) > 0:
                return path
        return None

    def download(self, video_id: str, output_dir: str) -> dict | None:
        """
        Download video with subtitles and thumbnail.

        Returns dict with paths:
            {
                "video_path": "/path/to/video.mp4",
                "subtitle_path": "/path/to/video.en.srt" or None,
                "thumbnail_path": "/path/to/video.jpg" or None,
                "description": "...",
                "title": "...",
                "duration": 120,
                "category": "Gaming",
            }
        Returns None if download fails.
        """
        url = f"https://www.youtube.com/watch?v={video_id}"
        output_template = os.path.join(output_dir, "%(id)s.%(ext)s")

        ydl_opts = {
            # Highest quality video up to 1080p + highest quality audio (regardless of source codec).
            # Remuxes into MP4 container via FFmpeg. Does NOT constrain download to legacy AVC/MP4.
            "format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
            "merge_output_format": "mp4",
            "outtmpl": output_template,

            # Player client emulation:
            # Android and iOS clients bypass web bot-detection, SABR throttling, and web login checks
            "extractor_args": {
                "youtube": {
                    "player_client": ["android", "ios", "web_creator", "web"],
                    "player_skip": ["configs", "webpage"],
                }
            },

            # Subtitles: try manual first, then auto-generated
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": self.source_langs,
            # Thumbnail
            "writethumbnail": True,
            # Post-processors
            "postprocessors": [
                {
                    # Convert subtitles to clean SRT
                    "key": "FFmpegSubtitlesConvertor",
                    "format": "srt",
                },
                {
                    # Convert thumbnail to jpg
                    "key": "FFmpegThumbnailsConvertor",
                    "format": "jpg",
                },
            ],
            # Resilience against network hiccups & throttling
            "retries": 10,
            "fragment_retries": 10,
            "file_access_retries": 5,
            "ignoreerrors": False,
            "no_warnings": False,
            "writeinfojson": True,
            "sleep_interval": 1,
            "max_sleep_interval": 4,
            "socket_timeout": 30,
            # No duration filter - download everything including Shorts
        }

        # Check for optional YouTube cookies file
        cookie_file = self._get_cookie_file()
        if cookie_file:
            logger.info(f"Using YouTube cookies file: {cookie_file}")
            ydl_opts["cookiefile"] = cookie_file
        else:
            logger.info(
                "No YouTube cookies file detected. Relying on Android/iOS client emulation."
            )

        try:
            logger.info(f"Downloading {url}...")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)

                if not info:
                    logger.error(f"No info extracted for {video_id}")
                    return None

            # Extract category (e.g. 'Gaming', 'Science & Technology', 'Entertainment')
            categories = info.get("categories") or []
            category = categories[0] if categories else info.get("category", "")

            # Log actual downloaded resolution
            height = info.get("height") or 0
            width = info.get("width") or 0
            format_id = info.get("format_id") or "unknown"
            logger.info(f"Downloaded stream: {width}x{height} (format: {format_id})")

            if height > 0 and height < 720:
                logger.warning(
                    f"⚠️ Video downloaded at lower resolution ({height}p). "
                    f"If 1080p was expected, place a valid 'yt_cookies.txt' inside 'data/'."
                )

            # Find the downloaded files
            result = {
                "video_path": self._find_video(output_dir, video_id),
                "subtitle_path": self._find_subtitle(output_dir, video_id),
                "thumbnail_path": self._find_thumbnail(output_dir, video_id),
                "description": info.get("description", ""),
                "title": info.get("title", ""),
                "duration": info.get("duration", 0),
                "category": category,
            }

            if not result["video_path"]:
                logger.error(f"Video file not found after download in {output_dir}")
                return None

            logger.info(f"Download complete:")
            logger.info(f"  Video: {result['video_path']}")
            logger.info(
                f"  Subtitles: {result['subtitle_path'] or 'NOT FOUND'}"
            )
            logger.info(
                f"  Thumbnail: {result['thumbnail_path'] or 'NOT FOUND'}"
            )
            logger.info(
                f"  Duration: {result['duration']}s"
            )

            return result

        except yt_dlp.utils.DownloadError as e:
            logger.error(f"yt-dlp download error: {e}")
            return None
        except Exception as e:
            logger.exception(f"Unexpected download error: {e}")
            return None

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

        # Fallback: glob for any video file
        for ext in ["mp4", "mkv", "webm", "flv"]:
            matches = glob.glob(os.path.join(output_dir, f"*.{ext}"))
            # Exclude subtitle-burned versions
            matches = [m for m in matches if "_zh" not in m]
            if matches:
                return matches[0]
        return None

    def _find_subtitle(self, output_dir: str, video_id: str) -> str | None:
        """Find the downloaded subtitle file (SRT preferred)."""
        # Try specific language patterns
        for lang in ["en", "en-orig", "en-US"]:
            srt_path = os.path.join(output_dir, f"{video_id}.{lang}.srt")
            if os.path.exists(srt_path):
                return srt_path

        # Fallback: any .srt file (not the translated .zh.srt)
        srt_files = glob.glob(os.path.join(output_dir, f"{video_id}.*.srt"))
        srt_files = [f for f in srt_files if ".zh.srt" not in f]
        if srt_files:
            return srt_files[0]

        # Try VTT fallback
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

        # Glob fallback
        for ext in ["jpg", "jpeg", "png", "webp"]:
            matches = glob.glob(os.path.join(output_dir, f"*.{ext}"))
            if matches:
                return matches[0]
        return None
