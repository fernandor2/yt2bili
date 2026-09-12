"""
Database module - SQLite-based tracking of processed videos and upload rate limiting.
Prevents re-processing, tracks failures for retry logic, and enforces upload cooldowns.
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
                CREATE TABLE IF NOT EXISTS metadata (
                    key   TEXT PRIMARY KEY,
                    value TEXT
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
                    status = CASE WHEN status = 'done' THEN 'done' ELSE 'pending' END,
                    updated_at = datetime('now')
                """,
                (video_id, channel_id, title),
            )
            conn.commit()

    def get_pending_videos(self, limit: int = 20) -> list[dict]:
        """Fetch pending videos ordered by oldest discovered first."""
        with self._get_conn() as conn:
            rows = conn.execute(
                """
                SELECT video_id, channel_id, title, created_at
                FROM processed_videos
                WHERE status = 'pending'
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

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

    def set_metadata(self, key: str, value: str):
        """Set a persistent key-value metadata entry."""
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO metadata (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            conn.commit()

    def get_metadata(self, key: str) -> str | None:
        """Get a persistent key-value metadata entry."""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT value FROM metadata WHERE key = ?",
                (key,),
            ).fetchone()
            return row["value"] if row else None

    def record_upload_success(self, video_id: str):
        """Mark video as done and record the upload timestamp for cooldown calculation."""
        self.mark_done(video_id)
        now_str = datetime.utcnow().isoformat()
        self.set_metadata("last_upload_time", now_str)
        logger.info(f"Recorded last upload timestamp: {now_str}")

    def get_seconds_since_last_upload(self) -> float | None:
        """Calculate how many seconds have elapsed since the most recent upload."""
        val = self.get_metadata("last_upload_time")
        if not val:
            return None
        try:
            last_dt = datetime.fromisoformat(val)
            return (datetime.utcnow() - last_dt).total_seconds()
        except Exception:
            return None

    def get_stats(self) -> dict:
        """Get processing statistics."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) as count FROM processed_videos GROUP BY status"
            ).fetchall()
            return {row["status"]: row["count"] for row in rows}
