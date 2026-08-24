from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, abort, g, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

# Load .env once at import time so app.py and gunicorn get the same view
# of the environment as run.py already does. This deliberately replaces the
# systemd EnvironmentFile approach — python-dotenv is permissive about
# spaces/parens/semicolons in values (e.g. our USER_AGENT string), whereas
# systemd's parser rejects them without quoting.
load_dotenv(Path(__file__).resolve().parent / ".env")

from src.config import SCORE_THRESHOLD_FULL_BODY
from src.keywords import load_keywords
from src.search import (
    article_paragraphs,
    filter_boilerplate_pairs,
    get_article,
    keyword_counts,
    list_sources,
    search_articles,
    total_articles,
)

# Presentational grouping of the watchlist keywords. Kept in code (not JSON)
# because the categorization is display-only — it doesn't affect matching, and
# with 18 keywords stable for weeks, plumbing a `category` field through JSON
# + CLI + loader would cost more than the 30-second edit here on the rare
# occasion a keyword is added or moved. Order within a group controls
# display order in that row.
KEYWORD_GROUPS = [
    ("产业动向", ["算力", "数据中心", "先进封装", "资本开支", "出口管制", "液冷", "光模块"]),
    ("技术缩写", ["HBM", "GPU", "ASIC", "CoWoS", "CPO", "DRAM"]),
    ("厂商",     ["NVIDIA/英伟达", "TSMC/台积电", "SK Hynix/海力士", "Broadcom"]),
    ("AI 应用",  ["OpenAI", "Anthropic"]),
]

BASE = Path(__file__).resolve().parent

# Single source of truth for search result pagination — change here and
# both the query and the template display update in lockstep.
PER_PAGE = 10

# Filter params carried across search ↔ article navigation so the
# back button restores the exact result set the user was on.
FILTER_KEYS = ("q", "source", "score", "days", "keyword", "sort", "kw_days", "show_zero", "page")

# The view=cn|both param controls bilingual toggle on the article page; carry
# it around like the FILTER_KEYS but keep it separate — search page doesn't
# consume it and shouldn't emit it either.
ARTICLE_KEYS = FILTER_KEYS + ("view",)


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

    # Session cookie signing key. Required for login state to survive across
    # requests. Set in .env — a missing key means every session cookie is
    # unsignable, so login would silently fail; fail loud instead.
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or "dev-only-do-not-ship"
    app.config["LOGIN_PASSWORD_HASH"] = os.environ.get("LOGIN_PASSWORD_HASH", "")
    # Long-lived session so a reader doesn't get bounced back to /login every
    # time the tab sleeps. 30 days is short enough that a stolen laptop
    # eventually re-auths.
    app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 30

    # x_prefix=1 lets nginx mount us under a subpath by sending
    # X-Forwarded-Prefix; url_for() then generates correctly-prefixed links.
    # No header = SCRIPT_NAME stays empty, so local dev is unaffected.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

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

    # Endpoints that don't require login. Static files are exempted too so
    # a login page can eventually pull CSS/JS without a redirect loop.
    PUBLIC_ENDPOINTS = {"login", "logout", "static"}

    @app.before_request
    def _require_login():
        if session.get("auth"):
            return None
        if request.endpoint in PUBLIC_ENDPOINTS:
            return None
        # Preserve where the reader was headed so login can bounce them back.
        # request.script_root carries the /ai-news-2 prefix (from ProxyFix
        # x_prefix); without it, next="/" would point at the domain root,
        # not our subpath, and the user would end up on the shared main site.
        nxt = request.script_root + request.full_path.rstrip("?")
        return redirect(url_for("login", next=nxt))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            pw = request.form.get("password", "")
            expected = app.config["LOGIN_PASSWORD_HASH"]
            if expected and check_password_hash(expected, pw):
                session.permanent = True
                session["auth"] = True
                nxt = request.args.get("next") or request.form.get("next") or url_for("search")
                # Only redirect back to paths under our own script root — an
                # attacker crafting ?next=//evil.com or ?next=/ (colleague's
                # site) would otherwise escape our app. Both plain "/" and
                # any absolute URL fall through to url_for('search').
                script_root = request.script_root or ""
                app_root = script_root + "/"
                if not (nxt == script_root or nxt.startswith(app_root)):
                    nxt = url_for("search")
                return redirect(nxt)
            error = "密码不对"
        return render_template("login.html", error=error, next=request.args.get("next", ""))

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

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
        active_keywords = [k.strip() for k in args.getlist("keyword") if k.strip()]
        # De-dup while preserving click order
        active_keywords = list(dict.fromkeys(active_keywords))
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
            keywords=active_keywords, sort=sort, page=page, per_page=PER_PAGE,
        )
        keywords = load_keywords(Path(app.config["KEYWORDS_PATH"]))
        stats = keyword_counts(db, keywords, days=kw_days)
        count_by_kw = {s["keyword"]: s["count"] for s in stats}
        # Group by presentational category. Any keyword in the JSON that is
        # NOT in KEYWORD_GROUPS gets appended to a fallback "其他" row so
        # a forgotten mapping update never silently hides it from the UI.
        grouped = [
            {"cat": cat,
             "entries": [{"keyword": kw, "count": count_by_kw.get(kw, 0)} for kw in kws]}
            for cat, kws in KEYWORD_GROUPS
        ]
        known = {kw for _, kws in KEYWORD_GROUPS for kw in kws}
        orphans = [s for s in stats if s["keyword"] not in known]
        if orphans:
            grouped.append({"cat": "其他", "entries": orphans})

        # DB file mtime = end time of the last successful pipeline run:
        # mac-daily.sh atomically renames the freshly scp'd snapshot into place,
        # so its mtime records exactly when launchd finished the daily job. On
        # local dev this instead reflects the last write, but the semantic
        # ("last time data changed") is what the reader expects either way.
        try:
            mtime = os.path.getmtime(app.config["DB_PATH"])
            from datetime import datetime
            latest_updated = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        except OSError:
            latest_updated = ""

        return render_template(
            "search.html",
            q=q, source=source, min_score=min_score, days=days,
            active_keywords=active_keywords,
            requested_sort=sort, sort=result["sort"],
            page=result["page"], pages=result["pages"], per_page=PER_PAGE,
            total=result["total"], rows=result["rows"],
            degraded=result["degraded"], mode=result["mode"],
            sources=list_sources(db),
            keyword_stats=stats,
            keyword_stats_grouped=grouped,
            kw_days=kw_days_raw,
            show_zero=show_zero,
            total_all=total_articles(db),
            latest_updated=latest_updated,
        )

    @app.route("/article/<int:article_id>")
    def article(article_id: int):
        db = get_db()
        art = get_article(db, article_id)
        if not art:
            abort(404)
        en_paras = article_paragraphs(art.get("article_text", ""))
        cn_paras = article_paragraphs(art.get("article_text_cn", ""))
        art["paragraphs"], art["paragraphs_cn"] = filter_boilerplate_pairs(en_paras, cn_paras)

        # Format published_at as CST datetime when the source gave us a real
        # timestamp; fall back to just the date if only a date is available.
        # Parsed via fromisoformat which handles the "+00:00"/"+09:00" suffix
        # our JSON-LD and RSS sources emit.
        from datetime import datetime, timezone, timedelta
        pub = (art.get("published_at") or "").strip()
        display = ""
        if pub:
            try:
                dt = datetime.fromisoformat(pub)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                cst = dt.astimezone(timezone(timedelta(hours=8)))
                # If the original string had no time (bare "YYYY-MM-DD"),
                # showing HH:MM would be fake precision — just show date.
                display = cst.strftime("%Y-%m-%d %H:%M" if "T" in pub else "%Y-%m-%d")
            except ValueError:
                display = pub[:16].replace("T", " ")
        art["published_display"] = display

        view = request.args.get("view", "both")
        if view not in ("both", "cn"):
            view = "both"
        return render_template(
            "article.html", article=art, view=view,
            score_threshold_full_body=SCORE_THRESHOLD_FULL_BODY,
        )

    @app.template_global()
    def page_url(**overrides):
        """URL to the current route preserving query params AND path vars
        (like article_id). value=None drops a key. Multi-value params like
        keyword are preserved as lists so url_for emits every occurrence."""
        args = {k: request.args.getlist(k) for k in request.args.keys()}
        for k, v in overrides.items():
            if v is None or v == "":
                args.pop(k, None)
            else:
                args[k] = v
        view_args = dict(request.view_args or {})
        return url_for(request.endpoint or "search", **view_args, **args)

    @app.template_global()
    def toggle_keyword_url(kw: str):
        """URL that toggles kw in/out of the active keyword set.
        Resets ?page= because filter change invalidates pagination."""
        current = [k.strip() for k in request.args.getlist("keyword") if k.strip()]
        new = [k for k in current if k != kw] if kw in current else current + [kw]
        args = {k: request.args.getlist(k) for k in request.args.keys() if k not in ("keyword", "page")}
        if new:
            args["keyword"] = new
        view_args = dict(request.view_args or {})
        return url_for(request.endpoint or "search", **view_args, **args)

    @app.template_global()
    def back_search_url():
        """Search URL that preserves the filter state the user came from."""
        args = {k: request.args.getlist(k) for k in request.args.keys() if k in FILTER_KEYS and request.args.getlist(k)}
        return url_for("search", **args)

    @app.template_global()
    def article_url(article_id: int):
        """Article URL that carries current search filters so the article's
        back button can restore the exact result set."""
        args = {k: request.args.getlist(k) for k in request.args.keys() if k in FILTER_KEYS and request.args.getlist(k)}
        return url_for("article", article_id=article_id, **args)

    return app


if __name__ == "__main__":
    # Werkzeug's debugger exposes remote code execution via the interactive
    # traceback console — must stay opt-in and off by default.
    create_app().run(host="127.0.0.1", port=5000, debug=os.getenv("FLASK_DEBUG") == "1")
