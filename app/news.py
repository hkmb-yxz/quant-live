# -*- coding: utf-8 -*-
"""实时金融热点新闻：RSS 聚合 + 关键词热度打分 + AI 中文解读。"""
import hashlib
import html
import re
import xml.etree.ElementTree as ET

from . import utils
from .ai import DeepSeekClient

DATA_FILE = utils.DATA_DIR + "/news.json"


def _parse_feed(xml_text: str) -> list[dict]:
    """解析 RSS/Atom，返回 [{title, link, published}]。"""
    items = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items
    for node in root.iter():
        tag = node.tag.split("}")[-1].lower()
        if tag not in ("item", "entry"):
            continue
        title = link = published = ""
        for child in node:
            ctag = child.tag.split("}")[-1].lower()
            text = (child.text or "").strip()
            if ctag == "title":
                title = html.unescape(text)
            elif ctag == "link":
                link = text or child.get("href", "")
            elif ctag in ("pubdate", "published", "updated", "date"):
                published = text
        if title and link.startswith("http"):
            items.append({"title": title, "link": link, "published": published[:25]})
    return items


def _score(title: str, hot_keywords: dict) -> tuple[float, list[str]]:
    low = title.lower()
    score = 0.0
    hits = []
    for kw, w in hot_keywords.items():
        if kw in low:
            score += w
            hits.append(kw)
    return score, hits


def _dedupe(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for it in items:
        key = hashlib.md5(re.sub(r"[^a-z0-9]+", "", it["title"].lower()).encode()).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def fetch_news(app_cfg: dict, history: list[dict] | None = None) -> list[dict]:
    """抓取全部 RSS 源，打分去重，与历史合并（同标题保留首次时间）。"""
    history = history or []
    known = {it.get("key", "") for it in history}
    fresh = []
    for feed in app_cfg.get("news_feeds", []):
        try:
            xml_text = utils.http_get(feed["url"], timeout=15, retries=1)
            if not xml_text:
                continue
            parsed = _parse_feed(xml_text)
            for p in parsed:
                score, kws = _score(p["title"], app_cfg.get("hot_keywords", {}))
                if score <= 0:
                    continue
                key = hashlib.md5(re.sub(r"[^a-z0-9]+", "", p["title"].lower()).encode()).hexdigest()
                fresh.append({
                    "key": key,
                    "title": p["title"],
                    "link": p["link"],
                    "source": feed["name"],
                    "published": p["published"],
                    "keywords": kws,
                    "score": round(score, 1),
                    "first_seen": utils.bj_now_str() if key not in known else next(
                        (h.get("first_seen") for h in history if h.get("key") == key),
                        utils.bj_now_str(),
                    ),
                })
        except Exception as e:  # noqa: BLE001
            print(f"[news] 源 {feed['name']} 失败: {e}")
    fresh = _dedupe(fresh)
    fresh.sort(key=lambda x: (-x["score"], x.get("first_seen", "")))
    merged = fresh + history
    merged = _dedupe(merged)
    merged.sort(key=lambda x: x.get("first_seen", ""), reverse=True)
    return merged[:300]


def ai_summarize(items: list[dict], client: DeepSeekClient) -> dict | None:
    """让 DeepSeek 批量解读前 N 条新闻，返回 {items:[...], updated_at}。"""
    if not client.available or not items:
        return None
    titles = [f"{i+1}. {it['title']} (来源:{it['source']})" for i, it in enumerate(items)]
    system = (
        "你是资深美股市场分析师。用户给你一组英文财经新闻标题，请输出 JSON 对象，"
        '格式为 {"items":[{"idx":序号,"title_cn":"简洁中文标题(25字内)","sentiment":"利好/利空/中性",'
        '"impact":1到5整数(5为影响最大),"summary":"一句话中文解读(60字内)","sectors":["受影响的板块，如 科技/半导体/能源，可空数组"]}]}。'
        "idx 必须与输入序号对应，逐条输出，不要省略任何一条。"
    )
    user = "\n".join(titles)
    data = client.chat_json(system, user, expect_keys={"items"})
    if not data:
        return None
    by_idx = {}
    for it in data.get("items", []):
        try:
            by_idx[int(it["idx"])] = it
        except (KeyError, ValueError, TypeError):
            continue
    for i, it in enumerate(items):
        ai = by_idx.get(i + 1, {})
        it["title_cn"] = ai.get("title_cn") or it["title"]
        it["sentiment"] = ai.get("sentiment") or "中性"
        it["impact"] = int(ai.get("impact") or 0)
        it["summary"] = ai.get("summary") or ""
        it["sectors"] = ai.get("sectors") or []
    return {"updated_at": utils.bj_now_str(), "ai_enabled": True}


def run_news(app_cfg: dict, client: DeepSeekClient, force_ai: bool = False) -> dict:
    """新闻管线主入口，返回 {ok, count, ai_done}。"""
    t0 = utils.timer_ms()
    prev = utils.load_json(DATA_FILE, default={"items": []})
    history = prev.get("items", [])
    items = fetch_news(app_cfg, history)
    top = items[: int(app_cfg.get("top_news_n", 20))]
    ai_done = False
    last_ai = prev.get("ai_updated_at")
    interval = int(app_cfg.get("ai", {}).get("news_interval_min", 30))
    due = utils.minutes_ago(last_ai) is None or utils.minutes_ago(last_ai) >= interval
    if (force_ai or due) and client.available:
        res = ai_summarize(top, client)
        if res:
            prev["ai_updated_at"] = res["updated_at"]
            ai_done = True
        else:
            # AI 失败：用关键词规则兜底标注
            for it in top:
                if "title_cn" not in it:
                    it["title_cn"] = it["title"]
                    it["sentiment"] = "中性"
                    it["impact"] = 0
                    it["summary"] = ""
                    it["sectors"] = []
    prev["updated_at"] = utils.bj_now_str()
    prev["count"] = len(items)
    prev["ai_enabled"] = client.available
    prev["items"] = items
    ok = utils.save_json(DATA_FILE, prev)
    return {"ok": ok, "count": len(items), "ai_done": ai_done, "sec": utils.elapsed_sec(t0)}
