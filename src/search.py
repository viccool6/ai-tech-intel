from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from typing import Iterable

# Columns scanned by the LIKE fallback when a query is shorter than the FTS5
# trigram tokenizer's 3-character minimum. article_text is included on purpose:
# many CJK monitor terms are 2 characters (液冷/算力/推理), and restricting
# fallback to titles would make those queries return nothing.
_LIKE_COLUMNS = [
    "title_cn", "title_original", "summary_cn", "article_text",
    "publisher", "source_name", "industry_tags", "companies", "matched_keywords",
]

# Display date follows html_generator._display_date's fallback chain.
_DISPLAY_DATE_EXPR = (
    "substr(COALESCE(NULLIF(a.published_at,''), NULLIF(a.rss_published_at,''), "
    "NULLIF(a.crawled_at,''), a.archived_at), 1, 10)"
)

_SOURCE_EXPR = "COALESCE(NULLIF(a.publisher,''), NULLIF(a.source_name,''), '未知信源')"


def _build_filters(source: str, min_score: int, days: str, keyword: str) -> tuple[list[str], list]:
    clauses: list[str] = []
    params: list = []
    if source:
        clauses.append(f"{_SOURCE_EXPR} = ?")
        params.append(source)
    if min_score and min_score > 0:
        clauses.append("COALESCE(a.score, 0) >= ?")
        params.append(min_score)
    if days and days != "all":
        try:
            n = int(days)
        except ValueError:
            n = 0
        if n > 0:
            cutoff = (date.today() - timedelta(days=n)).isoformat()
            clauses.append(f"{_DISPLAY_DATE_EXPR} >= ?")
            params.append(cutoff)
    if keyword:
        clauses.append("a.matched_keywords LIKE ?")
        params.append(f"%|{keyword}|%")
    return clauses, params


def _fts_query(q: str) -> str:
    """Wrap each whitespace-separated token as an FTS5 phrase, joined with AND.

    Doubled quotes escape internal quotes per FTS5 syntax. Empty result means
    the caller should fall back to no-query mode.
    """
    tokens = []
    for raw in q.split():
        escaped = raw.replace('"', '""').strip()
        if escaped:
            tokens.append(f'"{escaped}"')
    return " AND ".join(tokens)


def _hydrate(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["industry_tags"] = json.loads(item.get("industry_tags") or "[]")
    item["companies"] = json.loads(item.get("companies") or "[]")
    item["worth_attention"] = bool(item.get("worth_attention"))
    # Split |kw1|kw2| back into a list for the row-level chip display.
    raw = item.get("matched_keywords") or ""
    item["matched_keyword_list"] = [t for t in raw.split("|") if t]
    # Display date mirrors html_generator._display_date fallback chain.
    item["display_date"] = (
        item.get("published_at") or item.get("rss_published_at")
        or item.get("crawled_at") or item.get("archived_at") or ""
    )[:10]
    item["ai_succeeded"] = item.get("ai_status") == "success"
    item["date_is_verified"] = item.get("date_source") in {
        "jsonld.datePublished", "meta.article:published_time",
        "visible.labelled_publish_date", "webpage",
    } and bool(item.get("published_at"))
    return item


def search_articles(
    db: sqlite3.Connection, *, q: str = "", source: str = "",
    min_score: int = 0, days: str = "all", keyword: str = "",
    page: int = 1, per_page: int = 20,
) -> dict:
    """Run a query and return a page of hydrated rows.

    Returns:
        dict(total, rows, page, pages, degraded, mode).
        mode is 'fts' | 'like' | 'list'. degraded is True only when a non-empty
        query was too short for FTS and fell back to LIKE.
    """
    filter_clauses, filter_params = _build_filters(source, min_score, days, keyword)
    q = q.strip()
    degraded = False
    mode = "list"
    match_expr = ""

    if q:
        if len(q) < 3:
            degraded = True
            mode = "like"
        else:
            match_expr = _fts_query(q)
            if match_expr:
                mode = "fts"
            else:
                degraded = True
                mode = "like"

    order_by_date = (
        "ORDER BY COALESCE(NULLIF(a.published_at,''), NULLIF(a.rss_published_at,''), "
        "a.archived_at) DESC, COALESCE(a.score,0) DESC"
    )

    if mode == "fts":
        base_where = " AND ".join(
            ["a.id IN (SELECT rowid FROM articles_fts WHERE articles_fts MATCH ?)"] + filter_clauses
        )
        base_params = [match_expr] + filter_params
        select_sql = (
            "SELECT a.*, "
            "(SELECT snippet(articles_fts, -1, '<mark>', '</mark>', '…', 12) "
            " FROM articles_fts WHERE articles_fts MATCH ? AND rowid=a.id) AS snippet, "
            "(SELECT bm25(articles_fts) FROM articles_fts WHERE articles_fts MATCH ? AND rowid=a.id) AS rank "
            f"FROM articles a WHERE {base_where} ORDER BY rank LIMIT ? OFFSET ?"
        )
        select_params_prefix = [match_expr, match_expr] + base_params
    elif mode == "like":
        like = f"%{q}%"
        text_clause = "(" + " OR ".join(f"a.{c} LIKE ?" for c in _LIKE_COLUMNS) + ")"
        base_where = " AND ".join([text_clause] + filter_clauses)
        base_params = [like] * len(_LIKE_COLUMNS) + filter_params
        select_sql = f"SELECT a.*, NULL AS snippet FROM articles a WHERE {base_where} {order_by_date} LIMIT ? OFFSET ?"
        select_params_prefix = list(base_params)
    else:
        base_where = " AND ".join(filter_clauses) if filter_clauses else "1=1"
        base_params = list(filter_params)
        select_sql = f"SELECT a.*, NULL AS snippet FROM articles a WHERE {base_where} {order_by_date} LIMIT ? OFFSET ?"
        select_params_prefix = list(base_params)

    total = db.execute(f"SELECT COUNT(*) FROM articles a WHERE {base_where}", base_params).fetchone()[0]
    pages = max(1, (total + per_page - 1) // per_page) if total else 1
    page = max(1, min(page, pages))
    offset = (page - 1) * per_page
    rows = db.execute(select_sql, select_params_prefix + [per_page, offset]).fetchall()

    return {
        "total": total,
        "rows": [_hydrate(r) for r in rows],
        "page": page,
        "pages": pages,
        "degraded": degraded,
        "mode": mode,
    }


def list_sources(db: sqlite3.Connection) -> list[str]:
    rows = db.execute(
        f"SELECT DISTINCT {_SOURCE_EXPR} AS s FROM articles a ORDER BY s"
    ).fetchall()
    return [r[0] for r in rows if r[0]]


def keyword_counts(db: sqlite3.Connection, keywords: Iterable[str]) -> list[dict]:
    out = []
    for kw in keywords:
        n = db.execute(
            "SELECT COUNT(*) FROM articles WHERE matched_keywords LIKE ?",
            (f"%|{kw}|%",),
        ).fetchone()[0]
        out.append({"keyword": kw, "count": n})
    return out
