from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, timedelta
from html import escape
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

# Source dropdown + filter both key off source_name — the label of the
# configured feed (e.g. "OpenAI News", "Google News · HBM"). We deliberately
# do NOT fall back to publisher: publisher is the underlying media of the
# fetched article (Fortune, Mashable, 매일경제 ...), which for Google News
# aggregator feeds would flood the dropdown with dozens of downstream
# publishers. Publisher still shows in the results row and detail page.
_SOURCE_EXPR = "COALESCE(NULLIF(a.source_name,''), '未知信源')"

# Field-weight relevance expression. Used by both LIKE-mode search and the
# no-query-plus-keyword-filter view. Same-field match counts once regardless
# of occurrence count. Title 3 > summary 2 > body 1. Callers bind the same
# LIKE pattern 5 times positionally.
_WEIGHT_EXPR = (
    "((CASE WHEN a.title_cn LIKE ? OR a.title_original LIKE ? THEN 3 ELSE 0 END) + "
    " (CASE WHEN a.summary_cn LIKE ? THEN 2 ELSE 0 END) + "
    " (CASE WHEN a.article_text LIKE ? OR a.article_text_cn LIKE ? THEN 1 ELSE 0 END))"
)

# Sentinels for FTS snippet highlighting. SQLite's snippet() returns the
# column text verbatim (no HTML escaping), so if we hand it '<mark>' and
# the article body contains '<script>', the template's |safe filter would
# render live HTML. Use control characters that cannot appear in real text
# and that html.escape() leaves untouched, then swap them for <mark></mark>
# AFTER escaping. _like_snippet() uses the same escape-then-inject pattern.
_MARK_OPEN = "\x02"
_MARK_CLOSE = "\x03"


def _sanitize_fts_snippet(text: str) -> str:
    if not text:
        return ""
    return escape(text).replace(_MARK_OPEN, "<mark>").replace(_MARK_CLOSE, "</mark>")


def _build_filters(source: str, min_score: int, days: str, keywords: list[str]) -> tuple[list[str], list]:
    # Always exclude paywalled rows — they have no AI summary or full body
    # to display, and listing them wastes reader attention. They still exist
    # in DB so tomorrow's fetch dedups against them; count them with:
    #   SELECT COUNT(*) FROM articles WHERE fetch_status='paywalled'
    clauses: list[str] = ["COALESCE(a.fetch_status,'') <> 'paywalled'"]
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
    # Multiple selected keywords AND together — same semantics as source/score/date
    # filters, i.e. adding a chip narrows the result set rather than broadening it.
    for kw in keywords or []:
        clauses.append("a.matched_keywords LIKE ?")
        params.append(f"%|{kw}|%")
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


def _like_snippet(text: str, query: str, window: int = 60) -> str:
    """Case-insensitive substring highlight for LIKE-fallback rows.

    Returns HTML-escaped text around the first hit with <mark> around every
    occurrence of the query in the window. Returns '' when the query isn't in
    the text so the caller can try the next column.
    """
    if not text or not query:
        return ""
    pos = text.lower().find(query.lower())
    if pos < 0:
        return ""
    start = max(0, pos - window)
    end = min(len(text), pos + len(query) + window)
    escaped = escape(text[start:end])
    escaped_q = escape(query)
    pattern = re.compile(re.escape(escaped_q), re.IGNORECASE)
    highlighted = pattern.sub(lambda m: f"<mark>{m.group(0)}</mark>", escaped)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return prefix + highlighted + suffix


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
        "rss.trusted",
    } and bool(item.get("published_at"))
    return item


def search_articles(
    db: sqlite3.Connection, *, q: str = "", source: str = "",
    min_score: int = 0, days: str = "all", keywords: list[str] | None = None,
    sort: str = "relevance", page: int = 1, per_page: int = 10,
) -> dict:
    """Run a query and return a page of hydrated rows.

    Returns:
        dict(total, rows, page, pages, degraded, mode, sort).
        mode is 'fts' | 'like' | 'list'. sort is the *effective* order that
        was applied — 'relevance' only lands when mode=='fts' and the caller
        asked for relevance; everything else falls back to 'date' because
        LIKE/list have no bm25 signal to rank by.
    """
    keywords = keywords or []
    filter_clauses, filter_params = _build_filters(source, min_score, days, keywords)
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

    # Relevance is meaningful whenever there's SOMETHING to rank against —
    # FTS gets bm25, LIKE gets a field-weight score below, keyword-only
    # filter (no q, keyword chip clicked) also gets the field-weight score
    # using the keyword text as the ranking term. Only source/days/score
    # filters alone don't imply a ranking direction.
    can_rank = mode in ("fts", "like") or (mode == "list" and bool(keywords))
    effective_sort = "relevance" if (can_rank and sort == "relevance") else "date"

    order_by_date = (
        "ORDER BY COALESCE(NULLIF(a.published_at,''), NULLIF(a.rss_published_at,''), "
        "a.archived_at) DESC, COALESCE(a.score,0) DESC"
    )

    if mode == "fts":
        base_where = " AND ".join(
            ["a.id IN (SELECT rowid FROM articles_fts WHERE articles_fts MATCH ?)"] + filter_clauses
        )
        base_params = [match_expr] + filter_params
        # Snippet fallback order: summary_cn (col 2) → article_text (col 3) → title_cn (col 0).
        # Never draw the snippet from matched_keywords (col 8), otherwise users see the
        # internal |kw|kw| serialization on screen. When a column has no match, snippet()
        # still returns leading tokens without <mark>; instr() picks the first column that
        # actually highlighted the query.
        fts_order = "ORDER BY rank" if effective_sort == "relevance" else order_by_date
        # Sentinels here — HTML injection is guarded by _sanitize_fts_snippet
        # in Python. See _MARK_OPEN / _MARK_CLOSE comment above.
        select_sql = (
            "SELECT a.*, "
            f"(SELECT iif(instr(s_sum, '{_MARK_OPEN}'), s_sum, "
            f"            iif(instr(s_body, '{_MARK_OPEN}'), s_body, s_title)) "
            " FROM (SELECT "
            f"   snippet(articles_fts, 2, '{_MARK_OPEN}', '{_MARK_CLOSE}', '…', 12) AS s_sum, "
            f"   snippet(articles_fts, 3, '{_MARK_OPEN}', '{_MARK_CLOSE}', '…', 12) AS s_body, "
            f"   snippet(articles_fts, 0, '{_MARK_OPEN}', '{_MARK_CLOSE}', '…', 12) AS s_title "
            "  FROM articles_fts WHERE articles_fts MATCH ? AND rowid=a.id)) AS snippet, "
            "(SELECT bm25(articles_fts) FROM articles_fts WHERE articles_fts MATCH ? AND rowid=a.id) AS rank "
            f"FROM articles a WHERE {base_where} {fts_order} LIMIT ? OFFSET ?"
        )
        select_params_prefix = [match_expr, match_expr] + base_params
    elif mode == "like":
        like = f"%{q}%"
        text_clause = "(" + " OR ".join(f"a.{c} LIKE ?" for c in _LIKE_COLUMNS) + ")"
        base_where = " AND ".join([text_clause] + filter_clauses)
        base_params = [like] * len(_LIKE_COLUMNS) + filter_params
        if effective_sort == "relevance":
            select_sql = (
                f"SELECT a.*, {_WEIGHT_EXPR} AS relevance, NULL AS snippet "
                f"FROM articles a WHERE {base_where} "
                f"ORDER BY relevance DESC, "
                f"COALESCE(NULLIF(a.published_at,''), NULLIF(a.rss_published_at,''), a.archived_at) DESC "
                f"LIMIT ? OFFSET ?"
            )
            select_params_prefix = [like] * 5 + list(base_params)
        else:
            select_sql = f"SELECT a.*, NULL AS snippet FROM articles a WHERE {base_where} {order_by_date} LIMIT ? OFFSET ?"
            select_params_prefix = list(base_params)
    else:
        base_where = " AND ".join(filter_clauses) if filter_clauses else "1=1"
        base_params = list(filter_params)
        if effective_sort == "relevance":
            # No user query, but keyword chip(s) active — rank by where the
            # first keyword's text lands in the row (title > summary > body),
            # so chip-filtered results aren't just date-ordered. With multiple
            # keywords selected the intersection is already filtered; using
            # the first chip as ranking anchor keeps the SQL simple.
            kw_like = f"%{keywords[0]}%"
            select_sql = (
                f"SELECT a.*, {_WEIGHT_EXPR} AS relevance, NULL AS snippet "
                f"FROM articles a WHERE {base_where} "
                f"ORDER BY relevance DESC, "
                f"COALESCE(NULLIF(a.published_at,''), NULLIF(a.rss_published_at,''), a.archived_at) DESC "
                f"LIMIT ? OFFSET ?"
            )
            select_params_prefix = [kw_like] * 5 + list(base_params)
        else:
            select_sql = f"SELECT a.*, NULL AS snippet FROM articles a WHERE {base_where} {order_by_date} LIMIT ? OFFSET ?"
            select_params_prefix = list(base_params)

    total = db.execute(f"SELECT COUNT(*) FROM articles a WHERE {base_where}", base_params).fetchone()[0]
    pages = max(1, (total + per_page - 1) // per_page) if total else 1
    page = max(1, min(page, pages))
    offset = (page - 1) * per_page
    rows = db.execute(select_sql, select_params_prefix + [per_page, offset]).fetchall()

    hydrated = [_hydrate(r) for r in rows]
    if mode == "fts":
        for item in hydrated:
            item["snippet"] = _sanitize_fts_snippet(item.get("snippet") or "")
    elif mode == "like":
        for item in hydrated:
            for col in ("summary_cn", "article_text", "title_cn"):
                snip = _like_snippet(item.get(col) or "", q)
                if snip:
                    item["snippet"] = snip
                    break

    return {
        "total": total,
        "rows": hydrated,
        "page": page,
        "pages": pages,
        "degraded": degraded,
        "mode": mode,
        "sort": effective_sort,
    }


def get_article(db: sqlite3.Connection, article_id: int) -> dict | None:
    row = db.execute("SELECT * FROM articles WHERE id=?", (article_id,)).fetchone()
    return _hydrate(row) if row else None


def article_paragraphs(text: str) -> list[str]:
    """Split article_text into display paragraphs.

    Real articles here separate paragraphs with a single \\n (see the DB
    sample: bylines + prose are all single-newline-joined), so we split on
    any run of newlines and drop empties. Blank article_text returns [].
    """
    if not text:
        return []
    return [line.strip() for line in re.split(r"\n+", text) if line.strip()]


# Subscription CTAs, newsletter pitches, and other trailing boilerplate that
# publishers (THE DECODER, etc.) staple onto every article body. Trafilatura
# extracts them, then AI faithfully translates them — the reader ends up with
# a page of "立即订阅" bullets they didn't ask for. Any paragraph containing
# one of these substrings (casefold) is dropped from display. Add new patterns
# here as new junk shows up.
_BOILERPLATE_PATTERNS = (
    # English CTAs / feature bullets
    "subscribe now", "subscribe to", "subscribe for",
    "sign up for our newsletter", "start your free trial",
    "read on for the full", "join the comments",
    "full access to every article", "no ads",
    "weekly ai news recap", "daily ai news, always up to date",
    "ai radar", "hype-free coverage",
    "our full ten-year archive", "our full ten year archive",
    "ten-year archive", "ten year archive",
    "covered by a team with 10", "10+ years in ai",
    "archive access, and access to our comment",
    "ai news without the hype", "curated by humans",
    # Chinese CTA translations
    "立即订阅", "订阅无炒作", "订阅 the decoder", "订阅the decoder",
    "享受无广告", "无广告阅读", "评论区的访问权限",
    "继续阅读以了解", "完全访问 the decoder", "完全访问the decoder",
    "加入评论和社区", "ai 新闻摘要", "ai新闻摘要",
    "十年档案", "10 年以上 ai", "10年以上ai", "ai 经验的团队", "ai经验的团队",
    "始终最新", "每年 6 次", "每年6次",
    "无炒作的 ai 新闻", "无炒作的ai新闻", "由人工策划",
)


# Trigger phrases that mark the START of a related-articles / recommendation
# strip at the end of an article. Once we see one, everything from that
# paragraph to the end of the body is dropped — the tail is not the article,
# it's the next N articles the site suggests you read.
_CUTOFF_PATTERNS = (
    "you might also like", "you may also like",
    "related articles", "related stories", "more from",
    "recommended for you", "read next",
    "你可能还喜欢", "你可能也喜欢", "你可能感兴趣",
    "相关阅读", "相关文章", "相关新闻", "延伸阅读", "更多来自",
)


def _is_boilerplate(paragraph: str) -> bool:
    s = paragraph.casefold().strip()
    # Empty-looking bullet markers: a lone "-", "•", "*", etc., or fewer than
    # 3 real characters. Trafilatura sometimes leaves these behind as noise.
    stripped = s.strip("-•*·—– \t")
    if len(stripped) < 3:
        return True
    return any(pat in s for pat in _BOILERPLATE_PATTERNS)


def _is_cutoff(paragraph: str) -> bool:
    s = paragraph.casefold()
    return any(pat in s for pat in _CUTOFF_PATTERNS)


def filter_boilerplate_pairs(en: list[str], cn: list[str]) -> tuple[list[str], list[str]]:
    """Drop aligned index pairs where EITHER side is a subscription/newsletter
    CTA, and truncate the body at the first "you might also like"-style
    cutoff (whichever side triggers it first).

    Keeping alignment matters — the template pairs en[i] with cn[i], so
    filtering independently would shift the translation off its source line.
    """
    n_pairs = min(len(en), len(cn))
    en_out: list[str] = []
    cn_out: list[str] = []
    for i in range(n_pairs):
        if _is_cutoff(en[i]) or _is_cutoff(cn[i]):
            return en_out, cn_out
        if _is_boilerplate(en[i]) or _is_boilerplate(cn[i]):
            continue
        en_out.append(en[i])
        cn_out.append(cn[i])
    for p in en[n_pairs:]:
        if _is_cutoff(p):
            break
        if not _is_boilerplate(p):
            en_out.append(p)
    for p in cn[n_pairs:]:
        if _is_cutoff(p):
            break
        if not _is_boilerplate(p):
            cn_out.append(p)
    return en_out, cn_out


def total_articles(db: sqlite3.Connection) -> int:
    """Count the rows the search page can actually surface — paywalled rows
    exist in DB (for dedup + stats) but the search filter hides them, so
    including them in the top-of-page 「累计归档」 number would confuse the
    reader. For raw DB total including paywalled, query articles table."""
    return db.execute(
        "SELECT COUNT(*) FROM articles WHERE COALESCE(fetch_status,'') <> 'paywalled'"
    ).fetchone()[0]


def latest_archived_at(db: sqlite3.Connection) -> str:
    """Newest archived_at across all rows, or empty when the table is empty.

    archived_at is the CURRENT_TIMESTAMP the row was written, which reflects
    the last successful ingestion — what the top-of-page 「最近更新」 should show.
    """
    row = db.execute("SELECT MAX(archived_at) FROM articles").fetchone()
    return (row[0] or "") if row else ""


def list_sources(db: sqlite3.Connection) -> list[str]:
    rows = db.execute(
        f"SELECT DISTINCT {_SOURCE_EXPR} AS s FROM articles a ORDER BY s"
    ).fetchall()
    return [r[0] for r in rows if r[0]]


_DISPLAY_DATE_NO_ALIAS = (
    "substr(COALESCE(NULLIF(published_at,''), NULLIF(rss_published_at,''), "
    "NULLIF(crawled_at,''), archived_at), 1, 10)"
)


def keyword_counts(
    db: sqlite3.Connection, keywords: Iterable[str], days: int | None = 30
) -> list[dict]:
    """Count matched articles per keyword, optionally within the last N days.

    days=None → all-time. The date used is the same display-date fallback
    chain the results table uses (published_at → rss_published_at →
    crawled_at → archived_at), so the chip counts stay consistent with
    what the 时间 filter can actually reach.
    """
    date_clause = ""
    extra: list = []
    if days is not None and days > 0:
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        date_clause = f" AND {_DISPLAY_DATE_NO_ALIAS} >= ?"
        extra = [cutoff]
    out = []
    for kw in keywords:
        n = db.execute(
            f"SELECT COUNT(*) FROM articles WHERE matched_keywords LIKE ?{date_clause}",
            [f"%|{kw}|%"] + extra,
        ).fetchone()[0]
        out.append({"keyword": kw, "count": n})
    return out
