"""
Bilibili uploader using the biliup Python library directly.
Uses the BiliBili and Data classes from biliup.plugins.bili_webup
instead of shelling out to the CLI via subprocess.

The biliup package (pip install biliup) bundles:
- Python wrapper classes (BiliWeb, BiliBili, Data)
- Rust-compiled upload engine via PyO3 (stream-gears)
- Cookie management, CDN line selection, UPOS chunked upload

This gives us native Python integration with proper exceptions
instead of parsing subprocess stdout/stderr.
"""

import logging
import os

from biliup.plugins.bili_webup import BiliBili, Data

logger = logging.getLogger("yt2bili.uploader")


class BilibiliUploader:
    def __init__(self, config: dict):
        self.tid = config.get("tid", 17)
        self.copyright = config.get("copyright", 2)
        self.tags = config.get("tags", "搬运,翻译,中文字幕")
        self.cookie_file = config.get("cookie_file", "/app/cookies.json")
        self.lines = config.get("lines", "AUTO")
        self.threads = config.get("threads", 3)

    def upload(
        self,
        video_path: str,
        title: str,
        description: str,
        source_url: str = "",
        cover_path: str | None = None,
    ) -> bool:
        """
        Upload a video to Bilibili using biliup's Python API directly.

        Args:
            video_path: Path to the video file
            title: Video title (max 80 chars)
            description: Video description (max 2000 chars)
            source_url: Original source URL (required for copyright=2/reprint)
            cover_path: Optional path to cover image

        Returns:
            True if upload succeeded, False otherwise.
        """
        if not os.path.exists(video_path):
            logger.error(f"Video file not found: {video_path}")
            return False

        if not os.path.exists(self.cookie_file):
            logger.error(
                f"Cookie file not found: {self.cookie_file}. "
                f"Run 'biliup login' first to generate it."
            )
            return False

        # Truncate fields to Bilibili limits
        title = title[:80]
        description = description[:2000]

        logger.info(f"Uploading to Bilibili:")
        logger.info(f"  Title: {title}")
        logger.info(f"  TID: {self.tid}")
        logger.info(f"  Tags: {self.tags}")
        logger.info(f"  Copyright: {self.copyright}")
        logger.info(f"  File: {video_path} ({os.path.getsize(video_path) / 1e6:.1f}MB)")
        logger.info(f"  Line: {self.lines}, Threads: {self.threads}")

        try:
            # Create video metadata container
            video = Data()
            video.title = title
            video.desc = description
            video.desc_v2 = [
                {
                    "raw_text": description,
                    "biz_id": "",
                    "type": 1,
                }
            ]
            video.copyright = self.copyright
            if self.copyright == 2 and source_url:
                video.source = source_url
            video.tid = self.tid
            video.set_tag(self._parse_tags())

            # Initialize the uploader with cookie-based auth
            with BiliBili(video) as bili:
                bili.login(self.cookie_file, self.cookie_file)

                # Upload the video file (uses Rust UPOS engine)
                logger.info("Uploading video file via UPOS...")
                video_part = bili.upload_file(
                    video_path, self.lines, self.threads
                )
                video_part["title"] = title[:80]
                video.append(video_part)

                # Upload cover image if provided
                if cover_path and os.path.exists(cover_path):
                    logger.info(f"Uploading cover image: {cover_path}")
                    try:
                        cover_url = bili.cover_up(cover_path)
                        video.cover = cover_url.replace("http:", "")
                    except Exception as e:
                        logger.warning(f"Cover upload failed (non-fatal): {e}")

                # Submit the video (publish)
                logger.info("Submitting video metadata...")
                ret = bili.submit("web")

            # Check result
            if isinstance(ret, dict):
                code = ret.get("code", -1)
                if code == 0:
                    bvid = ret.get("data", {}).get("bvid", "unknown")
                    aid = ret.get("data", {}).get("aid", "unknown")
                    logger.info(f"✅ Upload successful! BV: {bvid}, AV: {aid}")
                    return True
                else:
                    message = ret.get("message", str(ret))
                    logger.error(f"❌ Bilibili rejected submission: code={code}, msg={message}")
                    return False
            else:
                logger.info(f"✅ Upload completed: {ret}")
                return True

        except FileNotFoundError:
            logger.error(
                "biliup package not found. Install it: pip install biliup"
            )
            return False
        except Exception as e:
            logger.exception(f"Upload error: {e}")
            return False

    def _parse_tags(self) -> list[str]:
        """Parse comma-separated tags string into a list."""
        if isinstance(self.tags, list):
            return self.tags
        return [t.strip() for t in self.tags.split(",") if t.strip()]

    def check_auth(self) -> bool:
        """Verify that the cookie file exists and is loadable."""
        if not os.path.exists(self.cookie_file):
            logger.error(f"Cookie file not found: {self.cookie_file}")
            return False

        try:
            video = Data()
            with BiliBili(video) as bili:
                bili.login(self.cookie_file, self.cookie_file)
            logger.info("Bilibili authentication valid")
            return True
        except Exception as e:
            logger.warning(f"Auth check failed: {e}")
            return False
