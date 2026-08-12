from __future__ import annotations

import json
import logging
import os
import time

import requests

from .scorer import fallback
from .utils import env_bool, extract_json

SCHEMA = {
    "type": "object",
    "properties": {
        "title_cn": {"type": "string"},
        "summary_cn": {"type": "string"},
        "score": {"type": "integer", "minimum": 1, "maximum": 5},
        "score_reason": {"type": "string"},
        "industry_tags": {"type": "array", "items": {"type": "string"}},
        "companies": {"type": "array", "items": {"type": "string"}},
        "worth_attention": {"type": "boolean"},
    },
    "required": [
        "title_cn", "summary_cn", "score", "score_reason",
        "industry_tags", "companies", "worth_attention",
    ],
    "additionalProperties": False,
}


def _prompt(article, rules, prompt_template):
    max_chars = int(os.getenv("AI_MAX_INPUT_CHARS", "12000"))
    body = (article.get("article_text") or article.get("rss_summary") or "")[:max_chars]
    return f"""{prompt_template}

重要性规则：
{json.dumps(rules.get("score_definitions", {}), ensure_ascii=False)}

允许标签：
{json.dumps(rules.get("industry_tags", []), ensure_ascii=False)}

质量纪律：
{json.dumps(rules.get("principles", []), ensure_ascii=False)}

信源：{article.get("source_name", "")}
发布日期：{article.get("published_at", "")}
原标题：{article.get("title_original", "")}
原文URL：{article.get("url", "")}

正文或RSS摘要：
{body}
"""


def _configured_key() -> str:
    """Prefer the generic key, while accepting common provider-specific names."""
    for name in (
        "AI_API_KEY",
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "SILICONFLOW_API_KEY",
    ):
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _fallback_with_error(article: dict, message: str) -> dict:
    result = fallback(
        article.get("title_original", ""),
        article.get("article_text") or article.get("rss_summary", ""),
    )
    result["ai_error"] = message
    return result


def analyze(article: dict, rules: dict, prompt_template: str, force_no_ai: bool = False) -> dict:
    if force_no_ai or not env_bool("AI_ENABLED", True):
        reason = "命令行已禁用AI" if force_no_ai else "AI_ENABLED=false"
        return _fallback_with_error(article, reason)

    key = _configured_key()
    if not key:
        return _fallback_with_error(article, "未配置AI_API_KEY或兼容的供应商密钥")

    base = os.getenv("AI_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
    model = os.getenv("AI_MODEL", "openrouter/free").strip()
    timeout = int(os.getenv("AI_TIMEOUT", "120"))
    retries = int(os.getenv("AI_RETRIES", "2"))
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if os.getenv("AI_HTTP_REFERER"):
        headers["HTTP-Referer"] = os.getenv("AI_HTTP_REFERER")
    if os.getenv("AI_APP_TITLE"):
        headers["X-Title"] = os.getenv("AI_APP_TITLE")

    messages = [
        {"role": "system", "content": "你是严谨的科技产业研究助理，只能依据输入材料。"},
        {"role": "user", "content": _prompt(article, rules, prompt_template)},
    ]
    payload = {
        "model": model,
        "messages": messages,
        "temperature": float(os.getenv("AI_TEMPERATURE", "0.1")),
    }
    # DeepSeek V4 defaults thinking ON, and thinking tokens count as output
    # billing. Summarization/scoring is structured extraction, not reasoning
    # — disable unless AI_THINKING=true is set (opt-in for A/B or debugging).
    if not env_bool("AI_THINKING", False):
        payload["thinking"] = {"type": "disabled"}
    if env_bool("AI_USE_JSON_SCHEMA", True):
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "news_analysis", "strict": True, "schema": SCHEMA},
        }
    else:
        payload["response_format"] = {"type": "json_object"}

    errors = []
    for attempt in range(retries + 1):
        try:
            response = requests.post(
                f"{base}/chat/completions",
                headers=headers,
                json=payload,
                timeout=timeout,
            )
            if response.status_code in {429, 500, 502, 503, 504} and attempt < retries:
                logging.warning(
                    "AI接口暂时不可用：HTTP %s，第%s次重试",
                    response.status_code,
                    attempt + 1,
                )
                time.sleep(2 ** attempt + 1)
                continue
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            u = data.get("usage") or {}
            logging.info(
                "AI 摘要 tokens：prompt=%s / completion=%s / total=%s（thinking=%s）",
                u.get("prompt_tokens", 0), u.get("completion_tokens", 0),
                u.get("total_tokens", 0),
                "ON" if env_bool("AI_THINKING", False) else "OFF",
            )
            out = extract_json(content)
            out["score"] = max(1, min(5, int(out.get("score", 1))))
            out["industry_tags"] = list(dict.fromkeys(out.get("industry_tags", [])))[:6]
            out["companies"] = list(dict.fromkeys(out.get("companies", [])))[:12]
            out["worth_attention"] = bool(out.get("worth_attention", out["score"] >= 4))
            out["ai_status"] = "success"
            out["ai_model"] = model
            out["ai_error"] = ""
            return out
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            errors.append(error)
            logging.warning("AI处理失败（第%s/%s次）：%s", attempt + 1, retries + 1, error)
            if attempt < retries:
                time.sleep(2 ** attempt + 1)

    return _fallback_with_error(article, " | ".join(errors)[-2000:])
