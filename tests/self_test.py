import sys
from pathlib import Path
import sqlite3
import tempfile
import types

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

sys.modules.setdefault("trafilatura", types.SimpleNamespace(extract=lambda *args, **kwargs: ""))
from bs4 import BeautifulSoup
from src.article_parser import _page_metadata
from src.scorer import fallback
from src.storage import Storage
from src.html_generator import HtmlGenerator
from src.keywords import matched_keywords

html_doc = '''<html><head>
<meta property="og:site_name" content="Example News">
<script type="application/ld+json">{"@type":"NewsArticle","headline":"NVIDIA launches new HBM AI server platform","datePublished":"2026-07-08T09:30:00Z","dateModified":"2026-07-09T10:00:00Z","author":{"name":"Jane Doe"},"publisher":{"name":"Example News"},"image":{"url":"/hero.jpg"}}</script>
</head><body></body></html>'''
metadata = _page_metadata(BeautifulSoup(html_doc, "html.parser"), "https://example.com/story", expected_title="NVIDIA launches new HBM AI server platform")
assert metadata["published_at"].startswith("2026-07-08")
assert metadata["date_source"] == "jsonld.datePublished"
assert metadata["author"] == "Jane Doe"
assert metadata["main_image"] == "https://example.com/hero.jpg"

article={
"url":"https://example.com/test","original_url":"https://news.google.com/rss/articles/test",
"title_original":"NVIDIA launches new HBM AI server platform",
"title_cn":"英伟达发布新一代HBM AI服务器平台","source_id":"test","source_name":"Google News - NVIDIA",
"publisher":"Example News","author":"Jane Doe","layer":"AI产业链上游",
"published_at":"2026-07-08T09:30:00+00:00","updated_at":"2026-07-09T10:00:00+00:00",
"rss_published_at":"2026-07-16T10:00:00+00:00","date_source":"webpage","crawled_at":"2026-07-16T12:00:00+08:00",
"rss_summary":"NVIDIA launched a new GPU server platform with advanced packaging.",
"article_text":"NVIDIA launched a new GPU server platform with HBM and advanced packaging.",
"main_image":"","fetch_status":"success","fetch_error":""
}
article.update(fallback(article["title_original"],article["article_text"]))
with tempfile.TemporaryDirectory() as td:
    td=Path(td)
    # Simulate an old V3 database and verify automatic migration.
    old_db=td/"old.db"
    conn=sqlite3.connect(old_db)
    conn.execute("CREATE TABLE articles(id INTEGER PRIMARY KEY AUTOINCREMENT,url TEXT UNIQUE NOT NULL,title_original TEXT NOT NULL,published_at TEXT,archived_at TEXT DEFAULT CURRENT_TIMESTAMP,industry_tags TEXT,companies TEXT)")
    conn.execute("INSERT INTO articles(url,title_original,published_at,industry_tags,companies) VALUES(?,?,?,?,?)", ("https://legacy.example","Legacy","2026-07-16T00:00:00Z","[]","[]"))
    conn.commit(); conn.close()
    migrated=Storage(old_db)
    columns={row[1] for row in migrated.conn.execute("PRAGMA table_info(articles)")}
    assert {"rss_published_at","date_source","crawled_at","updated_at","main_image"}.issubset(columns)
    legacy=dict(migrated.conn.execute("SELECT * FROM articles").fetchone())
    assert legacy["published_at"] == "" and legacy["rss_published_at"].startswith("2026-07-16")
    migrated.close()

    store=Storage(td/"news.db")
    gen=HtmlGenerator(BASE/"templates",td/"archive",keywords=["HBM","NVIDIA"])
    article["archive_file"]=gen.article(article)
    store.save(article)
    rebuilt=gen.rebuild_articles(store.all(),store)
    gen.index(rebuilt)
    index_text=(td/"archive/index.html").read_text(encoding="utf-8")
    detail_text=(td/"archive"/article["archive_file"]).read_text(encoding="utf-8")
    assert "searchInput" in index_text
    assert "HBM（1）" in index_text
    assert "原文↗" in index_text
    assert "打开原文网页" in detail_text
    assert "RSS提供/收录日期" not in detail_text
    assert "原文更新时间" not in detail_text
    assert "2026-07-08" in detail_text and "2026-07-16" not in detail_text
    assert matched_keywords(article,["HBM","CPO"])==["HBM"]
    store.close()
print("SELF_TEST_OK")
