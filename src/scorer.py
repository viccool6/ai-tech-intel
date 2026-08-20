from __future__ import annotations
import re
COMPANIES={"NVIDIA":["nvidia","英伟达"],"TSMC":["tsmc","台积电"],"OpenAI":["openai"],"Google":["google","deepmind"],"Microsoft":["microsoft","微软"],"Amazon":["amazon","aws"],"Meta":["meta"],"Anthropic":["anthropic"],"SK Hynix":["sk hynix","海力士"],"Samsung":["samsung","三星"],"Broadcom":["broadcom","博通"],"Intel":["intel","英特尔"],"AMD":["amd"],"SpaceX":["spacex"],"xAI":["xai","grok"]}
TAGS={"存储":["hbm","dram","nand","memory"],"先进封装":["cowos","advanced packaging","chiplet"],"晶圆代工":["foundry","wafer fab","tsmc"],"芯片设计":["gpu","asic","chip design"],"光通信":["cpo","optical","transceiver","inp"],"服务器/系统":["server","rack","system"],"云厂商":["cloud","aws","azure","google cloud"],"大模型":["model","llm","agent","openai","anthropic"],"电力/散热":["power","electricity","cooling","liquid cooling"],"设备":["euv","lithography","equipment"],"上游材料":["substrate","material","wafer"]}

def fallback(title: str, text: str) -> dict:
    blob = f"{title}\n{text}".lower()
    score = 2
    # 5 分只留真正表示产业动向的词：出口管制/资本开支/扩产。"billion" 之类
    # 纯金额词删掉——英文财经稿里随口提市值就会命中，太宽松。
    if any(k in blob for k in ["export control","capex","fab expansion","扩产","资本开支","出口管制"]): score=5
    elif any(k in blob for k in ["hbm","cowos","cpo","advanced packaging","supply chain"]): score=4
    elif any(k in blob for k in ["launch","release","earnings","model","product"]): score=3
    companies=[n for n,ws in COMPANIES.items() if any(w in blob for w in ws)]
    tags=[n for n,ws in TAGS.items() if any(w in blob for w in ws)]
    summary=re.sub(r"\s+"," ",text or "").strip()
    if len(summary)>380: summary=summary[:380]+"……"
    return {"title_cn":title,"summary_cn":summary or "AI不可用，请查看英文原文。","score":score,"score_reason":"AI接口不可用，暂按关键词规则评分，请人工复核。","industry_tags":tags[:5] or ["大模型"],"companies":companies[:10],"worth_attention":score>=4,"ai_status":"fallback","ai_model":"","ai_error":""}
