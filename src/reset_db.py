"""
Database reset utility for yt2bili.
Allows wiping processed videos, pending queue, and upload cooldown from SQLite,
enabling full re-processing and re-upload of YouTube channels.
"""

import argparse
import os
import shutil
import sqlite3
import yaml


def load_config() -> dict:
    """Load config.yml from standard paths."""
    config_paths = [
        "/app/config.yml",
        os.path.join(os.path.dirname(__file__), "..", "config.yml"),
        "config.yml",
    ]
    for path in config_paths:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f)
    return {}


def reset_database(mode: str = "all", keep_downloads: bool = False):
    config = load_config()
    db_path = config.get("pipeline", {}).get("db_path", "/app/data/db/processed.db")
    dl_dir = config.get("pipeline", {}).get("download_dir", "/app/data/downloads")

    print("=========================================")
    print("=== yt2bili Database Reset Utility ===")
    print("=========================================")
    print(f"Database location: {db_path}")

    if not os.path.exists(db_path):
        print("Database file does not exist yet. Nothing to reset.")
        print("=========================================")
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Get current statistics
    try:
        rows = conn.execute("SELECT status, COUNT(*) as count FROM processed_videos GROUP BY status").fetchall()
        stats = {r["status"]: r["count"] for r in rows}
        total = sum(stats.values())
        print(f"Current records in database: {total} total")
        for st, cnt in stats.items():
            print(f"  - {st}: {cnt}")
    except Exception:
        print("No existing processed_videos table found.")
        stats = {}

    last_upload = None
    try:
        row = conn.execute("SELECT value FROM metadata WHERE key = 'last_upload_time'").fetchone()
        if row:
            last_upload = row["value"]
            print(f"Last recorded upload time: {last_upload}")
    except Exception:
        pass

    print("-----------------------------------------")

    if mode == "all":
        # Delete all records from processed_videos
        try:
            conn.execute("DELETE FROM processed_videos")
            conn.execute("DELETE FROM metadata WHERE key = 'last_upload_time'")
            conn.commit()
            print("🗑️  Cleared all records from 'processed_videos'.")
            print("⏱️  Reset 'last_upload_time' cooldown (immediate upload ready).")
        except Exception as e:
            print(f"Error resetting tables: {e}")

    elif mode == "failed":
        try:
            cur = conn.execute("DELETE FROM processed_videos WHERE status = 'failed'")
            conn.commit()
            print(f"🗑️  Cleared {cur.rowcount} failed record(s). They will be retried on next run.")
        except Exception as e:
            print(f"Error clearing failed: {e}")

    elif mode == "pending":
        try:
            cur = conn.execute("DELETE FROM processed_videos WHERE status = 'pending'")
            conn.commit()
            print(f"🗑️  Cleared {cur.rowcount} pending record(s) from queue.")
        except Exception as e:
            print(f"Error clearing pending: {e}")

    elif mode == "cooldown":
        try:
            conn.execute("DELETE FROM metadata WHERE key = 'last_upload_time'")
            conn.commit()
            print("⏱️  Reset 'last_upload_time' cooldown only.")
        except Exception as e:
            print(f"Error clearing cooldown: {e}")

    conn.close()

    # Clean leftover files in download directory if requested
    if mode == "all" and not keep_downloads and os.path.exists(dl_dir):
        cleaned = 0
        for item in os.listdir(dl_dir):
            item_path = os.path.join(dl_dir, item)
            try:
                if os.path.isdir(item_path):
                    shutil.rmtree(item_path, ignore_errors=True)
                    cleaned += 1
                elif os.path.isfile(item_path):
                    os.remove(item_path)
                    cleaned += 1
            except Exception:
                pass
        if cleaned > 0:
            print(f"📁 Cleaned {cleaned} temporary directory/file(s) in {dl_dir}")

    print("=========================================")
    print("✅ RESET COMPLETE!")
    if mode == "all":
        print("All previously processed videos have been forgotten.")
        print("On the next scheduled check, yt2bili will scan channels,")
        print("discover all videos from the last 90 days as brand new,")
        print("and process/upload them 1 by 1.")
    print("=========================================")


def main():
    parser = argparse.ArgumentParser(description="yt2bili SQLite Database Reset Utility")
    parser.add_argument(
        "--failed-only",
        action="store_true",
        help="Only clear failed videos (retries them without deleting done videos)",
    )
    parser.add_argument(
        "--pending-only",
        action="store_true",
        help="Only clear pending videos from the queue",
    )
    parser.add_argument(
        "--cooldown-only",
        action="store_true",
        help="Only clear the upload cooldown timestamp",
    )
    parser.add_argument(
        "--keep-downloads",
        action="store_true",
        help="Do not delete existing files in data/downloads/",
    )

    args = parser.parse_args()

    mode = "all"
    if args.failed_only:
        mode = "failed"
    elif args.pending_only:
        mode = "pending"
    elif args.cooldown_only:
        mode = "cooldown"

    reset_database(mode=mode, keep_downloads=args.keep_downloads)


if __name__ == "__main__":
    main()
