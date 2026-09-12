"""
Database module - SQLite-based tracking of processed videos.
Prevents re-processing and tracks failures for retry logic.
"""

import sqlite3
import os
import logging
from datetime import datetime, timedelta

logger = logging.getLogger("yt2bili.db")


class Database:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db_path = db_path
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        """Create tables if they don't exist."""
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS processed_videos (
                    video_id    TEXT PRIMARY KEY,
                    channel_id  TEXT,
                    title       TEXT,
                    status      TEXT DEFAULT 'pending',
                    error_msg   TEXT,
                    created_at  TEXT DEFAULT (datetime('now')),
                    updated_at  TEXT DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_status
                ON processed_videos(status)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_channel
                ON processed_videos(channel_id)
            """)
            conn.commit()
        logger.info(f"Database initialized at {self.db_path}")

    def is_processed(self, video_id: str) -> bool:
        """Check if a video has been successfully processed."""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT status FROM processed_videos WHERE video_id = ?",
                (video_id,),
            ).fetchone()
            return row is not None and row["status"] == "done"

    def is_recently_failed(self, video_id: str, hours: int = 24) -> bool:
        """Check if a video failed processing within the last N hours."""
        cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT status, updated_at FROM processed_videos "
                "WHERE video_id = ? AND status = 'failed' AND updated_at > ?",
                (video_id, cutoff),
            ).fetchone()
            return row is not None

    def mark_pending(self, video_id: str, channel_id: str, title: str):
        """Record a video as pending processing."""
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO processed_videos (video_id, channel_id, title, status)
                VALUES (?, ?, ?, 'pending')
                ON CONFLICT(video_id) DO UPDATE SET
                    status = 'pending',
                    updated_at = datetime('now')
                """,
                (video_id, channel_id, title),
            )
            conn.commit()

    def mark_done(self, video_id: str):
        """Mark a video as successfully processed."""
        with self._get_conn() as conn:
            conn.execute(
                """
                UPDATE processed_videos
                SET status = 'done', error_msg = NULL, updated_at = datetime('now')
                WHERE video_id = ?
                """,
                (video_id,),
            )
            conn.commit()
        logger.info(f"Marked {video_id} as done")

    def mark_failed(self, video_id: str, error_msg: str):
        """Mark a video as failed with an error message."""
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO processed_videos (video_id, status, error_msg)
                VALUES (?, 'failed', ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    status = 'failed',
                    error_msg = ?,
                    updated_at = datetime('now')
                """,
                (video_id, error_msg, error_msg),
            )
            conn.commit()
        logger.warning(f"Marked {video_id} as failed: {error_msg}")

    def get_stats(self) -> dict:
        """Get processing statistics."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) as count FROM processed_videos GROUP BY status"
            ).fetchall()
            return {row["status"]: row["count"] for row in rows}
