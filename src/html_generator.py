from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .keywords import matched_keywords
from .utils import safe_filename


class HtmlGenerator:
    def __init__(self, templates: Path, archive: Path, keywords: list[str] | None = None):
        self.archive = archive
        self.archive.mkdir(parents=True, exist_ok=True)
        self.keywords = keywords or []
        self.env = Environment(
            loader=FileSystemLoader(str(templates)),
            autoescape=select_autoescape(["html"]),
        )

    @staticmethod
    def _display_date(article: dict) -> str:
        # Internal grouping/archive date only. It may fall back to RSS/crawl time,
        # but the UI never labels this fallback as the article publication date.
        return (
            article.get("published_at")
            or article.get("rss_published_at")
            or article.get("crawled_at")
            or article.get("archived_at")
            or datetime.now().astimezone().isoformat()
        )[:10]

    def article(self, article: dict) -> str:
        date = self._display_date(article)
        directory = self.archive / date
        directory.mkdir(parents=True, exist_ok=True)
        name = safe_filename(
            f"{article.get('source_name', '')}_{article.get('title_cn') or article.get('title_original', '')}"
        ) + ".html"
        prepared = dict(article)
        prepared["matched_keywords"] = matched_keywords(prepared, self.keywords)
        prepared["display_date"] = date
        prepared["ai_succeeded"] = prepared.get("ai_status") == "success"
        prepared["date_is_verified"] = prepared.get("date_source") in {"jsonld.datePublished", "meta.article:published_time", "visible.labelled_publish_date", "webpage"} and bool(prepared.get("published_at"))
        (directory / name).write_text(
            self.env.get_template("article_archive.html").render(article=prepared), encoding="utf-8"
        )
        return f"{date}/{name}"

    def rebuild_articles(self, articles: list[dict], storage=None) -> list[dict]:
        rebuilt = []
        for item in articles:
            prepared = dict(item)
            archive_file = self.article(prepared)
            prepared["archive_file"] = archive_file
            if storage is not None and prepared.get("id"):
                storage.update_archive_file(int(prepared["id"]), archive_file)
            rebuilt.append(prepared)
        return rebuilt

    def index(self, articles: list[dict]) -> None:
        groups: dict[str, list[dict]] = defaultdict(list)
        keyword_counts: Counter[str] = Counter()
        sources: set[str] = set()
        prepared_all: list[dict] = []

        for original in articles:
            article = dict(original)
            article["matched_keywords"] = matched_keywords(article, self.keywords)
            article["display_date"] = self._display_date(article)
            article["ai_succeeded"] = article.get("ai_status") == "success"
            article["date_is_verified"] = article.get("date_source") in {"jsonld.datePublished", "meta.article:published_time", "visible.labelled_publish_date", "webpage"} and bool(article.get("published_at"))
            article["search_blob"] = " ".join(
                str(value)
                for value in [
                    article.get("title_cn", ""), article.get("title_original", ""),
                    article.get("summary_cn", ""), article.get("article_text", ""),
                    article.get("publisher", ""), article.get("source_name", ""),
                    " ".join(article.get("industry_tags") or []),
                    " ".join(article.get("companies") or []),
                    " ".join(article["matched_keywords"]),
                ]
                if value
            )
            for keyword in article["matched_keywords"]:
                keyword_counts[keyword] += 1
            sources.add(article.get("publisher") or article.get("source_name") or "未知信源")
            groups[article["display_date"]].append(article)
            prepared_all.append(article)

        ordered = [
            {
                "date": date,
                "articles": sorted(groups[date], key=lambda x: int(x.get("score") or 1), reverse=True),
            }
            for date in sorted(groups, reverse=True)
        ]
        keyword_stats = [{"keyword": keyword, "count": keyword_counts.get(keyword, 0)} for keyword in self.keywords]
        (self.archive / "index.html").write_text(
            self.env.get_template("index.html").render(
                groups=ordered, total=len(prepared_all), updated=datetime.now().astimezone(),
                keyword_stats=keyword_stats, sources=sorted(sources),
            ),
            encoding="utf-8",
        )
