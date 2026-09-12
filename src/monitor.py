"""
YouTube channel monitor via RSS feeds.
Checks for new videos (including Shorts) without any duration filtering.
"""

import logging
import feedparser
import time

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
                videos = self._fetch_channel_feed(channel_id, channel_name)
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

    def _fetch_channel_feed(self, channel_id: str, channel_name: str) -> list[dict]:
        """
        Fetch and parse YouTube RSS/Atom feed for a channel.
        Returns the 15 most recent uploads (both regular videos and Shorts).
        """
        url = RSS_BASE_URL.format(channel_id)
        logger.info(f"Fetching RSS feed: {url}")

        feed = feedparser.parse(url)

        if feed.bozo and not feed.entries:
            raise RuntimeError(
                f"Failed to parse feed for {channel_id}: {feed.bozo_exception}"
            )

        videos = []
        for entry in feed.entries:
            video_id = entry.get("yt_videoid", "")
            if not video_id:
                continue

            # Extract video metadata from Atom feed
            video = {
                "video_id": video_id,
                "channel_id": channel_id,
                "channel_name": channel_name,
                "title": entry.get("title", ""),
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "published": entry.get("published", ""),
                "updated": entry.get("updated", ""),
                "description": _get_description(entry),
                "thumbnail": _get_thumbnail(entry),
            }
            videos.append(video)

        logger.info(f"[{channel_name}] Parsed {len(videos)} entries from RSS")
        return videos


def _get_description(entry) -> str:
    """Extract description from RSS entry's media:group."""
    try:
        # feedparser stores media:group content in media_description
        if hasattr(entry, "media_description"):
            return entry.media_description
        # Fallback to summary
        return entry.get("summary", "")
    except Exception:
        return ""


def _get_thumbnail(entry) -> str:
    """Extract thumbnail URL from RSS entry."""
    try:
        if hasattr(entry, "media_thumbnail") and entry.media_thumbnail:
            return entry.media_thumbnail[0].get("url", "")
        # Fallback: standard YouTube thumbnail URL
        video_id = entry.get("yt_videoid", "")
        if video_id:
            return f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"
    except Exception:
        pass
    return ""
