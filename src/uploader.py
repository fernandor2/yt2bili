"""
Bilibili uploader using the biliup Python library directly.
Uses the BiliBili and Data classes from biliup.plugins.bili_webup
instead of shelling out to the CLI via subprocess.

The biliup package (pip install biliup) bundles:
- Python wrapper classes (BiliWeb, BiliBili, Data)
- Rust-compiled upload engine via PyO3 (stream-gears)
- Cookie management, CDN line selection, UPOS chunked upload
"""

import logging
import os
import re
import time

from biliup.plugins.bili_webup import BiliBili, Data

logger = logging.getLogger("yt2bili.uploader")

# Mapping of YouTube categories (standard English & localized Spanish labels) to Bilibili partition TIDs
DEFAULT_CATEGORY_MAPPING = {
    # Gaming (Partition 4) -> 17 (Single-player Games)
    "gaming": 17,
    "videojuegos": 17,
    "juegos": 17,
    "games": 17,
    # Science & Technology (Partition 36) -> 188 (Technology & Digital)
    "science & technology": 188,
    "ciencia y tecnología": 188,
    "technology": 188,
    "tecnología": 188,
    # Education -> 201 (Science & Knowledge)
    "education": 201,
    "educación": 201,
    # Film & Television -> 241 (Entertainment Discussions / Pop Culture / 娱乐杂谈)
    "film & animation": 241,
    "películas y animación": 241,
    "cine y animación": 241,
    "film": 241,
    "cine": 241,
    "películas": 241,
    "movies": 241,
    # Animation (Partition 1) -> 27 (Animation Comprehensive)
    "animation": 27,
    "animación": 27,
    # Autos & Vehicles -> 176 (Automobiles Comprehensive)
    "autos & vehicles": 176,
    "motor": 176,
    "automóviles": 176,
    "autos": 176,
    # Music (Partition 3) -> 130 (Music Comprehensive)
    "music": 130,
    "música": 130,
    # Pets & Animals -> 217 (Pets Comprehensive)
    "pets & animals": 217,
    "animales": 217,
    "mascotas y animales": 217,
    # Sports -> 234 (Sports Comprehensive)
    "sports": 234,
    "deportes": 234,
    # Travel & Events -> 21 (Daily Life / Travel)
    "travel & events": 21,
    "viajes y eventos": 21,
    # People & Blogs (Partition 160) -> 21 (Daily Life)
    "people & blogs": 21,
    "gente y blogs": 21,
    # Comedy -> 138 (Comedy)
    "comedy": 138,
    "comedia": 138,
    "humor": 138,
    # Entertainment -> 71 (Entertainment Comprehensive)
    "entertainment": 71,
    "entretenimiento": 71,
    # News & Politics -> 204 (Hot Topics / News)
    "news & politics": 204,
    "noticias y política": 204,
    # Howto & Style -> 161 (Crafts & Style)
    "howto & style": 161,
    "consejos y estilo": 161,
    "bricolaje": 161,
    # Nonprofits & Activism -> 21 (Daily Life)
    "nonprofits & activism": 21,
    "ong y activismo": 21,
}


class BilibiliUploader:
    def __init__(self, config: dict):
        self.default_tid = config.get("tid", 17)
        self.copyright = config.get("copyright", 2)
        self.tags = config.get("tags", "搬运,翻译,中文字幕")
        self.cookie_file = config.get("cookie_file", "/app/data/cookies.json")
        self.lines = config.get("lines", "AUTO")
        self.threads = config.get("threads", 3)

        # Merge user custom category mappings from config
        self.category_mapping = dict(DEFAULT_CATEGORY_MAPPING)
        custom_mapping = config.get("category_mapping", {})
        if isinstance(custom_mapping, dict):
            for k, v in custom_mapping.items():
                self.category_mapping[str(k).strip().lower()] = int(v)

    def _resolve_cookie_file(self) -> str | None:
        """Find the active Bilibili cookies file across standard paths."""
        candidates = [
            self.cookie_file,
            "/app/data/cookies.json",
            "/app/cookies.json",
            "./data/cookies.json",
            "./cookies.json",
        ]
        for path in candidates:
            if path and os.path.exists(path) and os.path.isfile(path) and os.path.getsize(path) > 0:
                return path
        return None

    def resolve_tid(self, yt_category: str = "", tid_override: int | None = None) -> int:
        """
        Determine the appropriate Bilibili TID partition:
        1. Channel-specific override (highest priority if configured)
        2. Direct mapping from YouTube video category
        3. Default TID fallback from config
        """
        if tid_override is not None:
            logger.info(f"Using channel TID override: {tid_override}")
            return int(tid_override)

        if yt_category:
            normalized = yt_category.strip().lower()
            if normalized in self.category_mapping:
                matched_tid = self.category_mapping[normalized]
                logger.info(
                    f"Matched YouTube category '{yt_category}' -> Bilibili TID {matched_tid}"
                )
                return matched_tid
            else:
                logger.warning(
                    f"YouTube category '{yt_category}' has no direct mapping. "
                    f"Falling back to default TID {self.default_tid}"
                )

        return self.default_tid

    @staticmethod
    def clean_title(title: str) -> str:
        """
        Sanitize video title for Bilibili:
        - Remove hashtags (e.g. #shorts, #漫威, #蜘蛛侠)
        - Remove 4-byte Unicode emojis (astral plane characters >= U+10000)
        - Remove common Unicode emoji & symbols in BMP (U+2600-U+27BF, variation selectors)
        - Collapse multiple whitespace and trim
        """
        if not title:
            return ""
        cleaned = re.sub(r"#\S+", "", title)
        cleaned = re.sub(r"[\U00010000-\U0010FFFF]", "", cleaned)
        cleaned = re.sub(r"[\u2600-\u27BF\uFE00-\uFE0F]", "", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned if cleaned else title.strip()

    def upload(
        self,
        video_path: str,
        title: str,
        description: str,
        source_url: str = "",
        cover_path: str | None = None,
        yt_category: str = "",
        tid_override: int | None = None,
        draft: bool = False,
        tags: list[str] | str | None = None,
    ) -> bool:
        """
        Upload a video to Bilibili using biliup's Python API directly.

        Args:
            video_path: Path to the video file
            title: Video title (max 80 chars)
            description: Video description (max 2000 chars)
            source_url: Original source URL (required for copyright=2/reprint)
            cover_path: Optional path to cover image
            yt_category: Original YouTube category string (e.g. 'Gaming', 'Science & Technology')
            tid_override: Optional channel-specific TID override
            draft: If True, schedule publication 7 days ahead (unlisted draft in Creator Center)
            tags: Video-specific tags (translated from YouTube tags)

        Returns:
            True if upload succeeded, False otherwise.
        """
        if not os.path.exists(video_path):
            logger.error(f"Video file not found: {video_path}")
            return False

        cookie_path = self._resolve_cookie_file()
        if not cookie_path:
            logger.error(
                f"Cookie file not found. Checked: {self.cookie_file}, /app/data/cookies.json, /app/cookies.json. "
                f"Run 'docker compose run --rm yt2bili biliup login' first to authenticate."
            )
            return False

        # Resolve category TID
        tid = self.resolve_tid(yt_category=yt_category, tid_override=tid_override)

        # Clean title for Bilibili: remove hashtags and emojis
        title = self.clean_title(title)

        if draft and not title.startswith("【TEST/草稿】"):
            title = f"【TEST/草稿】{title}"

        # Truncate fields to Bilibili limits
        title = title[:80]
        description = description[:2000]

        final_tags = self._parse_tags(tags)

        logger.info(f"Uploading to Bilibili{' [DRAFT MODE]' if draft else ''}:")
        logger.info(f"  Title: {title}")
        logger.info(f"  TID: {tid} (YouTube Category: '{yt_category or 'N/A'}')")
        logger.info(f"  Tags: {', '.join(final_tags)}")
        logger.info(f"  Copyright: {self.copyright}")
        logger.info(f"  File: {video_path} ({os.path.getsize(video_path) / 1e6:.1f}MB)")
        logger.info(f"  Line: {self.lines}, Threads: {self.threads}")
        logger.info(f"  Auth: using {cookie_path}")

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
            video.tid = tid
            video.set_tag(final_tags)

            if draft:
                # Schedule publication 7 days in the future (unlisted draft in creator center)
                video.dtime = int(time.time()) + (7 * 86400)
                logger.info("  Draft mode: publication scheduled 7 days ahead (saved as unlisted draft)")

            # Initialize the uploader with cookie-based auth
            with BiliBili(video) as bili:
                bili.login(cookie_path, cookie_path)

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
                try:
                    if hasattr(bili, "_BiliBili__session"):
                        bili._BiliBili__session.get("https://member.bilibili.com/x/geetest/pre/add", timeout=5)
                except Exception:
                    pass
                ret = bili.submit_web()

            # Check result
            if isinstance(ret, dict):
                code = ret.get("code", -1)
                if code == 0:
                    bvid = ret.get("data", {}).get("bvid", "unknown")
                    aid = ret.get("data", {}).get("aid", "unknown")
                    logger.info(f"✅ Upload successful! BV: {bvid}, AV: {aid}")
                    return True
                else:
                    message = ret.get("message", "")
                    logger.error(f"❌ Bilibili rejected submission: code={code}, msg='{message}', details={ret}")
                    return False
            else:
                logger.error(f"❌ Unexpected response from Bilibili: {ret}")
                return False

        except FileNotFoundError:
            logger.error(
                "biliup package not found. Install it: pip install biliup"
            )
            return False
        except Exception as e:
            logger.exception(f"Upload error: {e}")
            return False

    def _parse_tags(self, extra_tags: list[str] | str | None = None) -> list[str]:
        """
        Merge base channel tags with extra tags (from YouTube translation).
        Returns up to 12 deduplicated tags with max 20 chars per tag.
        """
        base = self.tags if isinstance(self.tags, list) else [t.strip() for t in str(self.tags).split(",") if t.strip()]

        extras = []
        if isinstance(extra_tags, list):
            extras = [t.strip() for t in extra_tags if t.strip()]
        elif isinstance(extra_tags, str) and extra_tags.strip():
            extras = [t.strip() for t in re.split(r"[,，、]+", extra_tags) if t.strip()]

        # Combine: extra tags first (specific to video), followed by base tags (e.g. 原创, 翻译, 中文字幕)
        combined = []
        for tag in extras + base:
            clean = re.sub(r"[#\"'“”]", "", tag).strip()
            if clean and len(clean) <= 20 and clean not in combined:
                combined.append(clean)

        return combined[:10]

    def check_auth(self) -> bool:
        """Verify that the cookie file exists and is loadable."""
        cookie_path = self._resolve_cookie_file()
        if not cookie_path:
            logger.error(
                f"Cookie file not found. Checked: {self.cookie_file}, /app/data/cookies.json, /app/cookies.json"
            )
            return False

        try:
            video = Data()
            with BiliBili(video) as bili:
                bili.login(cookie_path, cookie_path)
            logger.info(f"Bilibili authentication valid (using {cookie_path})")
            return True
        except Exception as e:
            logger.warning(f"Auth check failed: {e}")
            return False
