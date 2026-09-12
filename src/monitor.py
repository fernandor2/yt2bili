"""
YouTube channel monitor.
Fetches all videos and Shorts uploaded within the configured lookback window (default: 90 days),
merging RSS feeds with yt-dlp flat playlist extraction to queue up candidates in SQLite.
"""

import logging
import re
import time
from datetime import datetime, timedelta
import feedparser
import requests
import yt_dlp

from db import Database

logger = logging.getLogger("yt2bili.monitor")

RSS_BASE_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={}"


class YouTubeMonitor:
    def __init__(self, config: dict):
        self.config = config
        self.channels = config.get("channels", [])
        self.db = Database(config["pipeline"]["db_path"])
        self.max_history_days = config.get("pipeline", {}).get("max_history_days", 90)

    def check_all_channels(self) -> list[dict]:
        """
        Check all configured channels for videos/shorts from the last N days (default 90).
        Discovers new videos and records them in SQLite as 'pending'.
        Returns list of newly discovered videos.
        """
        all_new = []
        all_valid_ids = set()

        for channel in self.channels:
            channel_id = channel["channel_id"]
            channel_name = channel.get("name", channel_id)

            try:
                videos = self._fetch_channel_videos(channel_id, channel_name)
                new_videos = []

                for video in videos:
                    vid = video["video_id"]
                    all_valid_ids.add(vid)
                    if not self.db.is_processed(vid):
                        video["tid_override"] = channel.get("tid")
                        new_videos.append(video)
                        # Record in SQLite queue as pending
                        self.db.mark_pending(
                            vid,
                            channel_id,
                            video["title"],
                            channel_name=channel_name,
                            upload_date=video.get("upload_date", ""),
                        )

                if new_videos:
                    logger.info(
                        f"[{channel_name}] Found {len(new_videos)} candidate video(s) within the last {self.max_history_days} days"
                    )
                else:
                    logger.info(f"[{channel_name}] No new videos to queue (all processed or up to date)")

                all_new.extend(new_videos)

            except Exception as e:
                logger.error(f"Error checking channel {channel_name}: {e}")

            time.sleep(2)

        # Purge any stale pending videos that were queued outside the 90-day window
        self.db.purge_stale_pending(all_valid_ids)

        return all_new

    def _fetch_channel_videos(self, channel_id: str, channel_name: str) -> list[dict]:
        """
        Fetch all regular videos and Shorts from the last N days.
        Combines RSS feed (fresh items) and yt-dlp tab extraction (/videos + /shorts).
        """
        seen_ids = set()
        videos = []

        # 1. Quick RSS scan (up to 15 recent items with full descriptions)
        rss_videos = self._fetch_via_rss(channel_id, channel_name)
        for v in rss_videos:
            seen_ids.add(v["video_id"])
            videos.append(v)

        # 2. Comprehensive yt-dlp scan across /videos and /shorts for 90-day archive
        ytdlp_videos = self._fetch_via_ytdlp(channel_id, channel_name, seen_ids)
        videos.extend(ytdlp_videos)

        logger.info(
            f"[{channel_name}] Total candidate videos & shorts from last {self.max_history_days} days: {len(videos)}"
        )
        return videos

    def _fetch_via_rss(self, channel_id: str, channel_name: str) -> list[dict]:
        """Attempt to fetch recent uploads via RSS feed."""
        url = RSS_BASE_URL.format(channel_id)
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                timeout=10,
            )
            if resp.status_code == 200 and (b"<?xml" in resp.content[:100] or b"<feed" in resp.content[:200]):
                feed = feedparser.parse(resp.content)
                if feed.entries:
                    cutoff_dt = datetime.utcnow() - timedelta(days=self.max_history_days)
                    results = []
                    for entry in feed.entries:
                        video_id = entry.get("yt_videoid", "")
                        if not video_id:
                            continue

                        # Check published date if available
                        pub_str = entry.get("published", "")
                        upload_date_str = ""
                        if pub_str:
                            try:
                                dt = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
                                if dt.replace(tzinfo=None) < cutoff_dt:
                                    continue
                                upload_date_str = dt.strftime("%Y%m%d")
                            except Exception:
                                pass

                        results.append({
                            "video_id": video_id,
                            "channel_id": channel_id,
                            "channel_name": channel_name,
                            "title": entry.get("title", ""),
                            "url": f"https://www.youtube.com/watch?v={video_id}",
                            "published": pub_str,
                            "upload_date": upload_date_str,
                            "description": _get_description(entry),
                            "thumbnail": _get_thumbnail(entry),
                        })
                    return results
        except Exception as e:
            logger.debug(f"[{channel_name}] RSS fetch note: {e}")
        return []

    def _get_video_upload_date(self, video_id: str) -> tuple[str | None, str]:
        """Fetch upload_date (YYYYMMDD) and title for a video, checking DB first, then yt-dlp metadata."""
        cached_date = self.db.get_upload_date(video_id)
        if cached_date:
            return cached_date, ""

        class QuietLogger:
            def debug(self, msg): pass
            def info(self, msg): pass
            def warning(self, msg): pass
            def error(self, msg): pass

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "logger": QuietLogger(),
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
                if info:
                    ud = info.get("upload_date")
                    title = info.get("title") or ""
                    if ud:
                        ud_str = str(ud)
                        self.db.set_upload_date(video_id, ud_str, title=title)
                        return ud_str, title
        except Exception as e:
            logger.debug(f"Could not extract upload date for {video_id}: {e}")
        return None, ""

    def _fetch_via_ytdlp(self, channel_id: str, channel_name: str, seen_ids: set) -> list[dict]:
        """Extract regular videos and Shorts directly from the channel tabs up to max_history_days old."""
        endpoints = [
            (f"https://www.youtube.com/channel/{channel_id}/videos", "videos"),
            (f"https://www.youtube.com/channel/{channel_id}/shorts", "shorts"),
        ]

        ydl_opts = {
            "extract_flat": "in_playlist",
            "playlist_end": 60,  # Prevent runaway scraping beyond reasonable 90-day window
            "quiet": True,
            "no_warnings": True,
        }

        cutoff_ts = time.time() - (self.max_history_days * 86400)
        cutoff_date = (datetime.utcnow() - timedelta(days=self.max_history_days)).strftime("%Y%m%d")

        videos = []

        for url, kind in endpoints:
            logger.info(f"Scanning {kind} tab for [{channel_name}] (up to {self.max_history_days} days)...")
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    res = ydl.extract_info(url, download=False)
                    entries = res.get("entries", []) if res else []

                    for entry in entries:
                        if not entry:
                            continue
                        video_id = entry.get("id")
                        if not video_id:
                            continue

                        # Check timestamp if present
                        ts = entry.get("timestamp")
                        if ts and ts < cutoff_ts:
                            logger.info(f"  [{channel_name}] Reached {kind} older than {self.max_history_days} days (timestamp). Ending tab scan.")
                            break

                        # Check relative text if present
                        raw_pub = str(entry.get("published_time") or entry.get("published") or "").lower()
                        if "year" in raw_pub or "año" in raw_pub:
                            logger.info(f"  [{channel_name}] Reached {kind} from last year ({raw_pub}). Ending tab scan.")
                            break
                        month_match = re.search(r"(\d+)\s*(?:month|mes)", raw_pub)
                        if month_match and int(month_match.group(1)) > (self.max_history_days // 30):
                            logger.info(f"  [{channel_name}] Reached {kind} {raw_pub}. Ending tab scan.")
                            break

                        # Check or fetch upload date and title
                        ud = entry.get("upload_date")
                        extracted_title = ""
                        if not ud and video_id not in seen_ids:
                            ud, extracted_title = self._get_video_upload_date(video_id)

                        if ud:
                            if str(ud) < cutoff_date:
                                logger.info(
                                    f"  [{channel_name}] Reached {kind} ({video_id}) uploaded on {ud} "
                                    f"(older than {self.max_history_days} days). Ending tab scan."
                                )
                                break

                        if video_id in seen_ids:
                            continue
                        seen_ids.add(video_id)

                        item_title = entry.get("title") or extracted_title or ""

                        videos.append({
                            "video_id": video_id,
                            "channel_id": channel_id,
                            "channel_name": channel_name,
                            "title": item_title,
                            "url": f"https://www.youtube.com/watch?v={video_id}",
                            "published": str(ud or entry.get("upload_date") or ""),
                            "upload_date": str(ud or ""),
                            "description": entry.get("description", ""),
                            "thumbnail": entry.get("thumbnails", [{}])[-1].get("url", "") if entry.get("thumbnails") else "",
                            "is_short": (kind == "shorts"),
                        })

            except Exception as e:
                logger.warning(f"[{channel_name}] Could not scan {kind} tab ({url}): {e}")

        return videos


def _get_description(entry) -> str:
    """Extract description from RSS entry's media:group."""
    try:
        if hasattr(entry, "media_description"):
            return entry.media_description
        return entry.get("summary", "")
    except Exception:
        return ""


def _get_thumbnail(entry) -> str:
    """Extract thumbnail URL from RSS entry."""
    try:
        if hasattr(entry, "media_thumbnail") and entry.media_thumbnail:
            return entry.media_thumbnail[0].get("url", "")
        video_id = entry.get("yt_videoid", "")
        if video_id:
            return f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"
    except Exception:
        pass
    return ""
