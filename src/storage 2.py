from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from .utils import normalize_url

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 url TEXT UNIQUE NOT NULL,
 original_url TEXT,
 title_original TEXT NOT NULL,
 title_cn TEXT, source_id TEXT, source_name TEXT, publisher TEXT, author TEXT, layer TEXT,
 published_at TEXT, updated_at TEXT, rss_published_at TEXT, date_source TEXT,
 crawled_at TEXT, archived_at TEXT DEFAULT CURRENT_TIMESTAMP,
 rss_summary TEXT, article_text TEXT, summary_cn TEXT, main_image TEXT,
 score INTEGER, score_reason TEXT, industry_tags TEXT, companies TEXT, worth_attention INTEGER,
 archive_file TEXT, fetch_status TEXT, fetch_error TEXT,
 ai_status TEXT, ai_model TEXT, ai_error TEXT, link_resolver TEXT
);
"""

MIGRATIONS = {
    "original_url": "TEXT", "title_cn": "TEXT", "source_id": "TEXT", "source_name": "TEXT",
    "publisher": "TEXT", "author": "TEXT", "layer": "TEXT", "published_at": "TEXT",
    "updated_at": "TEXT", "rss_published_at": "TEXT", "date_source": "TEXT",
    "crawled_at": "TEXT", "archived_at": "TEXT", "rss_summary": "TEXT",
    "article_text": "TEXT", "summary_cn": "TEXT", "main_image": "TEXT", "score": "INTEGER",
    "score_reason": "TEXT", "industry_tags": "TEXT", "companies": "TEXT",
    "worth_attention": "INTEGER", "archive_file": "TEXT", "fetch_status": "TEXT",
    "fetch_error": "TEXT", "ai_status": "TEXT", "ai_model": "TEXT", "ai_error": "TEXT", "link_resolver": "TEXT",
}


class Storage:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        existing = {row[1] for row in self.conn.execute("PRAGMA table_info(articles)")}
        legacy_date_schema = "date_source" not in existing
        for name, kind in MIGRATIONS.items():
            if name not in existing:
                self.conn.execute(f"ALTER TABLE articles ADD COLUMN {name} {kind}")
        if legacy_date_schema:
            # V3 stored the feed timestamp in published_at. Preserve it as RSS date and
            # require webpage verification before calling anything the true publication date.
            self.conn.execute("UPDATE articles SET rss_published_at=published_at, published_at='', date_source='rss' WHERE COALESCE(published_at,'')<>''")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_score ON articles(score)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_published ON articles(published_at)")
        self.conn.commit()

    def exists_url(self, url):
        value = normalize_url(url)
        return self.conn.execute(
            "SELECT 1 FROM articles WHERE url=? OR original_url=?", (value, value)
        ).fetchone() is not None

    def similar_title(self, title, threshold):
        rows = self.conn.execute("SELECT title_original FROM articles ORDER BY id DESC LIMIT 500").fetchall()
        target = (title or "").lower().strip()
        return any(
            SequenceMatcher(None, target, (row["title_original"] or "").lower().strip()).ratio() >= threshold
            for row in rows
        )

    def save(self, article):
        fields = [
            "url", "original_url", "title_original", "title_cn", "source_id", "source_name",
            "publisher", "author", "layer", "published_at", "updated_at", "rss_published_at",
            "date_source", "crawled_at", "rss_summary", "article_text", "summary_cn", "main_image",
            "score", "score_reason", "industry_tags", "companies", "worth_attention", "archive_file",
            "fetch_status", "fetch_error", "ai_status", "ai_model", "ai_error", "link_resolver",
        ]
        values = []
        for field in fields:
            value = article.get(field, "")
            if field in {"industry_tags", "companies"}:
                value = json.dumps(value, ensure_ascii=False)
            if field == "worth_attention":
                value = 1 if value else 0
            if field in {"url", "original_url"} and value:
                value = normalize_url(value)
            values.append(value)
        self.conn.execute(
            f"INSERT OR IGNORE INTO articles({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
            values,
        )
        self.conn.commit()

    def update_archive_file(self, article_id: int, archive_file: str):
        self.conn.execute("UPDATE articles SET archive_file=? WHERE id=?", (archive_file, article_id))
        self.conn.commit()


    def update_ai_result(self, article_id: int, result: dict):
        fields = [
            "title_cn", "summary_cn", "score", "score_reason", "industry_tags",
            "companies", "worth_attention", "ai_status", "ai_model", "ai_error",
        ]
        values = []
        for field in fields:
            value = result.get(field, "")
            if field in {"industry_tags", "companies"}:
                value = json.dumps(value, ensure_ascii=False)
            if field == "worth_attention":
                value = 1 if value else 0
            values.append(value)
        values.append(article_id)
        self.conn.execute(
            f"UPDATE articles SET {','.join(name + '=?' for name in fields)} WHERE id=?",
            values,
        )
        self.conn.commit()

    def update_metadata(self, article_id: int, metadata: dict):
        allowed = ["url", "publisher", "author", "published_at", "updated_at", "date_source", "main_image", "article_text", "fetch_status", "fetch_error", "crawled_at", "link_resolver"]
        fields = [name for name in allowed if name in metadata]
        if not fields:
            return
        values = [normalize_url(metadata[name]) if name == "url" and metadata[name] else metadata[name] for name in fields]
        values.append(article_id)
        self.conn.execute(
            f"UPDATE articles SET {','.join(name + '=?' for name in fields)} WHERE id=?", values
        )
        self.conn.commit()

    def all(self):
        rows = self.conn.execute(
            "SELECT * FROM articles ORDER BY COALESCE(NULLIF(published_at,''), NULLIF(rss_published_at,''), archived_at) DESC, score DESC"
        ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["industry_tags"] = json.loads(item.get("industry_tags") or "[]")
            item["companies"] = json.loads(item.get("companies") or "[]")
            item["worth_attention"] = bool(item.get("worth_attention"))
            # Compatibility for databases created before V4.
            if not item.get("crawled_at"):
                item["crawled_at"] = item.get("archived_at") or ""
            if not item.get("original_url"):
                item["original_url"] = item.get("url") or ""
            output.append(item)
        return output

    def close(self):
        self.conn.close()
