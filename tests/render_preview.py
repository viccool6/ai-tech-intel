"""Render static HTML snapshots of the two pages using real DB rows.

Dev-only helper for reviewing UI changes without running a server.
Writes into the directory given as argv[1] (default: /tmp/preview).
Not part of the app or the deploy path.

Usage:
    python3 tests/render_preview.py /tmp/uicheck
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/preview")
OUT.mkdir(parents=True, exist_ok=True)

app = create_app()
client = app.test_client()

PAGES = {
    "search.html": "/",
    "search-query.html": "/?q=HBM",
    "search-keyword.html": "/?keyword=NVIDIA&kw_days=all",
    "search-empty.html": "/?q=zzzzzzzz",
}

for name, url in PAGES.items():
    r = client.get(url)
    (OUT / name).write_text(r.get_data(as_text=True), encoding="utf-8")
    print(f"{name:24} {url:32} {r.status_code}")

# Pick one high-score article (translated body shown) and one low-score
# article (body suppressed) so both detail-page branches get exercised.
import sqlite3  # noqa: E402

conn = sqlite3.connect(app.config["DB_PATH"])
hi = conn.execute(
    "SELECT id FROM articles WHERE score>=4 AND COALESCE(article_text_cn,'')<>'' "
    "ORDER BY id DESC LIMIT 1"
).fetchone()
lo = conn.execute(
    "SELECT id FROM articles WHERE COALESCE(score,0)<4 ORDER BY id DESC LIMIT 1"
).fetchone()
conn.close()

for label, row in (("article-high.html", hi), ("article-low.html", lo)):
    if not row:
        print(f"{label:24} (no matching article)")
        continue
    url = f"/article/{row[0]}"
    r = client.get(url)
    (OUT / label).write_text(r.get_data(as_text=True), encoding="utf-8")
    print(f"{label:24} {url:32} {r.status_code}")

print(f"\n-> {OUT}")
