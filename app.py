from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from flask import Flask, abort, g, render_template, request, url_for

from src.keywords import load_keywords
from src.search import (
    article_paragraphs,
    get_article,
    keyword_counts,
    latest_archived_at,
    list_sources,
    search_articles,
    total_articles,
)

BASE = Path(__file__).resolve().parent

# Single source of truth for search result pagination — change here and
# both the query and the template display update in lockstep.
PER_PAGE = 10

# Filter params carried across search ↔ article navigation so the
# back button restores the exact result set the user was on.
FILTER_KEYS = ("q", "source", "score", "days", "keyword", "sort", "kw_days", "show_zero", "page")


def _parse_kw_days(raw: str) -> tuple[int | None, str]:
    """Return (int_or_None_for_all, canonical_string) for the kw_days param."""
    if raw == "all":
        return None, "all"
    try:
        n = max(1, int(raw))
        return n, str(n)
    except ValueError:
        return 30, "30"


def create_app() -> Flask:
    app = Flask(__name__, template_folder=str(BASE / "templates"))
    app.config["DB_PATH"] = str(BASE / "data/news.db")
    app.config["KEYWORDS_PATH"] = str(BASE / "config/watch_keywords.json")

    def get_db() -> sqlite3.Connection:
        if "db" not in g:
            conn = sqlite3.connect(app.config["DB_PATH"])
            conn.row_factory = sqlite3.Row
            g.db = conn
        return g.db

    @app.teardown_appcontext
    def _close_db(_exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.route("/")
    def search():
        args = request.args
        q = args.get("q", "").strip()
        source = args.get("source", "").strip()
        try:
            min_score = int(args.get("score", "0") or 0)
        except ValueError:
            min_score = 0
        days = args.get("days", "all") or "all"
        keyword = args.get("keyword", "").strip()
        sort = args.get("sort", "relevance") or "relevance"
        if sort not in ("relevance", "date"):
            sort = "relevance"
        try:
            page = int(args.get("page", "1"))
        except ValueError:
            page = 1
        if page < 1:
            page = 1

        kw_days, kw_days_raw = _parse_kw_days(args.get("kw_days", "30") or "30")
        show_zero = args.get("show_zero", "") == "1"

        db = get_db()
        result = search_articles(
            db, q=q, source=source, min_score=min_score, days=days,
            keyword=keyword, sort=sort, page=page, per_page=PER_PAGE,
        )
        keywords = load_keywords(Path(app.config["KEYWORDS_PATH"]))
        return render_template(
            "search.html",
            q=q, source=source, min_score=min_score, days=days,
            active_keyword=keyword,
            requested_sort=sort, sort=result["sort"],
            page=result["page"], pages=result["pages"], per_page=PER_PAGE,
            total=result["total"], rows=result["rows"],
            degraded=result["degraded"], mode=result["mode"],
            sources=list_sources(db),
            keyword_stats=keyword_counts(db, keywords, days=kw_days),
            kw_days=kw_days_raw,
            show_zero=show_zero,
            total_all=total_articles(db),
            latest_updated=latest_archived_at(db),
        )

    @app.route("/article/<int:article_id>")
    def article(article_id: int):
        db = get_db()
        art = get_article(db, article_id)
        if not art:
            abort(404)
        art["paragraphs"] = article_paragraphs(art.get("article_text", ""))
        return render_template("article.html", article=art)

    @app.template_global()
    def page_url(**overrides):
        """URL to the current route preserving query params. value=None drops a key."""
        args = dict(request.args)
        for k, v in overrides.items():
            if v is None or v == "":
                args.pop(k, None)
            else:
                args[k] = v
        return url_for(request.endpoint or "search", **args)

    @app.template_global()
    def back_search_url():
        """Search URL that preserves the filter state the user came from."""
        args = {k: v for k, v in request.args.items() if k in FILTER_KEYS and v}
        return url_for("search", **args)

    @app.template_global()
    def article_url(article_id: int):
        """Article URL that carries current search filters so the article's
        back button can restore the exact result set."""
        args = {k: v for k, v in request.args.items() if k in FILTER_KEYS and v}
        return url_for("article", article_id=article_id, **args)

    return app


if __name__ == "__main__":
    # Werkzeug's debugger exposes remote code execution via the interactive
    # traceback console — must stay opt-in and off by default.
    create_app().run(host="127.0.0.1", port=5000, debug=os.getenv("FLASK_DEBUG") == "1")
