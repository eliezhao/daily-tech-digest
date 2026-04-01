#!/usr/bin/env python3.11
"""
scrapers.py — 多源数据采集模块

Sources:
  Events  : Meetup (多城市 Apollo cache)  + 活动行 (事件 URL 列表)
  Funding : TechCrunch RSS (多 tag)       + 36kr 简单抓取
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
