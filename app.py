from __future__ import annotations

import sqlite3
from pathlib import Path

from flask import Flask, g, render_template, request, url_for

from src.keywords import load_keywords
from src.search import (
    keyword_counts,
    latest_archived_at,
    list_sources,
    search_articles,
    total_articles,
)

BASE = Path(__file__).resolve().parent


def create_app() -> Flask:
    app = Flask(__name__, template_folder=str(BASE / "templates"))
    app.config["DB_PATH"] = str(BASE / "data/news.db")
    app.config["KEYWORDS_PATH"] = str(BASE / "config/watch_keywords.json")
    app.config["PER_PAGE"] = 20

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
        try:
            page = int(args.get("page", "1"))
        except ValueError:
            page = 1
        if page < 1:
            page = 1

        kw_days_raw = args.get("kw_days", "30") or "30"
        if kw_days_raw == "all":
            kw_days: int | None = None
        else:
            try:
                kw_days = max(1, int(kw_days_raw))
            except ValueError:
                kw_days = 30
                kw_days_raw = "30"
        show_zero = args.get("show_zero", "") == "1"

        db = get_db()
        per_page = app.config["PER_PAGE"]
        result = search_articles(
            db, q=q, source=source, min_score=min_score, days=days,
            keyword=keyword, page=page, per_page=per_page,
        )
        keywords = load_keywords(Path(app.config["KEYWORDS_PATH"]))
        return render_template(
            "search.html",
            q=q, source=source, min_score=min_score, days=days,
            active_keyword=keyword,
            page=result["page"], pages=result["pages"], per_page=per_page,
            total=result["total"], rows=result["rows"],
            degraded=result["degraded"], mode=result["mode"],
            sources=list_sources(db),
            keyword_stats=keyword_counts(db, keywords, days=kw_days),
            kw_days=kw_days_raw,
            show_zero=show_zero,
            total_all=total_articles(db),
            latest_updated=latest_archived_at(db),
        )

    @app.template_global()
    def page_url(**overrides):
        """Build a URL that preserves current query params, overriding the given keys.
        Pass value=None to drop a key."""
        args = dict(request.args)
        for k, v in overrides.items():
            if v is None or v == "":
                args.pop(k, None)
            else:
                args[k] = v
        return url_for("search", **args)

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5000, debug=True)
