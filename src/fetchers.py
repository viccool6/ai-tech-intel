from __future__ import annotations
import calendar
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus
import feedparser
from .utils import normalize_url, strip_html

def _time(entry):
    for key in ("published_parsed","updated_parsed","created_parsed"):
        val = entry.get(key)
        if val:
            try: return datetime.fromtimestamp(calendar.timegm(val), tz=timezone.utc)
            except Exception: pass
    for key in ("published","updated","created"):
        val = entry.get(key)
        if val:
            try:
                dt = parsedate_to_datetime(val)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except Exception: pass
    return None

def _feed_url(source):
    if source.get("type") == "rss":
        return source.get("url","")
    query = quote_plus(source.get("query",""))
    return f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"

def fetch_source(source: dict, hours: int, max_items: int):
    url = _feed_url(source)
    parsed = feedparser.parse(url)
    if not parsed.entries:
        return [], str(getattr(parsed, "bozo_exception", "RSS无条目"))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    out = []
    for e in parsed.entries:
        dt = _time(e)
        if dt and dt < cutoff: continue
        title, link = strip_html(e.get("title","")), normalize_url(e.get("link",""))
        if not title or not link: continue
        out.append({
            "source_id":source.get("id",""), "source_name":source.get("name",""),
            "source_type":source.get("type",""), "layer":source.get("layer",""),
            "title_original":title, "url":link,
            "published_at":dt.isoformat() if dt else "",
            "rss_summary":strip_html(e.get("summary","") or e.get("description",""))
        })
        if len(out) >= max_items: break
    warning = str(getattr(parsed, "bozo_exception", "")) if getattr(parsed,"bozo",False) else None
    return out, warning
