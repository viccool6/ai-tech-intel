from __future__ import annotations

import json
import sqlite3
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable

from .keywords import matched_keywords as compute_matched_keywords
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
 ai_status TEXT, ai_model TEXT, ai_error TEXT, link_resolver TEXT,
 matched_keywords TEXT
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
    "fetch_error": "TEXT", "ai_status": "TEXT", "ai_model": "TEXT", "ai_error": "TEXT",
    "link_resolver": "TEXT", "matched_keywords": "TEXT",
}

# Columns mirrored into the FTS5 shadow table. matched_keywords is included so
# the search box can hit监控关键词 as first-class content.
FTS_COLUMNS = [
    "title_cn", "title_original", "summary_cn", "article_text",
    "publisher", "source_name", "industry_tags", "companies", "matched_keywords",
]

FTS_SCHEMA = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5("
    + ", ".join(FTS_COLUMNS)
    + ", content='articles', content_rowid='id', tokenize='trigram');"
)

_FTS_COL_LIST = ", ".join(FTS_COLUMNS)
_FTS_NEW_LIST = ", ".join(f"new.{c}" for c in FTS_COLUMNS)
_FTS_OLD_LIST = ", ".join(f"old.{c}" for c in FTS_COLUMNS)

FTS_TRIGGERS = f"""
CREATE TRIGGER IF NOT EXISTS articles_ai AFTER INSERT ON articles BEGIN
  INSERT INTO articles_fts(rowid, {_FTS_COL_LIST}) VALUES (new.id, {_FTS_NEW_LIST});
END;
CREATE TRIGGER IF NOT EXISTS articles_ad AFTER DELETE ON articles BEGIN
  INSERT INTO articles_fts(articles_fts, rowid, {_FTS_COL_LIST})
  VALUES ('delete', old.id, {_FTS_OLD_LIST});
END;
CREATE TRIGGER IF NOT EXISTS articles_au AFTER UPDATE ON articles BEGIN
  INSERT INTO articles_fts(articles_fts, rowid, {_FTS_COL_LIST})
  VALUES ('delete', old.id, {_FTS_OLD_LIST});
  INSERT INTO articles_fts(rowid, {_FTS_COL_LIST}) VALUES (new.id, {_FTS_NEW_LIST});
END;
"""


class Storage:
    def __init__(self, path: Path, keywords: Iterable[str] | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        existing = self._existing_columns()
        legacy_date_schema = "date_source" not in existing
        # Idempotent migration: only ADD COLUMN when it is missing, so re-runs are safe.
        for name, kind in MIGRATIONS.items():
            if name not in existing:
                self.conn.execute(f"ALTER TABLE articles ADD COLUMN {name} {kind}")
        if legacy_date_schema:
            # V3 stored the feed timestamp in published_at. Preserve it as RSS date and
            # require webpage verification before calling anything the true publication date.
            self.conn.execute("UPDATE articles SET rss_published_at=published_at, published_at='', date_source='rss' WHERE COALESCE(published_at,'')<>''")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_score ON articles(score)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_published ON articles(published_at)")
        self.conn.execute(FTS_SCHEMA)
        self.conn.executescript(FTS_TRIGGERS)
        self.conn.commit()
        self.keywords: list[str] = list(keywords) if keywords else []

    # --- schema helpers -----------------------------------------------------

    def _existing_columns(self) -> set[str]:
        return {row[1] for row in self.conn.execute("PRAGMA table_info(articles)")}

    def set_keywords(self, keywords: Iterable[str]) -> None:
        self.keywords = list(keywords) if keywords else []

    # --- matched_keywords serialization -------------------------------------

    @staticmethod
    def _format_matched(matches: Iterable[str]) -> str:
        # Stored as |kw1|kw2| so counts use LIKE '%|X|%' — this is a full scan
        # over one short column, NOT an indexed lookup. Fine for the keyword-cloud
        # counts today; if it ever hurts, switch to an article_keywords bridge table.
        # Keywords containing '|' are filtered out here as a defensive backstop;
        # CLI-level rejection lands in round 4 (run.py --add-keyword).
        safe = [k for k in matches if k and "|" not in k]
        return "|" + "|".join(safe) + "|" if safe else ""

    def _match_for(self, article: dict) -> str:
        if not self.keywords:
            return article.get("matched_keywords") or ""
        return self._format_matched(compute_matched_keywords(article, self.keywords))

    # --- ingestion API ------------------------------------------------------

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
        article = dict(article)
        article["matched_keywords"] = self._match_for(article)
        fields = [
            "url", "original_url", "title_original", "title_cn", "source_id", "source_name",
            "publisher", "author", "layer", "published_at", "updated_at", "rss_published_at",
            "date_source", "crawled_at", "rss_summary", "article_text", "summary_cn", "main_image",
            "score", "score_reason", "industry_tags", "companies", "worth_attention", "archive_file",
            "fetch_status", "fetch_error", "ai_status", "ai_model", "ai_error", "link_resolver",
            "matched_keywords",
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
        self._refresh_matched(article_id)

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
        self._refresh_matched(article_id)

    def _refresh_matched(self, article_id: int) -> None:
        if not self.keywords:
            return
        row = self.conn.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
        if not row:
            return
        item = dict(row)
        item["industry_tags"] = json.loads(item.get("industry_tags") or "[]")
        item["companies"] = json.loads(item.get("companies") or "[]")
        payload = self._format_matched(compute_matched_keywords(item, self.keywords))
        self.conn.execute("UPDATE articles SET matched_keywords=? WHERE id=?", (payload, article_id))
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

    # --- reindex operations -------------------------------------------------

    def _drop_fts_triggers(self) -> None:
        for name in ("articles_ai", "articles_au", "articles_ad"):
            self.conn.execute(f"DROP TRIGGER IF EXISTS {name}")

    def rematch_keywords(self) -> int:
        """Recompute matched_keywords for every row against self.keywords.

        Returns the number of rows touched. Matching is case-insensitive
        because compute_matched_keywords() casefolds both sides. This method
        assumes triggers are dropped for a clean bulk rebuild; call reindex()
        for the full safe flow.
        """
        rows = self.conn.execute("SELECT * FROM articles").fetchall()
        touched = 0
        for row in rows:
            item = dict(row)
            item["industry_tags"] = json.loads(item.get("industry_tags") or "[]")
            item["companies"] = json.loads(item.get("companies") or "[]")
            payload = self._format_matched(compute_matched_keywords(item, self.keywords))
            self.conn.execute(
                "UPDATE articles SET matched_keywords=? WHERE id=?",
                (payload, item["id"]),
            )
            touched += 1
        self.conn.commit()
        return touched

    def rebuild_fts(self) -> None:
        """Re-derive the FTS shadow table from articles.

        Uses FTS5's built-in 'rebuild' command, which discards existing shadow
        content and rewrites it from the source table.
        """
        self.conn.execute("INSERT INTO articles_fts(articles_fts) VALUES('rebuild')")
        self.conn.commit()

    def reindex(self) -> tuple[int, int]:
        """Full safe backfill: drop triggers → rematch matched_keywords →
        rebuild FTS → recreate triggers. Returns (rows_rematched, fts_row_count).

        Triggers are dropped during bulk work because on an empty FTS shadow
        the 'delete' half of the update trigger corrupts the index.
        """
        self._drop_fts_triggers()
        touched = self.rematch_keywords()
        self.rebuild_fts()
        self.conn.executescript(FTS_TRIGGERS)
        self.conn.commit()
        fts_count = self.conn.execute("SELECT COUNT(*) FROM articles_fts").fetchone()[0]
        return touched, fts_count

    def close(self):
        self.conn.close()


def _cli() -> int:
    """Round-1 helper: `python -m src.storage reindex` populates matched_keywords
    for existing rows and rebuilds the FTS shadow. Used for the initial 26-row
    backfill and any later manual reindex."""
    import sys
    from .keywords import load_keywords

    base = Path(__file__).resolve().parent.parent
    action = sys.argv[1] if len(sys.argv) > 1 else "reindex"
    storage = Storage(base / "data/news.db", keywords=load_keywords(base / "config/watch_keywords.json"))
    try:
        if action == "reindex":
            touched, fts_count = storage.reindex()
            print(f"reindex done: rematched {touched} rows, FTS shadow now has {fts_count} rows")
        else:
            print(f"unknown action: {action}", file=sys.stderr)
            return 2
        return 0
    finally:
        storage.close()


if __name__ == "__main__":
    raise SystemExit(_cli())
