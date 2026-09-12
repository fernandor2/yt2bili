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

        for channel in self.channels:
            channel_id = channel["channel_id"]
            channel_name = channel.get("name", channel_id)

            try:
                videos = self._fetch_channel_videos(channel_id, channel_name)
                new_videos = []

                for video in videos:
                    vid = video["video_id"]
                    if not self.db.is_processed(vid):
                        video["tid_override"] = channel.get("tid")
                        new_videos.append(video)
                        # Record in SQLite queue as pending
                        self.db.mark_pending(vid, channel_id, video["title"])

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
                        if pub_str:
                            try:
                                dt = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
                                if dt.replace(tzinfo=None) < cutoff_dt:
                                    continue
                            except Exception:
                                pass

                        results.append({
                            "video_id": video_id,
                            "channel_id": channel_id,
                            "channel_name": channel_name,
                            "title": entry.get("title", ""),
                            "url": f"https://www.youtube.com/watch?v={video_id}",
                            "published": pub_str,
                            "description": _get_description(entry),
                            "thumbnail": _get_thumbnail(entry),
                        })
                    return results
        except Exception as e:
            logger.debug(f"[{channel_name}] RSS fetch note: {e}")
        return []

    def _fetch_via_ytdlp(self, channel_id: str, channel_name: str, seen_ids: set) -> list[dict]:
        """Extract regular videos and Shorts directly from the channel tabs up to max_history_days old."""
        endpoints = [
            (f"https://www.youtube.com/channel/{channel_id}/videos", "videos"),
            (f"https://www.youtube.com/channel/{channel_id}/shorts", "shorts"),
        ]

        ydl_opts = {
            "extract_flat": "in_playlist",
            "playlist_end": 150,  # Deep enough to capture the full 90-day archive
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

                        # Check date boundaries to stop scanning when reaching older content
                        ts = entry.get("timestamp")
                        if ts and ts < cutoff_ts:
                            logger.info(f"  [{channel_name}] Reached {kind} older than {self.max_history_days} days. Ending tab scan.")
                            break

                        ud = entry.get("upload_date")
                        if ud and str(ud) < cutoff_date:
                            logger.info(f"  [{channel_name}] Reached {kind} date {ud} older than {self.max_history_days} days. Ending tab scan.")
                            break

                        raw_pub = str(entry.get("published_time") or entry.get("published") or "").lower()
                        if "year" in raw_pub or "año" in raw_pub:
                            logger.info(f"  [{channel_name}] Reached {kind} from last year ({raw_pub}). Ending tab scan.")
                            break
                        month_match = re.search(r"(\d+)\s*(?:month|mes)", raw_pub)
                        if month_match and int(month_match.group(1)) > (self.max_history_days // 30):
                            logger.info(f"  [{channel_name}] Reached {kind} {raw_pub}. Ending tab scan.")
                            break

                        if video_id in seen_ids:
                            continue
                        seen_ids.add(video_id)

                        videos.append({
                            "video_id": video_id,
                            "channel_id": channel_id,
                            "channel_name": channel_name,
                            "title": entry.get("title", ""),
                            "url": f"https://www.youtube.com/watch?v={video_id}",
                            "published": str(entry.get("upload_date") or ""),
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
