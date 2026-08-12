from __future__ import annotations
import argparse, json, logging, os, shutil, sys
from pathlib import Path
from dotenv import load_dotenv
from src.ai_client import analyze
from src.article_parser import extract_article
from src.fetchers import fetch_source
from src.html_generator import HtmlGenerator
from src.keywords import add_keyword, load_keywords, remove_keyword
from src.storage import Storage
from src.translator import translate_text
from src.utils import env_bool, load_json, normalize_url

BASE=Path(__file__).resolve().parent
load_dotenv(BASE/".env")

def args():
    p=argparse.ArgumentParser(description="AI科技产业情报系统 V4.4")
    p.add_argument("--hours",type=int,default=24)
    p.add_argument("--max-items",type=int,default=3,help="每个信源最多条数")
    p.add_argument("--source",help="仅运行指定source id")
    p.add_argument("--no-ai",action="store_true")
    p.add_argument("--dry-run",action="store_true")
    p.add_argument("--reset",action="store_true")
    p.add_argument("--rebuild-index",action="store_true")
    p.add_argument("--refetch-content",action="store_true",help="重新解析历史原文链接并补抓正文")
    p.add_argument("--refetch-only-summary",action="store_true",help="与--refetch-content配合，仅处理正文不完整/失败的文章")
    p.add_argument("--refetch-limit",type=int,default=0,help="历史正文重抓篇数上限，0表示不限制")
    p.add_argument("--repair-dates",action="store_true",help="重新访问历史文章并修复真实发布日期/原文链接")
    p.add_argument("--reprocess-ai",action="store_true",help="对历史文章重新执行AI翻译、摘要和评分，不重新抓取")
    p.add_argument("--reprocess-only-fallback",action="store_true",help="与--reprocess-ai配合，仅处理此前AI失败或回退的文章")
    p.add_argument("--reprocess-limit",type=int,default=0,help="历史AI重处理篇数上限，0表示不限制")
    p.add_argument("--add-keyword",help="新增一个监控关键词后重建索引")
    p.add_argument("--remove-keyword",help="删除一个监控关键词后重建索引")
    p.add_argument("--list-keywords",action="store_true",help="列出全部监控关键词")
    p.add_argument("--translate",action="store_true",help="对历史 4-5 分文章批量翻译正文（不重新抓取）")
    p.add_argument("--only-missing",action="store_true",help="与 --translate 配合，只处理未成功翻译的文章（跳过已 success）")
    p.add_argument("--translate-limit",type=int,default=0,help="--translate 处理篇数上限，0=不限")
    p.add_argument("--translate-id",type=int,default=0,help="只翻这一篇（按 articles.id），绕过 score/fetch/length/only-missing 全部筛选；用于改完提示词后强制重翻")
    p.add_argument("--no-translate",action="store_true",help="常规抓取时禁用自动翻译（省 API 或 dry-run）")
    return p.parse_args()


def _should_translate(item: dict) -> bool:
    """Auto-translation gate: score>=4, fetch succeeded, body long enough.
    Kept here (not in translator.py) because translator only knows text."""
    return (
        int(item.get("score") or 0) >= 4
        and item.get("fetch_status") == "success"
        and len(item.get("article_text") or "") > 500
    )

def logging_setup():
    d=BASE/"data/logs"; d.mkdir(parents=True,exist_ok=True)
    logging.basicConfig(level=logging.INFO,format="%(asctime)s | %(levelname)s | %(message)s",handlers=[logging.StreamHandler(sys.stdout),logging.FileHandler(d/"latest.log",encoding="utf-8")])

def reset():
    db=BASE/"data/news.db"
    if db.exists(): db.unlink()
    archive=BASE/"档案库"
    for p in archive.iterdir():
        if p.name=="专题": continue
        shutil.rmtree(p) if p.is_dir() else p.unlink()

def main():
    a=args(); logging_setup()
    if a.reset: reset(); logging.info("已清空测试数据库和归档")
    sources=[s for s in load_json(BASE/"config/sources.json").get("sources",[]) if s.get("enabled")]
    if a.source:
        sources=[s for s in sources if s.get("id")==a.source]
        if not sources: logging.error("找不到source id：%s",a.source); return 2
    keyword_path=BASE/"config/watch_keywords.json"
    keywords=load_keywords(keyword_path)
    if a.list_keywords:
        print("\n".join(f"{i+1}. {keyword}" for i,keyword in enumerate(keywords)) or "尚未设置关键词")
        return 0
    if a.add_keyword:
        changed=add_keyword(keyword_path,a.add_keyword)
        keywords=load_keywords(keyword_path)
        print(f"已新增关键词：{a.add_keyword}" if changed else f"关键词已存在：{a.add_keyword}")
    if a.remove_keyword:
        changed=remove_keyword(keyword_path,a.remove_keyword)
        keywords=load_keywords(keyword_path)
        print(f"已删除关键词：{a.remove_keyword}" if changed else f"未找到关键词：{a.remove_keyword}")
    rules=load_json(BASE/"config/scoring_rules.json")
    prompt=(BASE/"prompts/news_analysis.md").read_text(encoding="utf-8")
    storage=Storage(BASE/"data/news.db"); html=HtmlGenerator(BASE/"templates",BASE/"档案库",keywords=keywords)
    if a.add_keyword or a.remove_keyword:
        articles=html.rebuild_articles(storage.all(),storage)
        html.index(articles); storage.close(); logging.info("关键词已更新，详情页和索引已重建"); return 0
    if a.rebuild_index:
        articles=html.rebuild_articles(storage.all(),storage)
        html.index(articles); storage.close(); logging.info("详情页和索引已重建"); return 0
    if a.refetch_content:
        articles=storage.all()
        if a.refetch_only_summary:
            articles=[item for item in articles if item.get("fetch_status") != "success"]
        if a.refetch_limit > 0:
            articles=articles[:a.refetch_limit]
        complete=partial=failed=0
        for index, article in enumerate(articles, 1):
            target=article.get("original_url") or article.get("url")
            if not target:
                continue
            logging.info("历史正文重抓 [%s/%s]：%s",index,len(articles),article.get("title_original",""))
            parsed=extract_article(target, expected_title=article.get("title_original", ""))
            metadata={
                "url": normalize_url(parsed.get("final_url") or article.get("url") or target),
                "publisher": parsed.get("publisher") or article.get("publisher", ""),
                "author": parsed.get("author") or article.get("author", ""),
                "main_image": parsed.get("main_image") or article.get("main_image", ""),
                "article_text": parsed.get("text") or article.get("article_text", ""),
                "fetch_status": parsed.get("fetch_status", "failed"),
                "fetch_error": parsed.get("fetch_error", ""),
                "link_resolver": parsed.get("link_resolver", ""),
                "crawled_at": __import__("datetime").datetime.now().astimezone().isoformat(),
            }
            if parsed.get("published_at"):
                metadata["published_at"]=parsed["published_at"]
                metadata["date_source"]=parsed.get("date_source") or "webpage"
            storage.update_metadata(int(article["id"]),metadata)
            if parsed.get("fetch_status") == "success":
                complete+=1
                refreshed=dict(article); refreshed.update(metadata)
                result=analyze(refreshed,rules,prompt,force_no_ai=a.no_ai)
                storage.update_ai_result(int(article["id"]),result)
            elif parsed.get("fetch_status") == "summary_only": partial+=1
            else: failed+=1
        rebuilt=html.rebuild_articles(storage.all(),storage)
        html.index(rebuilt); storage.close()
        print(f"历史正文重抓完成：完整{complete}篇｜摘要级{partial}篇｜失败{failed}篇｜合计{len(articles)}篇")
        return 0

    if a.reprocess_ai:
        articles=storage.all()
        if a.reprocess_only_fallback:
            articles=[item for item in articles if item.get("ai_status") != "success"]
        if a.reprocess_limit > 0:
            articles=articles[:a.reprocess_limit]
        success=fallback_count=0
        for index, article in enumerate(articles, 1):
            logging.info("历史AI重处理 [%s/%s]：%s",index,len(articles),article.get("title_original",""))
            result=analyze(article,rules,prompt,force_no_ai=a.no_ai)
            storage.update_ai_result(int(article["id"]),result)
            if result.get("ai_status") == "success":
                success+=1
            else:
                fallback_count+=1
                logging.warning("历史AI重处理回退：%s",result.get("ai_error") or "未知原因")
        rebuilt=html.rebuild_articles(storage.all(),storage)
        html.index(rebuilt); storage.close()
        print(f"历史AI重处理完成：成功{success}篇｜回退{fallback_count}篇｜合计{len(articles)}篇")
        return 0

    if a.repair_dates:
        repaired=verified=failed=0
        for article in storage.all():
            target=article.get("url") or article.get("original_url")
            if not target:
                continue
            logging.info("修复日期：%s",article.get("title_original",""))
            parsed=extract_article(target, expected_title=article.get("title_original", ""))
            metadata={
                "url": normalize_url(parsed.get("final_url") or target),
                "publisher": parsed.get("publisher") or article.get("publisher", ""),
                "author": parsed.get("author") or article.get("author", ""),
                "updated_at": "",
                "main_image": parsed.get("main_image") or article.get("main_image", ""),
                "fetch_status": parsed.get("fetch_status", ""),
                "fetch_error": parsed.get("fetch_error", ""),
                "crawled_at": __import__("datetime").datetime.now().astimezone().isoformat(),
            }
            if parsed.get("published_at"):
                metadata["published_at"]=parsed["published_at"]
                metadata["date_source"]=parsed.get("date_source") or "webpage"
                verified+=1
            else:
                # Clear old RSS dates that had previously been stored as if they were true publication dates.
                metadata["published_at"]=""
                metadata["date_source"]="rss" if article.get("rss_published_at") else "unknown"
                failed+=1
            if parsed.get("text"):
                metadata["article_text"]=parsed["text"]
            storage.update_metadata(int(article["id"]),metadata)
            repaired+=1
        articles=html.rebuild_articles(storage.all(),storage)
        html.index(articles); storage.close()
        print(f"历史日期修复完成：处理{repaired}篇｜网页识别{verified}篇｜页面不显示日期{failed}篇")
        return 0

    if a.translate:
        candidates=storage.articles_needing_translation(
            only_missing=a.only_missing,
            limit=a.translate_limit,
            article_id=a.translate_id or None,
        )
        if not candidates:
            if a.translate_id:
                print(f"数据库里没有 id={a.translate_id} 的文章")
            else:
                print("没有符合条件的文章需要翻译（score>=4 且 fetch=success 且正文>500 字"+ ("；已排除 translate_status=success 的" if a.only_missing else "") +"）")
            storage.close(); return 0
        ok=partial=failed=skipped=0
        for index, article in enumerate(candidates, 1):
            logging.info("翻译 [%s/%s]（score %s / %s 字）：%s",
                         index, len(candidates), article.get("score"),
                         len(article.get("article_text") or ""),
                         article.get("title_original",""))
            result=translate_text(article.get("article_text") or "", force_no_ai=a.no_ai)
            storage.update_translation(int(article["id"]),
                                        result.get("article_text_cn",""),
                                        result.get("translate_status",""),
                                        result.get("translate_model",""))
            status=result.get("translate_status","")
            if status=="success": ok+=1
            elif status=="partial": partial+=1
            elif status=="skipped": skipped+=1
            else: failed+=1
            if result.get("translate_error"):
                logging.warning("翻译提示：%s", result["translate_error"])
        storage.close()
        print(f"翻译完成：成功{ok}篇｜部分{partial}篇｜失败{failed}篇｜跳过{skipped}篇｜合计{len(candidates)}篇")
        return 0

    fetched=new=dup=content_fail=summary_only=translated_ok=translated_fail=0
    threshold=float(os.getenv("TITLE_SIMILARITY_THRESHOLD","0.88"))
    for source in sources:
        logging.info("抓取：%s",source.get("name"))
        items,warning=fetch_source(source,a.hours,a.max_items)
        if warning: logging.warning("%s：%s",source.get("name"),warning)
        fetched+=len(items)
        for item in items:
            if storage.exists_url(item["url"]) or storage.similar_title(item["title_original"],threshold):
                dup+=1; continue
            item["original_url"]=normalize_url(item["url"])
            item["rss_published_at"]=item.get("published_at","")
            parsed=extract_article(item["url"], expected_title=item.get("title_original", ""))
            item["url"]=normalize_url(parsed.get("final_url") or item["url"])
            item["article_text"]=parsed.get("text",""); item["main_image"]=parsed.get("main_image","")
            item["publisher"]=parsed.get("publisher",""); item["author"]=parsed.get("author","")
            item["updated_at"]=""
            if parsed.get("published_at"):
                item["published_at"]=parsed["published_at"]
                item["date_source"]=parsed.get("date_source") or "webpage"
            else:
                item["published_at"]=""
                item["date_source"]="rss" if item.get("rss_published_at") else "unknown"
            item["crawled_at"]=__import__("datetime").datetime.now().astimezone().isoformat()
            item["fetch_status"]=parsed.get("fetch_status",""); item["fetch_error"]=parsed.get("fetch_error",""); item["link_resolver"]=parsed.get("link_resolver","")
            if item.get("fetch_status") == "summary_only":
                summary_only+=1
            if not item["article_text"] and not (env_bool("ALLOW_RSS_SUMMARY_FALLBACK",True) and item["rss_summary"]):
                content_fail+=1; continue
            if a.dry_run:
                print(json.dumps(item,ensure_ascii=False,indent=2)[:3000]); continue
            item.update(analyze(item,rules,prompt,force_no_ai=a.no_ai))
            # Auto-translate 4-5 分 English bodies before save so article_text_cn
            # lands in the same INSERT. --no-translate disables this leg entirely.
            if not a.no_translate and _should_translate(item):
                tr=translate_text(item.get("article_text") or "", force_no_ai=a.no_ai)
                item.update(tr)
                if tr.get("translate_status")=="success": translated_ok+=1
                elif tr.get("translate_status") in ("partial","failed"): translated_fail+=1
                if tr.get("translate_error"):
                    logging.info("翻译提示：%s", tr["translate_error"])
            item["archive_file"]=html.article(item)
            storage.save(item); new+=1
            logging.info("归档 [%s分/%s] %s",item["score"],item["ai_status"],item["title_cn"])
    if not a.dry_run: html.index(storage.all())
    storage.close()
    print("\n===== 运行报告 =====")
    print(f"抓取：{fetched}｜新增：{new}｜重复：{dup}｜完整正文不足：{summary_only}｜内容失败：{content_fail}")
    print(f"翻译：{translated_ok}成功｜{translated_fail}失败或部分")
    print(f"索引：{BASE/'档案库/index.html'}")
    if not any(os.getenv(name) for name in ("AI_API_KEY","OPENROUTER_API_KEY","OPENAI_API_KEY","DEEPSEEK_API_KEY","SILICONFLOW_API_KEY")) and not a.no_ai: print("提示：未配置AI_API_KEY或兼容供应商密钥，本次自动使用规则回退。")
    return 0

if __name__=="__main__": raise SystemExit(main())
