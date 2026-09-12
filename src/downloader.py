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
        self.sub_config = config.get("subtitles", {})
        self.source_langs = self.sub_config.get("source_langs", ["en.*", "en"])

    def download(self, video_id: str, output_dir: str) -> dict | None:
        """
        Download video with subtitles and thumbnail.

        Returns dict with paths:
            {
                "video_path": "/path/to/video.mp4",
                "subtitle_path": "/path/to/video.en.srt" or None,
                "thumbnail_path": "/path/to/video.jpg" or None,
                "description": "...",
                "title": "..."
            }
        Returns None if download fails.
        """
        url = f"https://www.youtube.com/watch?v={video_id}"
        output_template = os.path.join(output_dir, "%(id)s.%(ext)s")

        ydl_opts = {
            # Video format: best mp4 up to 1080p to balance quality/size
            "format": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
            "merge_output_format": "mp4",
            "outtmpl": output_template,
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
            # Retry and robustness
            "retries": 5,
            "fragment_retries": 5,
            "ignoreerrors": False,
            "no_warnings": False,
            # Metadata
            "writeinfojson": True,
            # Avoid issues with YouTube rate limiting
            "sleep_interval": 1,
            "max_sleep_interval": 5,
            # No duration filter - download everything including Shorts
        }

        try:
            logger.info(f"Downloading {url}...")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)

                if not info:
                    logger.error(f"No info extracted for {video_id}")
                    return None

            # Find the downloaded files
            result = {
                "video_path": self._find_video(output_dir, video_id),
                "subtitle_path": self._find_subtitle(output_dir, video_id),
                "thumbnail_path": self._find_thumbnail(output_dir, video_id),
                "description": info.get("description", ""),
                "title": info.get("title", ""),
                "duration": info.get("duration", 0),
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
