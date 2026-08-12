"""Bilingual translation for 4-5 分 English article bodies.

Design decisions:
- System prompt is intentionally FIXED (never templated with per-article data)
  so DeepSeek prompt caching can hit and cut input cost on subsequent calls.
  If you change the system prompt, cache is invalidated.
- Paragraphs are batched to ≤ TRANSLATE_BATCH_CHARS (default 3000) per API
  call. Each batch is independent — a failed batch leaves other batches
  intact and gets a [本段翻译失败] placeholder so paragraph alignment
  survives, letting the bilingual template zip .en/.zh 1:1.
- JSON I/O (input: {"paragraphs": [...]}, output: same shape) avoids the
  parsing fragility of custom delimiters.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

import requests

from .utils import env_bool

# Fixed across calls — do not template with article-specific data.
# Cost impact: DeepSeek prompt caching charges cache-hit input at ~50% of
# cache-miss; keeping this stable across the ~60 monthly translations means
# from call #2 onward the system-prompt tokens are billed at the discount.
# Any edit here invalidates the cache — worth it for prompt quality.
_SYSTEM_PROMPT = """你是资深的中英科技产业新闻翻译，服务对象是中国证券行业研究员。

任务：将下方 JSON `paragraphs` 列表中的英文段落逐段翻译为简体中文，返回同长度的中文段落列表。

【总则 · 优先级最高】
遇到任何不确定的专有名词、人名、机构名或产品名，一律保留原文，不要猜测、不要音译杜撰。此规则优先级高于下方"应译成中文"的要求——译错的人名会让读者查无此人，比留英文危害更大。

【格式铁律】
1. 段落数量与顺序必须与输入完全一致，一个不能多、一个不能少。
2. 忠实原文，不改写、不增减、不擅自扩写或压缩。
3. 译文只放翻译内容本身，不要加"第 X 段"之类的前缀。
4. 全文术语、公司名、人名在同一篇文章中必须用同一种译法。
5. 数字、百分比、日期原样保留数值，仅按下方规则翻译单位。

【必须译成中文】
- 数字单位（这条必须做到，不要偷懒保留英文）：
  * billion → 亿；million → 百万；trillion → 万亿；thousand → 千
  * 例：US$800 billion → 8000 亿美元；NT$500 billion → 5000 亿新台币；50 million units → 5000 万台
  * 数值不做汇率换算，只译单位
- 通用商业术语：
  * capex / capital expenditure → 资本开支
  * supply chain → 供应链
  * capacity → 产能
  * data center → 数据中心
  * revenue → 营收；earnings → 盈利（视上下文用"财报"）
  * guidance → 业绩指引；yield → 良率（半导体语境）
  * fab / foundry → 晶圆厂 / 代工厂；packaging → 封装
  * inference → 推理；training → 训练

【必须保留英文】
- 技术缩写与产品代号（不译）：
  HBM、HBM3E、HBM4、DRAM、NAND、SSD、CoWoS、CPO、GPU、ASIC、CPU、SoC、LLM、RAG、KV cache、EUV、WoW、UWIO、B200、H200、H100、GB200、GB300、Blackwell、Hopper、DGX
- 研究机构 / 咨询公司 / 媒体名（除非已有稳定通用中文译名）：
  * 保留英文：TrendForce、Gartner、IDC、Counterpoint、Reuters、Bloomberg、Next Apple、Mirror Daily
  * 有通用中文译名的可以用：Liberty Times →《自由时报》；Nikkei →《日经》；Financial Times →《金融时报》；Wall Street Journal →《华尔街日报》

【公司名 · 采用中文通用译名】
- NVIDIA → 英伟达；TSMC → 台积电；SK Hynix → SK 海力士；Micron → 美光；Broadcom → 博通
- Nanya Technology → 南亚科技；Samsung → 三星
- Google / Alphabet → 谷歌；Microsoft → 微软；Apple → 苹果；Tesla → 特斯拉
- 无通用中文译名者保留英文：Meta、OpenAI、Anthropic、DeepMind、AMD、Intel、Qualcomm

【人名 · 三档处理】
1. 有公认中文译名的用中文（判断标准：中文财经/科技媒体已长期稳定使用）：
   * Elon Musk → 马斯克；Jensen Huang → 黄仁勋；Sam Altman → 奥特曼；Lee Jae-yong → 李在镕
   * Satya Nadella → 纳德拉；Tim Cook → 库克；Mark Zuckerberg → 扎克伯格；Sundar Pichai → 皮查伊
2. 不在上述常见人物范围内的一律保留英文原名。**严禁音译或杜撰中文名**。
3. 首次出现时若原文提及身份/职务，把身份放在前面加姓名：
   * "南亚科技资深副总经理 Joseph Wu"、"英伟达 CEO 黄仁勋"
   * 后文再出现只用姓名（或对应中文译名）。原文未交代身份则不要凭空补
4. 同一个人在同一篇里不能一处中文一处英文。

【标题类短句】≤20 字的短句译文也保持简短，语气一致。

严格按以下 JSON 格式返回：{"paragraphs": ["段1中文", "段2中文", ...]}"""

_PLACEHOLDER = "[本段翻译失败，请对照左侧原文]"


def _configured_key() -> str:
    for name in ("TRANSLATE_API_KEY", "AI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _split_paragraphs(text: str) -> list[str]:
    """Same paragraph split policy as the display code so en/zh line up 1:1."""
    if not text:
        return []
    return [line.strip() for line in re.split(r"\n+", text) if line.strip()]


def _batch(paragraphs: list[str], max_chars: int) -> list[list[str]]:
    """Pack paragraphs into batches ≤ max_chars each, preserving order.
    A single paragraph longer than max_chars still ships alone (rare)."""
    batches: list[list[str]] = []
    cur: list[str] = []
    cur_len = 0
    for p in paragraphs:
        pl = len(p)
        if cur and cur_len + pl > max_chars:
            batches.append(cur)
            cur = [p]
            cur_len = pl
        else:
            cur.append(p)
            cur_len += pl
    if cur:
        batches.append(cur)
    return batches


def _call_api(batch: list[str], key: str, base: str, model: str, timeout: int, retries: int) -> tuple[list[str], dict]:
    """Translate one batch. Returns (paragraphs, usage) where usage is
    {'prompt_tokens': int, 'completion_tokens': int, 'total_tokens': int}
    (all zero if the provider didn't return usage). Raises RuntimeError on
    final failure."""
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps({"paragraphs": batch}, ensure_ascii=False)},
    ]
    payload = {
        "model": model,
        "messages": messages,
        "temperature": float(os.getenv("TRANSLATE_TEMPERATURE", "0.1")),
        "response_format": {"type": "json_object"},
    }
    # DeepSeek V4 defaults thinking ON, and thinking tokens count as output
    # billing even for deterministic tasks like translation. Explicitly disable
    # unless TRANSLATE_THINKING=true is set (opt-in for A/B or debugging).
    if not env_bool("TRANSLATE_THINKING", False):
        payload["thinking"] = {"type": "disabled"}
    errors: list[str] = []
    for attempt in range(retries + 1):
        try:
            response = requests.post(
                f"{base.rstrip('/')}/chat/completions",
                headers=headers, json=payload, timeout=timeout,
            )
            if response.status_code in {429, 500, 502, 503, 504} and attempt < retries:
                logging.warning("翻译接口暂不可用 HTTP %s，第 %s 次重试", response.status_code, attempt + 1)
                time.sleep(2 ** attempt + 1)
                continue
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            paragraphs = parsed.get("paragraphs")
            if not isinstance(paragraphs, list):
                raise ValueError("响应缺少 paragraphs 数组")
            paragraphs = [str(p).strip() for p in paragraphs]
            if len(paragraphs) != len(batch):
                raise ValueError(f"段数不一致：期望 {len(batch)}，实际 {len(paragraphs)}")
            u = data.get("usage") or {}
            usage = {
                "prompt_tokens": int(u.get("prompt_tokens") or 0),
                "completion_tokens": int(u.get("completion_tokens") or 0),
                "total_tokens": int(u.get("total_tokens") or 0),
            }
            return paragraphs, usage
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            logging.warning("翻译失败（第 %s/%s 次）：%s", attempt + 1, retries + 1, errors[-1])
            if attempt < retries:
                time.sleep(2 ** attempt + 1)
    raise RuntimeError(" | ".join(errors))


def translate_text(text: str, *, force_no_ai: bool = False) -> dict:
    """Translate an English article body paragraph-by-paragraph.

    Returns:
        dict with keys article_text_cn, translate_status, translate_model,
        translate_error. translate_status is one of:
          - 'success'  every batch translated
          - 'partial'  some batches failed; those paragraphs replaced with a
                       marker so alignment survives — retry via --translate
          - 'failed'   nothing translated
          - 'skipped'  disabled or missing key
    """
    if force_no_ai or not env_bool("TRANSLATE_ENABLED", True):
        reason = "命令行禁用翻译" if force_no_ai else "TRANSLATE_ENABLED=false"
        return {"article_text_cn": "", "translate_status": "skipped", "translate_model": "", "translate_error": reason}

    key = _configured_key()
    if not key:
        return {"article_text_cn": "", "translate_status": "skipped", "translate_model": "", "translate_error": "未配置 TRANSLATE_API_KEY 或兼容供应商密钥"}

    base = os.getenv("TRANSLATE_BASE_URL", os.getenv("AI_BASE_URL", "https://api.deepseek.com/v1"))
    model = os.getenv("TRANSLATE_MODEL", "deepseek-chat").strip()
    max_chars = int(os.getenv("TRANSLATE_BATCH_CHARS", "3000"))
    timeout = int(os.getenv("TRANSLATE_TIMEOUT", "180"))
    retries = int(os.getenv("TRANSLATE_RETRIES", "2"))

    paragraphs = _split_paragraphs(text)
    if not paragraphs:
        return {"article_text_cn": "", "translate_status": "skipped", "translate_model": model, "translate_error": "空正文"}

    batches = _batch(paragraphs, max_chars)
    thinking_state = "ON" if env_bool("TRANSLATE_THINKING", False) else "OFF"
    logging.info("翻译：%s 段，切成 %s 批（thinking=%s，model=%s）",
                 len(paragraphs), len(batches), thinking_state, model)

    out_paragraphs: list[str] = []
    errors: list[str] = []
    ok_batches = 0
    total_prompt = total_completion = 0
    for i, batch in enumerate(batches, 1):
        try:
            translated, usage = _call_api(batch, key, base, model, timeout, retries)
            out_paragraphs.extend(translated)
            ok_batches += 1
            total_prompt += usage["prompt_tokens"]
            total_completion += usage["completion_tokens"]
            logging.info("翻译批次 %s/%s 完成（%s 段｜prompt=%s / completion=%s tokens）",
                         i, len(batches), len(batch), usage["prompt_tokens"], usage["completion_tokens"])
        except Exception as exc:
            errors.append(f"batch {i}: {exc}")
            logging.warning("翻译批次 %s/%s 放弃：%s", i, len(batches), exc)
            out_paragraphs.extend([_PLACEHOLDER] * len(batch))

    logging.info("翻译 token 合计：prompt=%s / completion=%s / total=%s",
                 total_prompt, total_completion, total_prompt + total_completion)

    if ok_batches == 0:
        return {"article_text_cn": "", "translate_status": "failed", "translate_model": model, "translate_error": (" | ".join(errors))[-2000:]}

    article_text_cn = "\n\n".join(out_paragraphs)
    status = "success" if ok_batches == len(batches) else "partial"
    return {
        "article_text_cn": article_text_cn,
        "translate_status": status,
        "translate_model": model,
        "translate_error": (" | ".join(errors))[-2000:] if errors else "",
    }
