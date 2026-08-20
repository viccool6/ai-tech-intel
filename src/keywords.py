from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

DEFAULT_KEYWORDS = [
    "HBM", "CoWoS", "CPO", "GPU", "ASIC", "先进封装", "液冷",
    "NVIDIA", "TSMC", "SK Hynix", "Micron", "OpenAI", "Anthropic",
]


def load_keywords(path: Path) -> list[str]:
    """Load unique watch keywords while preserving order."""
    if not path.exists():
        save_keywords(path, DEFAULT_KEYWORDS)
        return DEFAULT_KEYWORDS.copy()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    values = raw.get("keywords", raw) if isinstance(raw, dict) else raw
    if not isinstance(values, list):
        return []
    return normalize_keywords(values)


def save_keywords(path: Path, keywords: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"keywords": normalize_keywords(keywords)}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_keywords(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        keyword = str(value).strip()
        key = keyword.casefold()
        if keyword and key not in seen:
            seen.add(key)
            out.append(keyword)
    return out


def add_keyword(path: Path, keyword: str) -> bool:
    keywords = load_keywords(path)
    if keyword.strip().casefold() in {x.casefold() for x in keywords}:
        return False
    keywords.append(keyword.strip())
    save_keywords(path, keywords)
    return True


def remove_keyword(path: Path, keyword: str) -> bool:
    keywords = load_keywords(path)
    new_values = [x for x in keywords if x.casefold() != keyword.strip().casefold()]
    if len(new_values) == len(keywords):
        return False
    save_keywords(path, new_values)
    return True


def searchable_text(article: dict) -> str:
    parts = [
        article.get("title_cn", ""),
        article.get("title_original", ""),
        article.get("summary_cn", ""),
        article.get("rss_summary", ""),
        article.get("article_text", ""),
        # Translated body: Chinese-only monitor keywords (台积电, 英伟达, ...)
        # only catch English articles once we've translated them.
        article.get("article_text_cn", ""),
        article.get("publisher", ""),
        article.get("source_name", ""),
        " ".join(article.get("industry_tags") or []),
        " ".join(article.get("companies") or []),
    ]
    return "\n".join(str(x) for x in parts if x).casefold()


def matched_keywords(article: dict, keywords: Iterable[str]) -> list[str]:
    haystack = searchable_text(article)
    hits = []
    for kw in keywords:
        aliases = [a.strip() for a in kw.split("/") if a.strip()] or [kw]
        if any(a.casefold() in haystack for a in aliases):
            hits.append(kw)
    return hits
