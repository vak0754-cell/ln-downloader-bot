from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DownloadStore:
    """SQLite cache for downloaded chapters (enables instant resume & incremental updates) and history."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS chapter_cache (
                    url_hash TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    body_html TEXT NOT NULL,
                    fetched_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    source_url TEXT NOT NULL,
                    book_title TEXT NOT NULL,
                    chapters_count INTEGER DEFAULT 0,
                    output_file TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _hash(url: str) -> str:
        return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()

    def get_cached_chapter(self, url: str) -> dict[str, str] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT title, body_html FROM chapter_cache WHERE url_hash = ?", (self._hash(url),)).fetchone()
            return dict(row) if row else None

    def save_cached_chapter(self, url: str, title: str, body_html: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO chapter_cache (url_hash, url, title, body_html, fetched_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (self._hash(url), url, title, body_html, _utc_now()),
            )

    def add_history(self, user_id: int, source_url: str, book_title: str, chapters_count: int, output_file: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO history (user_id, source_url, book_title, chapters_count, output_file, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, source_url, book_title, chapters_count, output_file, _utc_now()),
            )

    def recent_history(self, user_id: int, limit: int = 10) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM history WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit)
            ).fetchall()
            return [dict(r) for r in rows]
