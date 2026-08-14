from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from urllib.parse import urljoin, urlparse

import requests
import trafilatura
from bs4 import BeautifulSoup
from dateutil import parser as date_parser


def _clean(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(_clean(x) for x in value if _clean(x))
    if isinstance(value, dict):
        # Prefer human-readable name. Deliberately NOT falling back to @id:
        # JSON-LD's @id is an Organization identifier URL (e.g.
        # "https://the-decoder.com/#organization") and using it as the
        # publisher name leaks a URL to the reader-facing UI.
        return _clean(value.get("name") or "")
    return re.sub(r"\s+", " ", str(value)).strip()


def _clean_name(value) -> str:
    """_clean for fields where a person or organisation NAME is expected
    (publisher, author). Rejects URL-shaped strings so JSON-LD/meta values
    that leaked through as an id URL don't hit the UI."""
    result = _clean(value)
    if not result:
        return ""
    if re.match(r"^(?:https?://|www\.)", result, re.I):
        return ""
    return result


def _date(value) -> str:
    value = _clean(value)
    if not value:
        return ""
    try:
        dt = date_parser.parse(value, fuzzy=False)
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except Exception:
        match = re.search(r"(?:19|20)\d{2}[-/.]\d{1,2}[-/.]\d{1,2}", value)
        if match:
            try:
                dt = date_parser.parse(match.group(0))
                return dt.replace(tzinfo=timezone.utc).isoformat()
            except Exception:
                pass
    return ""


def _walk_json(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _title_key(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", _clean(value).lower())


def _title_matches(expected: str, actual: str) -> bool:
    expected_key, actual_key = _title_key(expected), _title_key(actual)
    if not expected_key or not actual_key:
        return False
    if min(len(expected_key), len(actual_key)) >= 20 and (expected_key in actual_key or actual_key in expected_key):
        return True
    return SequenceMatcher(None, expected_key, actual_key).ratio() >= 0.65


def _json_ld_metadata(soup: BeautifulSoup, expected_title: str = "") -> dict:
    """Return only article-level JSON-LD whose headline matches the target article."""
    preferred_types = {"NewsArticle", "Article", "BlogPosting", "TechArticle", "Report"}
    candidates = []
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = script.string or script.get_text(" ", strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            cleaned = re.sub(r"^\s*<!--|-->\s*$", "", raw.strip()).rstrip(";")
            try:
                data = json.loads(cleaned)
            except Exception:
                continue
        for obj in _walk_json(data):
            obj_type = obj.get("@type", "")
            types = set(obj_type if isinstance(obj_type, list) else [obj_type])
            if not (types & preferred_types) or not obj.get("datePublished"):
                continue
            headline = _clean(obj.get("headline") or obj.get("name"))
            # A matching headline prevents selecting dates from related/recommended articles.
            if expected_title and not _title_matches(expected_title, headline):
                continue
            score = 4
            if headline:
                score += 2
            if obj.get("mainEntityOfPage"):
                score += 1
            candidates.append((score, obj))
    if not candidates:
        return {}
    obj = max(candidates, key=lambda x: x[0])[1]
    image = obj.get("image")
    if isinstance(image, dict):
        image = image.get("url") or image.get("contentUrl")
    if isinstance(image, list) and image:
        image = image[0]
    return {
        "published_at": _date(obj.get("datePublished")),
        "date_source": "jsonld.datePublished",
        "author": _clean_name(obj.get("author")),
        "publisher": _clean_name(obj.get("publisher")),
        "main_image": _clean(image),
    }

def _meta_content(soup: BeautifulSoup, selectors: list[tuple[str, str]]) -> str:
    for attr, name in selectors:
        tag = soup.find("meta", attrs={attr: re.compile(rf"^{re.escape(name)}$", re.I)})
        if tag and tag.get("content"):
            return _clean(tag.get("content"))
    return ""


def _page_metadata(soup: BeautifulSoup, base_url: str, expected_title: str = "") -> dict:
    ld = _json_ld_metadata(soup, expected_title=expected_title)
    published = ld.get("published_at", "")
    date_source = ld.get("date_source", "")
    if not published:
        # Explicit article publication metadata. Generic date/pubdate fields and
        # arbitrary <time> elements remain intentionally rejected.
        published = _date(_meta_content(soup, [
            ("property", "article:published_time"),
            ("name", "article:published_time"),
            ("itemprop", "datePublished"),
        ]))
        if published:
            date_source = "meta.article:published_time"
    if not published:
        published, date_source = _visible_publish_date(soup, expected_title=expected_title)
    # Wrap both paths in _clean_name so a stray URL from <meta> can't leak
    # even when JSON-LD is absent (same defense as the JSON-LD path uses).
    publisher = _clean_name(ld.get("publisher") or _meta_content(soup, [
        ("property", "og:site_name"), ("name", "application-name"),
    ]))
    author = _clean_name(ld.get("author") or _meta_content(soup, [
        ("name", "author"), ("property", "article:author"), ("name", "byl"),
    ]))
    # If we successfully parsed the page but no author metadata is present,
    # the article is deliberately institutional (OpenAI/DeepMind blog etc.
    # publish under the org name, not an individual). Distinguish from
    # 'unknown' (fetch failed / no HTML at all) — that case is set in the
    # extract_article() failure return dict below.
    author_status = "named" if author else "institutional"
    image = ld.get("main_image") or _meta_content(soup, [
        ("property", "og:image"), ("name", "twitter:image"), ("name", "twitter:image:src"),
    ])
    if image:
        image = urljoin(base_url, image)
    return {
        "published_at": published,
        "date_source": date_source,
        "publisher": publisher,
        "author": author,
        "author_status": author_status,
        "main_image": image,
    }



def _visible_publish_date(soup: BeautifulSoup, expected_title: str = "") -> tuple[str, str]:
    """Find an explicitly labelled visible publication date near the article header.

    This deliberately avoids scanning the whole page, which can confuse event dates,
    related-story dates, comments, or footer years with the article publication date.
    """
    labels = re.compile(
        r"\b(published|posted|publication date|date published|first published)\b|"
        r"发布日期|发布时间|发表于|发布于|刊登于",
        re.I,
    )
    date_patterns = [
        re.compile(r"(?:19|20)\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?"),
        re.compile(r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},?\s+(?:19|20)\d{2}", re.I),
        re.compile(r"\d{1,2}\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(?:19|20)\d{2}", re.I),
    ]

    roots = []
    article = soup.find("article")
    if article:
        roots.append(article)
    main = soup.find("main")
    if main and main not in roots:
        roots.append(main)
    if not roots:
        roots = [soup]

    for root in roots:
        heading = None
        if expected_title:
            for candidate in root.find_all(["h1", "h2"], limit=12):
                if _title_matches(expected_title, candidate.get_text(" ", strip=True)):
                    heading = candidate
                    break
        if heading is None:
            heading = root.find("h1") or root.find("h2")

        nodes = []
        if heading:
            nodes.append(heading)
            nodes.extend(list(heading.next_elements)[:120])
            parent = heading.parent
            if parent:
                nodes.extend(list(parent.children)[:40])
        else:
            nodes.extend(list(root.descendants)[:220])

        seen = set()
        for node in nodes:
            tag = node if getattr(node, "name", None) else getattr(node, "parent", None)
            if not tag or id(tag) in seen:
                continue
            seen.add(id(tag))
            text = _clean(tag.get_text(" ", strip=True))
            if not text or len(text) > 320 or not labels.search(text):
                continue
            for pattern in date_patterns:
                match = pattern.search(text)
                if match:
                    parsed = _date(match.group(0).replace("年", "-").replace("月", "-").replace("日", ""))
                    if parsed:
                        return parsed, "visible.labelled_publish_date"
    return "", ""


def _is_google_news_url(url: str) -> bool:
    try:
        return urlparse(url).netloc.lower() in {"news.google.com", "www.news.google.com"}
    except Exception:
        return False


def _is_bing_news_url(url: str) -> bool:
    """Bing News redirect wrapper: http(s)://www.bing.com/news/apiclick.aspx?...&url=<encoded>"""
    try:
        p = urlparse(url)
        return p.netloc.lower() in {"www.bing.com", "bing.com"} and p.path.startswith("/news/apiclick")
    except Exception:
        return False


def _resolve_google_news_url(url: str) -> tuple[str, str]:
    """Resolve a Google News wrapper URL to the publisher URL.

    The optional decoder is deliberately isolated: if Google changes its wrapper
    format or the decoder is unavailable, crawling still continues and reports a
    precise error instead of silently treating the Google page as the article.
    """
    if not _is_google_news_url(url):
        return url, ""
    try:
        from googlenewsdecoder import gnewsdecoder
        decoded = gnewsdecoder(url, interval=0)
        if isinstance(decoded, dict) and decoded.get("status"):
            target = _clean(decoded.get("decoded_url"))
            if target and not _is_google_news_url(target):
                return target, "googlenewsdecoder"
        message = decoded.get("message", "无法解析Google News链接") if isinstance(decoded, dict) else "无法解析Google News链接"
        return url, str(message)
    except ImportError:
        return url, "未安装googlenewsdecoder，请运行 pip install -r requirements.txt"
    except Exception as exc:
        return url, f"GoogleNewsDecodeError: {type(exc).__name__}: {exc}"


def _resolve_bing_news_url(url: str) -> tuple[str, str]:
    """Resolve a Bing News redirect wrapper to the publisher URL.

    Bing embeds the real URL as a plain URL-encoded 'url' query param
    (unlike Google's base64 obfuscation). Just parse_qs + unquote — no
    external decoder needed.
    """
    if not _is_bing_news_url(url):
        return url, ""
    try:
        from urllib.parse import parse_qs
        qs = parse_qs(urlparse(url).query)
        target = _clean((qs.get("url") or [""])[0])
        if target and target.startswith(("http://", "https://")) and not _is_bing_news_url(target):
            return target, "bing.apiclick"
        return url, "无法从Bing News apiclick中解析url参数"
    except Exception as exc:
        return url, f"BingNewsDecodeError: {type(exc).__name__}: {exc}"


def _resolve_redirect_url(url: str) -> tuple[str, str]:
    """Dispatch to the right aggregator wrapper decoder. Returns
    (resolved_url, note). note is empty when no wrapper was matched."""
    if _is_google_news_url(url):
        return _resolve_google_news_url(url)
    if _is_bing_news_url(url):
        return _resolve_bing_news_url(url)
    return url, ""


def _is_aggregator_wrapper(url: str) -> bool:
    """Any known aggregator redirect wrapper — used to flag 'summary_only'
    when unwrap failed and we're still on a wrapper page."""
    return _is_google_news_url(url) or _is_bing_news_url(url)


def _extract_readable_text(html: str, url: str) -> str:
    """Extract article text with a precision pass and two conservative fallbacks."""
    candidates = []
    for precision in (True, False):
        text = trafilatura.extract(
            html, include_comments=False, include_tables=False,
            favor_precision=precision, favor_recall=not precision, url=url,
        ) or ""
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if text:
            candidates.append(text)
    soup = BeautifulSoup(html, "html.parser")
    root = soup.find("article") or soup.find("main")
    if root:
        paragraphs = [
            _clean(tag.get_text(" ", strip=True))
            for tag in root.find_all(["p", "h2", "h3", "li"])
        ]
        fallback = "\n\n".join(x for x in paragraphs if len(x) >= 35)
        if fallback:
            candidates.append(fallback)
    return max(candidates, key=len, default="")


def _content_is_complete(text: str, expected_title: str = "") -> bool:
    compact = _clean(text)
    min_chars = int(os.getenv("CONTENT_MIN_CHARS", "500"))
    min_words = int(os.getenv("CONTENT_MIN_WORDS", "80"))
    words = re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]", compact)
    if len(compact) < min_chars or len(words) < min_words:
        return False
    if expected_title and _title_matches(expected_title, compact) and len(compact) < max(min_chars * 2, len(expected_title) * 5):
        return False
    return True


def extract_article(url: str, expected_title: str = "") -> dict:
    timeout = int(os.getenv("REQUEST_TIMEOUT", "25"))
    retries = int(os.getenv("REQUEST_RETRIES", "2"))
    headers = {
        "User-Agent": os.getenv("USER_AGENT", "Mozilla/5.0"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.7",
    }
    resolved_url, resolve_note = _resolve_redirect_url(url)
    result = {
        "final_url": resolved_url, "text": "", "main_image": "", "author": "",
        "author_status": "unknown",
        "publisher": "", "published_at": "",
        "date_source": "", "fetch_status": "failed", "fetch_error": "",
        # resolve_note carries which decoder ran ('googlenewsdecoder' or
        # 'bing.apiclick'); empty when the URL was already direct.
        "link_resolver": resolve_note if resolved_url != url else "",
    }
    if _is_aggregator_wrapper(resolved_url):
        result["fetch_status"] = "summary_only"
        result["fetch_error"] = resolve_note or "聚合器中转链接未解析到原文"
        return result
    for attempt in range(retries + 1):
        try:
            response = requests.get(resolved_url, headers=headers, timeout=timeout, allow_redirects=True)
            response.raise_for_status()
            result["final_url"] = response.url
            content_type = (response.headers.get("Content-Type") or "").lower()
            if "html" not in content_type and "xml" not in content_type and content_type:
                raise ValueError(f"非HTML内容：{content_type}")
            soup = BeautifulSoup(response.text, "html.parser")
            metadata = _page_metadata(soup, response.url, expected_title=expected_title)
            result.update(metadata)
            text = _extract_readable_text(response.text, response.url)
            result["text"] = text
            if _content_is_complete(text, expected_title):
                result["fetch_status"] = "success"
                result["fetch_error"] = ""
            else:
                result["fetch_status"] = "summary_only"
                length = len(_clean(text))
                result["fetch_error"] = f"正文不足（提取{length}字符，未达到完整正文阈值）"
            return result
        except Exception as exc:
            result["fetch_error"] = f"{type(exc).__name__}: {exc}"
            if attempt < retries:
                time.sleep(2 ** attempt + 1)
    return result

