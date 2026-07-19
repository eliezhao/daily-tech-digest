#!/usr/bin/env python3.11
"""
scrapers.py — 多源数据采集模块

Sources:
  Events  : Meetup (多城市 Apollo cache)  + 活动行 (事件 URL 列表)
  Funding : TechCrunch RSS (多 tag)       + 36kr 简单抓取
  Market  : akshare A股板块行情 + 财联社快讯
  Macro   : 华尔街见闻宏观日历 (akshare macro_info_ws) + 财联社政策快讯
"""

import json
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timedelta, timezone

import feedparser
import requests
from bs4 import BeautifulSoup

# ── HTTP 配置 ────────────────────────────────────────────────────────────────
# akshare 内部的 requests 调用不设超时（requests timeout=None 会永久阻塞，
# socket.setdefaulttimeout 拦不住），必须用守护线程加墙钟超时兜底
def _call_with_timeout(fn, timeout_s: float = 30):
    """在守护线程中执行 fn，超时抛 TimeoutError（挂死的线程不阻塞进程退出）。"""
    import threading
    result = {"value": None, "exc": None}

    def _runner():
        try:
            result["value"] = fn()
        except Exception as e:
            result["exc"] = e

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        raise TimeoutError(f"调用超时（>{timeout_s}s）")
    if result["exc"] is not None:
        raise result["exc"]
    return result["value"]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

def _get(url: str, timeout: int = 15) -> Optional[requests.Response]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return r
    except Exception as e:
        print(f"  [SCRAPE WARN] {url[:60]}: {e}")
        return None

# ── 数据结构 ─────────────────────────────────────────────────────────────────
@dataclass
class EventItem:
    title: str
    date_str: str = ""
    venue: str = ""
    city: str = ""
    url: str = ""
    organizer: str = ""
    source: str = ""

@dataclass
class FundingItem:
    title: str
    url: str = ""
    summary: str = ""
    published: str = ""
    source: str = ""

# ════════════════════════════════════════════════════════════════════════════
# EVENTS
# ════════════════════════════════════════════════════════════════════════════

# ── Meetup (多城市) ──────────────────────────────────────────────────────────
MEETUP_SEARCHES = [
    # (label, keywords, location_slug)
    ("深圳",     "AI tech",      "cn--guangdong--shenzhen"),
    ("广州",     "AI tech",      "cn--guangdong--guangzhou"),
    ("上海",     "AI tech",      "cn--shanghai--shanghai"),
    ("香港",     "AI tech",      "hk--hong-kong"),
    ("新加坡",   "AI cloud",     "sg--singapore"),
    ("东京",     "AI startup",   "jp--tokyo"),
    ("首尔",     "AI tech",      "kr--seoul"),
    ("台北",     "AI tech",      "tw--taipei"),
    ("吉隆坡",   "AI tech",      "my--selangor--kuala-lumpur"),
]

def _parse_meetup_apollo(html: str) -> list[EventItem]:
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>',
        html, re.DOTALL
    )
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
        apollo = (data.get("props", {})
                      .get("pageProps", {})
                      .get("__APOLLO_STATE__", {}))
    except Exception:
        return []

    items = []
    now = datetime.now(tz=timezone.utc)
    cutoff = now + timedelta(days=30)   # 只要未来30天的活动

    for val in apollo.values():
        if not isinstance(val, dict):
            continue
        if val.get("__typename") not in ("Event",):
            continue
        title = val.get("title", "").strip()
        if not title:
            continue
        date_str = val.get("dateTime", "")
        # 过滤过去活动
        try:
            dt = datetime.fromisoformat(date_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt < now or dt > cutoff:
                continue
        except Exception:
            pass
        venue_obj = val.get("venue") or {}
        if isinstance(venue_obj, dict):
            venue = venue_obj.get("name", "")
            city  = venue_obj.get("city", "")
        else:
            venue = city = ""
        items.append(EventItem(
            title=title,
            date_str=date_str,
            venue=venue,
            city=city,
            url=val.get("eventUrl", ""),
            organizer=val.get("group", {}).get("name", "") if isinstance(val.get("group"), dict) else "",
            source="Meetup",
        ))
    return items

def scrape_meetup_events() -> list[EventItem]:
    results = []
    seen = set()
    for label, kw, loc in MEETUP_SEARCHES:
        url = f"https://www.meetup.com/find/?keywords={kw.replace(' ','+')}&location={loc}&source=EVENTS"
        r = _get(url)
        if not r:
            continue
        items = _parse_meetup_apollo(r.text)
        new = 0
        for it in items:
            key = it.url or it.title
            if key not in seen:
                seen.add(key)
                results.append(it)
                new += 1
        print(f"  [Meetup] {label}: {new} 新活动")
        time.sleep(0.6)
    return results

# ── 活动行 — 收集事件 URL (列表页静态可抓) ───────────────────────────────────
HDX_SEARCHES = [
    ("深圳", "AI"),
    ("深圳", "云计算"),
    ("深圳", "软件"),
    ("广州", "AI"),
    ("广州", "云服务"),
    ("深圳", "创业"),
]

def scrape_huodongxing_urls() -> list[str]:
    """返回活动行上找到的活动 URL 列表（detail 页 JS 渲染，供 Kimi 后续查询）"""
    urls = []
    seen = set()
    for city, kw in HDX_SEARCHES:
        url = f"https://www.huodongxing.com/search?kw={kw}&ct={city}&status=0"
        r = _get(url)
        if not r:
            continue
        soup = BeautifulSoup(r.text, "lxml")
        links = [a["href"] for a in soup.find_all("a", href=True)
                 if re.match(r"/event/\d+", a["href"])]
        new = 0
        for path in links:
            full = f"https://www.huodongxing.com{path}"
            if full not in seen:
                seen.add(full)
                urls.append(full)
                new += 1
        print(f"  [活动行] {city}/{kw}: {new} URLs")
        time.sleep(0.4)
    return list(dict.fromkeys(urls))  # 去重保序

# ════════════════════════════════════════════════════════════════════════════
# FUNDING
# ════════════════════════════════════════════════════════════════════════════

# ── TechCrunch RSS (多 tag) ──────────────────────────────────────────────────
TC_FEEDS = [
    ("TC Asia",       "https://techcrunch.com/tag/asia/feed/"),
    ("TC China",      "https://techcrunch.com/tag/china/feed/"),
    ("TC Japan",      "https://techcrunch.com/tag/japan/feed/"),
    ("TC Korea",      "https://techcrunch.com/tag/korea/feed/"),
    ("TC Singapore",  "https://techcrunch.com/tag/singapore/feed/"),
    ("TC India",      "https://techcrunch.com/tag/india/feed/"),
    ("TC Funding",    "https://techcrunch.com/category/funding/feed/"),
    ("TC Startups",   "https://techcrunch.com/category/startups/feed/"),
]

def fetch_techcrunch_rss() -> list[FundingItem]:
    results = []
    seen = set()
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=72)

    for label, feed_url in TC_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            new = 0
            for e in feed.entries:
                if e.link in seen:
                    continue
                # 时间过滤
                pub = e.get("published_parsed")
                if pub:
                    dt = datetime(*pub[:6], tzinfo=timezone.utc)
                    if dt < cutoff:
                        continue
                seen.add(e.link)
                results.append(FundingItem(
                    title=e.get("title", ""),
                    url=e.get("link", ""),
                    summary=(e.get("summary", "") or "")[:600],
                    published=e.get("published", ""),
                    source=label,
                ))
                new += 1
            print(f"  [TC RSS] {label}: {new} 新条目")
        except Exception as ex:
            print(f"  [TC RSS WARN] {label}: {ex}")
        time.sleep(0.3)

    return results

# ── 36kr — 尝试抓取融资相关文章列表 ─────────────────────────────────────────
KR_KEYWORDS = ["深圳融资", "广州融资", "广东融资", "AI融资", "云服务融资", "创业融资"]

def scrape_36kr_headlines() -> list[FundingItem]:
    """
    36kr 搜索页是 SSR，部分内容可以直接抓到标题+链接。
    仅用于给 Kimi 提供候选 URL，不做深度解析。
    """
    results = []
    seen = set()
    for kw in KR_KEYWORDS:
        url = f"https://36kr.com/search/articles/{kw}"
        r = _get(url)
        if not r:
            continue
        soup = BeautifulSoup(r.text, "lxml")
        # 36kr SSR 返回的文章条目
        for tag in soup.find_all(["a"], href=True):
            href = tag["href"]
            if not re.match(r"^/p/\d+", href):
                continue
            full = f"https://36kr.com{href}"
            if full in seen:
                continue
            seen.add(full)
            title = tag.get_text(strip=True)
            if len(title) > 8:
                results.append(FundingItem(title=title, url=full, source="36kr"))
        time.sleep(0.4)
    print(f"  [36kr] 共抓到 {len(results)} 条候选标题")
    return results

# ── 统一入口 ─────────────────────────────────────────────────────────────────
def collect_all_events() -> tuple[list[EventItem], list[str]]:
    """返回 (meetup结构化事件, 活动行URL列表)"""
    print("[Scraper] 采集 Meetup 活动...")
    meetup = scrape_meetup_events()
    print(f"  → Meetup 合计 {len(meetup)} 条")

    print("[Scraper] 采集 活动行 URL...")
    hdx_urls = scrape_huodongxing_urls()
    print(f"  → 活动行 URL {len(hdx_urls)} 条")

    return meetup, hdx_urls

def collect_all_funding() -> list[FundingItem]:
    """返回所有 RSS + 抓取的融资条目"""
    print("[Scraper] 采集 TechCrunch RSS...")
    tc_items = fetch_techcrunch_rss()

    print("[Scraper] 采集 36kr 标题...")
    kr_items = scrape_36kr_headlines()

    all_items = tc_items + kr_items
    print(f"  → 融资原始数据合计 {len(all_items)} 条")
    return all_items

# ════════════════════════════════════════════════════════════════════════════
# A股科技股行情 + 财联社快讯（akshare 数据源）
# ════════════════════════════════════════════════════════════════════════════

# 关注的科技概念板块关键字（akshare 概念板块名称匹配）
TECH_CONCEPT_KEYWORDS = [
    "人工智能", "AIGC", "大模型", "算力", "AI芯片", "GPU",
    "半导体", "芯片", "存储芯片", "EDA", "光刻机",
    "云计算", "云服务", "数据要素", "数据中心",
    "软件开发", "操作系统", "鸿蒙", "信创",
    "智能驾驶", "机器人", "人形机器人", "智能座舱",
    "数字经济", "金融科技", "区块链",
]

def is_a_stock_open_today() -> bool:
    """简易判断今日是否 A 股交易日。失败时默认 True（保证数据采集尝试）。"""
    try:
        import akshare as ak
        df = _call_with_timeout(ak.tool_trade_date_hist_sina, 30)
        today = datetime.now().strftime("%Y-%m-%d")
        return today in df["trade_date"].astype(str).values
    except Exception as e:
        print(f"  [Market WARN] 交易日判断失败: {e}（默认按交易日处理）")
        return True

def fetch_tech_concept_sectors(top_n: int = 10) -> list[dict]:
    """获取科技相关概念板块涨跌幅，按涨幅排序返回 TOP N。"""
    try:
        import akshare as ak
        df = _call_with_timeout(ak.stock_board_concept_name_em, 30)
    except Exception as e:
        print(f"  [Market WARN] 板块数据获取失败: {e}")
        return []

    name_col = "板块名称" if "板块名称" in df.columns else df.columns[1]
    chg_col = next((c for c in df.columns if "涨跌幅" in c), None)
    if not chg_col:
        return []

    mask = df[name_col].astype(str).apply(
        lambda n: any(k in n for k in TECH_CONCEPT_KEYWORDS)
    )
    sub = df[mask].copy()
    if sub.empty:
        return []
    sub[chg_col] = sub[chg_col].astype(float, errors="ignore")
    sub = sub.sort_values(chg_col, ascending=False).head(top_n)

    out = []
    for _, row in sub.iterrows():
        out.append({
            "name": str(row.get(name_col, "")),
            "change_pct": float(row.get(chg_col, 0) or 0),
            "leader": str(row.get("领涨股票", "") or row.get("领涨股", "")),
            "stocks_up": int(row.get("上涨家数", 0) or 0) if "上涨家数" in df.columns else 0,
            "stocks_down": int(row.get("下跌家数", 0) or 0) if "下跌家数" in df.columns else 0,
        })
    return out

def fetch_concept_top_stocks(concept: str, n: int = 3) -> list[dict]:
    """获取某概念板块涨幅前 N 个成分股。"""
    try:
        import akshare as ak
        df = _call_with_timeout(lambda: ak.stock_board_concept_cons_em(symbol=concept), 30)
    except Exception as e:
        print(f"  [Market WARN] 板块 {concept} 成分股获取失败: {e}")
        return []

    chg_col = next((c for c in df.columns if "涨跌幅" in c), None)
    name_col = "名称" if "名称" in df.columns else None
    code_col = "代码" if "代码" in df.columns else None
    price_col = "最新价" if "最新价" in df.columns else None
    if not (chg_col and name_col):
        return []

    sub = df.sort_values(chg_col, ascending=False).head(n)
    return [
        {
            "name": str(r.get(name_col, "")),
            "code": str(r.get(code_col, "") or ""),
            "price": float(r.get(price_col, 0) or 0) if price_col else 0.0,
            "change_pct": float(r.get(chg_col, 0) or 0),
        }
        for _, r in sub.iterrows()
    ]

def fetch_north_bound_flow() -> Optional[dict]:
    """北向资金当日净流入（单位：亿元）。"""
    try:
        import akshare as ak
        df = _call_with_timeout(ak.stock_hsgt_fund_flow_summary_em, 30)
        if df is None or df.empty:
            return None
        # 取北上汇总行
        net_col = next((c for c in df.columns if "净" in c and ("买" in c or "流入" in c)), None)
        type_col = df.columns[0]
        north = df[df[type_col].astype(str).str.contains("北向|沪股通|深股通", na=False)]
        if north.empty or not net_col:
            return None
        total = north[net_col].astype(float, errors="ignore").sum()
        return {"net_inflow_yi": float(total)}
    except Exception as e:
        print(f"  [Market WARN] 北向资金获取失败: {e}")
        return None

# 财联社快讯过滤关键词：科技/资本（市场日报用）
CLS_TECH_KEYWORDS = [
    "AI", "人工智能", "大模型", "算力", "芯片", "半导体", "GPU",
    "云", "软件", "SaaS", "数据", "鸿蒙", "操作系统", "机器人",
    "智能驾驶", "自动驾驶", "OpenAI", "英伟达", "腾讯", "阿里",
    "华为", "比亚迪", "字节", "百度", "中芯", "寒武纪",
    "融资", "投资", "上市", "IPO", "并购", "回购", "增持",
]

# 财联社快讯过滤关键词：政策/宏观（政策快报用）
CLS_POLICY_KEYWORDS = [
    "国务院", "国常会", "中共中央", "政治局", "中央财经", "五年规划",
    "央行", "人民银行", "发改委", "财政部", "证监会", "工信部", "商务部",
    "金融监管总局", "网信办", "国资委", "税务总局", "海关总署", "国家统计局",
    "政策", "新规", "条例", "办法", "细则", "指导意见", "行动方案", "试点",
    "降准", "降息", "LPR", "逆回购", "MLF", "专项债", "特别国债", "关税", "补贴",
]

BEIJING_TZ = timezone(timedelta(hours=8))  # 快讯时间戳均为北京时间

_telegraph_cache: Optional[list[dict]] = None

def _fetch_news_telegraph() -> list[dict]:
    """快讯原始条目 [{time,title,content}]：财联社优先，失效时降级东方财富全球快讯。
    结果按运行进程缓存（科技快讯与政策快讯共用同一次抓取）。"""
    global _telegraph_cache
    if _telegraph_cache is not None:
        return _telegraph_cache
    import akshare as ak
    # 1) 财联社（2026-07 起接口 404/挂起，保留以便上游修复后自动恢复）
    try:
        try:
            df = _call_with_timeout(lambda: ak.stock_info_global_cls(symbol="重点"), 25)
        except Exception:
            df = _call_with_timeout(ak.stock_info_global_cls, 25)
        if df is not None and not df.empty:
            title_col = next((c for c in df.columns if "标题" in c or "title" in c.lower()), None)
            content_col = next((c for c in df.columns if "内容" in c or "content" in c.lower()), None)
            time_col = next((c for c in df.columns if "时间" in c or "发布" in c), None)
            if title_col:
                _telegraph_cache = [
                    {
                        "time": str(r.get(time_col, "") or "") if time_col else "",
                        "title": str(r.get(title_col, "")).strip(),
                        "content": str(r.get(content_col, "") or "").strip() if content_col else "",
                    }
                    for _, r in df.iterrows()
                ]
                return _telegraph_cache
    except Exception as e:
        print(f"  [News WARN] 财联社快讯获取失败: {str(e)[:120]}")
    # 2) 东方财富全球财经快讯
    try:
        df = _call_with_timeout(ak.stock_info_global_em, 25)
        if df is not None and not df.empty:
            print("  [News] 财联社不可用，改用东方财富全球快讯")
            _telegraph_cache = [
                {
                    "time": str(r.get("发布时间", "") or ""),
                    "title": str(r.get("标题", "")).strip(),
                    "content": str(r.get("摘要", "") or "").strip(),
                }
                for _, r in df.iterrows()
            ]
            return _telegraph_cache
    except Exception as e:
        print(f"  [News WARN] 东方财富快讯获取失败: {str(e)[:120]}")
    _telegraph_cache = []
    return _telegraph_cache

def fetch_cls_news(n: int = 20, keywords: Optional[list[str]] = None, hours: int = 24) -> list[dict]:
    """近 hours 小时内重要快讯，按关键词过滤（默认科技/AI/资本相关）。"""
    rows = _fetch_news_telegraph()
    if not rows:
        return []

    keywords = keywords or CLS_TECH_KEYWORDS
    cutoff = datetime.now(BEIJING_TZ).replace(tzinfo=None) - timedelta(hours=hours)
    out = []
    for r in rows:
        title, content = r["title"], r["content"]
        if not title:
            continue
        # 时间窗口过滤（解析失败则保留，交给下游判断）
        try:
            if datetime.strptime(r["time"][:19], "%Y-%m-%d %H:%M:%S") < cutoff:
                continue
        except Exception:
            pass
        if not any(k in title or k in content for k in keywords):
            continue
        out.append({"time": r["time"][:16], "title": title, "content": content[:200]})
        if len(out) >= n:
            break
    return out

def collect_a_stock_tech_market() -> dict:
    """A股科技板块行情一站式采集。"""
    print("[Scraper] 采集 A股科技板块行情...")
    sectors = fetch_tech_concept_sectors(top_n=10)
    print(f"  → 科技板块 {len(sectors)} 个")

    leaders = {}
    for s in sectors[:5]:
        stocks = fetch_concept_top_stocks(s["name"], n=3)
        if stocks:
            leaders[s["name"]] = stocks

    north = fetch_north_bound_flow()
    if north:
        print(f"  → 北向资金净流入 {north['net_inflow_yi']:.2f} 亿")

    return {
        "sectors": sectors,
        "leaders": leaders,
        "north_flow": north,
    }

def collect_cls_tech_news() -> list[dict]:
    print("[Scraper] 采集科技/资本快讯...")
    items = fetch_cls_news(n=20)
    print(f"  → 科技/资本快讯 {len(items)} 条")
    return items

def collect_cls_policy_news() -> list[dict]:
    """快讯中的政策/宏观相关条目（政策快报数据源之一）。
    窗口 32h ≈ 昨天 00:00 → 今早 08:00 发报时刻。"""
    print("[Scraper] 采集政策相关快讯...")
    items = fetch_cls_news(n=15, keywords=CLS_POLICY_KEYWORDS, hours=32)
    print(f"  → 政策相关快讯 {len(items)} 条")
    return items

# ════════════════════════════════════════════════════════════════════════════
# 宏观经济数据日历（华尔街见闻，时间为北京时间）
# ════════════════════════════════════════════════════════════════════════════

# 重要性 >=3 的条目全部保留；重要性 ==2 的仅保留下列关键地区（美联储/中国优先）
MACRO_KEY_REGIONS = {"美国", "中国", "欧元区"}

def collect_macro_calendar(date_list: list[str], start: str, end: str) -> list[dict]:
    """
    拉取华尔街见闻宏观日历（akshare macro_info_ws）。
    date_list: YYYYMMDD 查询日期列表；start/end: YYYY-MM-DD，
    用于过滤接口溢出返回的窗口外条目。
    """
    print("[Scraper] 采集宏观经济日历...")
    try:
        import akshare as ak
    except Exception as e:
        print(f"  [Macro WARN] akshare 不可用: {e}")
        return []

    out, seen = [], set()
    for d in date_list:
        try:
            df = _call_with_timeout(lambda: ak.macro_info_ws(date=d), 30)
        except Exception as e:
            print(f"  [Macro WARN] {d} 日历获取失败: {str(e)[:120]}")
            continue
        if df is None or df.empty:
            continue
        for _, row in df.iterrows():
            try:
                imp = int(float(row.get("重要性", 0) or 0))
            except Exception:
                imp = 0
            region = str(row.get("地区", "") or "").strip()
            if imp < 2:
                continue
            if imp == 2 and region not in MACRO_KEY_REGIONS:
                continue
            t = str(row.get("时间", "") or "")[:16]  # YYYY-MM-DD HH:MM
            if not (start <= t[:10] <= end):
                continue
            event = str(row.get("事件", "") or "").strip()
            if not event:
                continue
            key = (t, event)
            if key in seen:
                continue
            seen.add(key)

            def _val(v) -> str:
                s = str(v).strip()
                return "" if s.lower() in ("nan", "none", "nat", "") else s

            out.append({
                "time": t,
                "region": region,
                "event": event,
                "importance": imp,
                "expected": _val(row.get("预期")),
                "previous": _val(row.get("前值")),
                "actual": _val(row.get("今值")),
            })
        time.sleep(0.3)

    # 条目过多时优先保留高重要性，再统一按时间排序
    if len(out) > 120:
        out.sort(key=lambda x: -x["importance"])
        out = out[:120]
    out.sort(key=lambda x: x["time"])
    print(f"  → 宏观日历条目 {len(out)} 条（{start} ~ {end}）")
    return out
