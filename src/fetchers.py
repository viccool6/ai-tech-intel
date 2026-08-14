from __future__ import annotations
import calendar
import logging
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, quote_plus, urlparse
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


def _target_host(url: str) -> str:
    """Return the eventual publisher host for blacklist checking.

    For Bing News wrappers (bing.com/news/apiclick?...&url=<encoded>) we can
    cheaply parse out the real target host without a network round-trip, so
    blacklisting kicks in at fetch time. Google News wrappers hide the
    target behind base64 obfuscation that only googlenewsdecoder can unwrap
    (network + slow), so this function returns 'news.google.com' for them
    and the blacklist won't match — Google-source blacklisting would have
    to happen in article_parser after decoding, which is fine because the
    Google feed path can't reach the server anyway (requires_proxy).
    """
    try:
        p = urlparse(url)
        host = p.netloc.lower()
        if host in {"www.bing.com", "bing.com"} and p.path.startswith("/news/apiclick"):
            real = (parse_qs(p.query).get("url") or [""])[0]
            if real:
                try:
                    host = urlparse(real).netloc.lower()
                except Exception:
                    pass
        return host.removeprefix("www.")
    except Exception:
        return ""


def _is_blacklisted(host: str, blacklist) -> bool:
    if not host or not blacklist:
        return False
    return any(host == b or host.endswith("." + b) for b in blacklist)


def fetch_source(source: dict, hours: int, max_items: int, blacklist=()):
    url = _feed_url(source)
    parsed = feedparser.parse(url)
    if not parsed.entries:
        base = str(getattr(parsed, "bozo_exception", "RSS无条目"))
        if source.get("requires_proxy"):
            # The source is explicitly marked as needing a proxy. Log clearly
            # so a nightly cron doesn't look like a real failure.
            base = f"[requires_proxy] {base}（当前环境访问不了，已跳过）"
        return [], base
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    out = []
    blocked = 0
    for e in parsed.entries:
        dt = _time(e)
        if dt and dt < cutoff: continue
        title, link = strip_html(e.get("title","")), normalize_url(e.get("link",""))
        if not title or not link: continue
        host = _target_host(link)
        if _is_blacklisted(host, blacklist):
            blocked += 1
            logging.info("跳过黑名单域：%s（%s / %s）", host, source.get("id",""), title[:60])
            continue
        out.append({
            "source_id":source.get("id",""), "source_name":source.get("name",""),
            "source_type":source.get("type",""), "layer":source.get("layer",""),
            "title_original":title, "url":link,
            "published_at":dt.isoformat() if dt else "",
            "rss_summary":strip_html(e.get("summary","") or e.get("description",""))
        })
        if len(out) >= max_items: break
    warning = str(getattr(parsed, "bozo_exception", "")) if getattr(parsed,"bozo",False) else None
    if blocked and not warning:
        warning = f"黑名单命中 {blocked} 条已跳过"
    return out, warning
