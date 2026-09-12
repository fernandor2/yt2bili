"""
YouTube channel monitor.
Fetches recent videos (including Shorts) with automatic fallback to yt-dlp flat playlist extraction
when YouTube's legacy RSS server returns 404 or invalid HTML.
"""

import logging
import requests
import feedparser
import time
import yt_dlp

from db import Database

logger = logging.getLogger("yt2bili.monitor")

RSS_BASE_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={}"


class YouTubeMonitor:
    def __init__(self, config: dict):
        self.channels = config.get("channels", [])
        self.db = Database(config["pipeline"]["db_path"])

    def check_all_channels(self) -> list[dict]:
        """
        Check all configured channels for new videos.
        Returns a list of video dicts not yet in the database.
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
                        # Attach channel-specific overrides
                        video["tid_override"] = channel.get("tid")
                        new_videos.append(video)
                        # Mark as pending
                        self.db.mark_pending(vid, channel_id, video["title"])

                if new_videos:
                    logger.info(
                        f"[{channel_name}] {len(new_videos)} new video(s) found"
                    )
                else:
                    logger.info(f"[{channel_name}] No new videos")

                all_new.extend(new_videos)

            except Exception as e:
                logger.error(f"Error checking channel {channel_name}: {e}")

            # Brief pause between channel checks
            time.sleep(2)

        return all_new

    def _fetch_channel_videos(self, channel_id: str, channel_name: str) -> list[dict]:
        """
        Attempt to fetch recent uploads via RSS feed.
        If YouTube's RSS server returns 404 or invalid HTML, automatically fall back to
        yt-dlp flat extraction (Innertube API).
        """
        url = RSS_BASE_URL.format(channel_id)
        logger.info(f"Checking channel [{channel_name}]...")

        try:
            resp = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                timeout=10,
            )

            # Check if RSS actually returned a valid XML feed and not Google's HTML 404 page
            if resp.status_code == 200 and (b"<?xml" in resp.content[:100] or b"<feed" in resp.content[:200]):
                feed = feedparser.parse(resp.content)
                if feed.entries:
                    videos = []
                    for entry in feed.entries:
                        video_id = entry.get("yt_videoid", "")
                        if not video_id:
                            continue
                        videos.append({
                            "video_id": video_id,
                            "channel_id": channel_id,
                            "channel_name": channel_name,
                            "title": entry.get("title", ""),
                            "url": f"https://www.youtube.com/watch?v={video_id}",
                            "published": entry.get("published", ""),
                            "description": _get_description(entry),
                            "thumbnail": _get_thumbnail(entry),
                        })
                    logger.info(f"[{channel_name}] Found {len(videos)} video(s) via RSS")
                    return videos

            logger.info(
                f"[{channel_name}] YouTube RSS feed returned status {resp.status_code}. "
                f"Scanning channel directly via yt-dlp..."
            )

        except Exception as e:
            logger.info(f"[{channel_name}] RSS unavailable ({e}). Scanning via yt-dlp...")

        # ── Bulletproof Fallback: yt-dlp Flat Playlist Extraction ──
        return self._fetch_via_ytdlp(channel_id, channel_name)

    def _fetch_via_ytdlp(self, channel_id: str, channel_name: str) -> list[dict]:
        """Extract the latest regular videos AND Shorts directly from the channel page using yt-dlp."""
        endpoints = [
            (f"https://www.youtube.com/channel/{channel_id}/videos", "videos"),
            (f"https://www.youtube.com/channel/{channel_id}/shorts", "shorts"),
        ]

        ydl_opts = {
            "extract_flat": "in_playlist",
            "playlist_end": 15,
            "quiet": True,
            "no_warnings": True,
        }

        videos = []
        seen_ids = set()

        for url, kind in endpoints:
            logger.info(f"Scanning {kind}: {url}...")
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    res = ydl.extract_info(url, download=False)
                    entries = res.get("entries", []) if res else []

                    for entry in entries:
                        if not entry:
                            continue
                        video_id = entry.get("id")
                        if not video_id or video_id in seen_ids:
                            continue
                        seen_ids.add(video_id)
                        videos.append({
                            "video_id": video_id,
                            "channel_id": channel_id,
                            "channel_name": channel_name,
                            "title": entry.get("title", ""),
                            "url": f"https://www.youtube.com/watch?v={video_id}",
                            "published": "",
                            "description": entry.get("description", ""),
                            "thumbnail": entry.get("thumbnails", [{}])[-1].get("url", "") if entry.get("thumbnails") else "",
                            "is_short": (kind == "shorts"),
                        })
            except Exception as e:
                logger.warning(f"[{channel_name}] Failed to scan {kind} tab ({url}): {e}")

        logger.info(f"[{channel_name}] Total found: {len(videos)} video(s) and Shorts via yt-dlp")
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
