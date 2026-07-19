#!/usr/bin/env python3
"""
广深科技日报机器人 v2.1

流程：
  1. scrapers.py  — 爬虫采集 Meetup / 活动行 URL / TechCrunch RSS / 36kr / akshare 行情与宏观日历
  2. Kimi 多轮联网搜索 — 补充中文活动 & 融资信息 & 中国政策（华南+亚洲+全球）
  3. 合并全部原始数据 → Kimi 最终整理去重格式化
  4. 依次发送 Telegram 消息：活动 / 投融资 / A股科技市场 / 宏观数据前瞻 / 中国政策快报*
     * 政策快报仅在过去24小时有重磅政策时才发送

密钥从环境变量读取（本地用 .env 文件，GitHub Actions 用 Secrets）
"""

import os
import time
import sys
import traceback
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from openai import OpenAI
import requests

# 本地开发：自动加载 .env 文件（生产环境不依赖此文件）
_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

from scrapers import (
    collect_all_events,
    collect_all_funding,
    collect_a_stock_tech_market,
    collect_cls_tech_news,
    collect_cls_policy_news,
    collect_macro_calendar,
    is_a_stock_open_today,
)

# ── 配置（全部从环境变量读取）──────────────────────────────────────────────
def _require(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"环境变量 {key} 未设置，请检查 .env 或 GitHub Secrets")
    return val

KIMI_API_KEY  = _require("KIMI_API_KEY")
KIMI_BASE_URL = "https://api.moonshot.cn/v1"
# kimi-k3：当前 Kimi 通用旗舰（分析/整合最强的非 code 模型），用于纯分析/整理任务
KIMI_MODEL          = os.environ.get("KIMI_MODEL", "kimi-k3")
# 联网搜索任务仍用 k2.6：k3 暂不支持 $web_search builtin（回传 tool_calls 会
# tokenization failed；改 type 则搜索结果不注入，模型只能凭记忆作答）。
# 待 Moonshot 支持后，把 KIMI_SEARCH_MODEL 设为 kimi-k3 即可切换。
KIMI_SEARCH_MODEL   = os.environ.get("KIMI_SEARCH_MODEL", "kimi-k2.6")
KIMI_FALLBACK_MODEL = os.environ.get("KIMI_FALLBACK_MODEL", "kimi-k2.6")

TG_TOKEN   = _require("TG_TOKEN")
TG_CHAT_ID = _require("TG_CHAT_ID")

client = OpenAI(api_key=KIMI_API_KEY, base_url=KIMI_BASE_URL)

# ── Telegram ─────────────────────────────────────────────────────────────────
def tg_send(text: str):
    url_api = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    MAX = 4000
    chunks = [text[i:i+MAX] for i in range(0, len(text), MAX)]
    for i, chunk in enumerate(chunks):
        sent = False
        for parse_mode in ["Markdown", None]:
            payload = {"chat_id": TG_CHAT_ID, "text": chunk}
            if parse_mode:
                payload["parse_mode"] = parse_mode
            try:
                r = requests.post(url_api, json=payload, timeout=15)
                if r.ok:
                    sent = True
                    break
                else:
                    print(f"  [TG WARN] chunk {i+1} parse_mode={parse_mode}: {r.status_code} {r.text[:200]}")
            except Exception as e:
                print(f"  [TG ERROR] chunk {i+1}: {e}")
        if not sent:
            print(f"  [TG FAIL] chunk {i+1} 所有方式均失败，内容前50字: {chunk[:50]}")
        time.sleep(0.5)

# ── Kimi 联网搜索（多轮 tool_calls）────────────────────────────────────────
class KimiContentFilterError(Exception):
    """Kimi 内容风控拦截，无法通过重试解决。"""

def _is_content_filter_error(exc: Exception) -> bool:
    s = str(exc)
    return ("content_filter" in s) or ("high risk" in s) or ("considered high risk" in s)

def _is_model_unavailable_error(exc: Exception) -> bool:
    s = str(exc).lower()
    return any(k in s for k in [
        "model not found", "not found the model", "invalid_model", "model_not_found",
        "resource_not_found", "unsupported model", "does not exist",
    ])

def _temp_for(model: str) -> float:
    # kimi-k 系列（k2/k3）强制 temperature=1（k3 曾短暂允许低温，2026-07-19 起服务端也改为仅允许 1）；
    # moonshot-v1 系列用低温做整理任务。下方 kimi_ask 另有 invalid temperature 自动重试兜底。
    return 1.0 if model.startswith("kimi-k") else 0.3

def _max_tokens_for(model: str) -> int:
    # kimi-k 系列（k2/k3）是推理模型，思维链占用输出额度，长任务需给足空间，否则正文为空
    return 16384 if model.startswith("kimi-k") else 4096

def _assistant_msg_to_dict(msg) -> dict:
    """把带 tool_calls 的 assistant 回复重建为可回传的 dict（剔除 reasoning_content 等多余字段）。
    注意 type 必须原样保留：$web_search 回传时 type='builtin_function'，
    改成 'function' 服务端就不再注入搜索结果，模型会凭记忆瞎答。"""
    return {
        "role": "assistant",
        "content": msg.content or "",
        "tool_calls": [
            {
                "id": tc.id,
                "type": tc.type,
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in (msg.tool_calls or [])
        ],
    }

def kimi_ask(prompt: str, max_rounds: int = 10, enable_search: bool = True) -> str:
    tools = [{"type": "builtin_function", "function": {"name": "$web_search"}}] if enable_search else None
    messages = [{"role": "user", "content": prompt}]
    # 搜索任务与分析任务用不同模型（k3 暂不支持 $web_search）
    model_in_use = KIMI_SEARCH_MODEL if enable_search else KIMI_MODEL
    temp_override = None  # Moonshot 会不定期调整温度约束，报错时自动改用 1 重试
    for _ in range(max_rounds):
        try:
            kwargs = dict(model=model_in_use, messages=messages,
                          temperature=temp_override if temp_override is not None
                                      else _temp_for(model_in_use),
                          max_tokens=_max_tokens_for(model_in_use))
            if tools:
                kwargs["tools"] = tools
            resp = client.chat.completions.create(**kwargs)
        except Exception as e:
            if _is_content_filter_error(e):
                print(f"  [Kimi BLOCKED] 内容被风控拦截：{str(e)[:200]}")
                raise KimiContentFilterError(str(e)) from e
            if "invalid temperature" in str(e).lower() and temp_override is None:
                print(f"  [Kimi] {model_in_use} 温度约束变更，改用 temperature=1 重试")
                temp_override = 1.0
                continue
            if _is_model_unavailable_error(e) and model_in_use != KIMI_FALLBACK_MODEL:
                print(f"  [Kimi] 模型 {model_in_use} 不可用，回退到 {KIMI_FALLBACK_MODEL}")
                model_in_use = KIMI_FALLBACK_MODEL
                continue
            raise
        choice = resp.choices[0]
        msg = choice.message
        if choice.finish_reason == "tool_calls":
            messages.append(_assistant_msg_to_dict(msg))
            for tc in msg.tool_calls:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tc.function.name,
                    "content": tc.function.arguments,
                })
        else:
            content = msg.content or ""
            if not content.strip() and choice.finish_reason == "length":
                print("  [Kimi WARN] 输出为空且 max_tokens 耗尽（思维链占满输出额度）")
            return content
    return "（Kimi 搜索轮次超限）"

# ══════════════════════════════════════════════════════════════════════════════
# 任务一：线下活动
# ══════════════════════════════════════════════════════════════════════════════

# Kimi 补充搜索：多个独立 prompt 并发
EVENT_KIMI_PROMPTS = [
    # 广深中文
    "今天是{date}。请联网搜索深圳、广州地区未来15天内的AI/云计算/软件/创业相关线下活动。"
    "搜索活动行(huodongxing.com)、互动吧(hudongba.com)、bagevent.com，"
    "关键词：深圳AI活动、广深技术沙龙、广州云计算meetup 等。"
    "只返回找到的活动原始信息（标题、时间、地点、主办、链接），不需要格式化，尽量多列。",

    # 云厂商活动
    "今天是{date}。请联网搜索腾讯云、阿里云、华为云在广东省（深圳/广州为主）举办的近期线下开发者活动、workshop、沙龙。"
    "同时搜索 AWS、Google Cloud、Microsoft Azure 在华南/香港的活动。"
    "返回原始信息（标题、时间、地点、链接）。",

    # 香港+东南亚英文活动
    "Today is {date}. Please search for AI, cloud, and startup offline events in the next 30 days "
    "in Hong Kong, Singapore, Bangkok, and other major Asian tech cities. "
    "Return raw event info: title, date, venue, city, URL. List as many as possible.",
]

def kimi_search_events(date: str) -> str:
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(kimi_ask, p.format(date=date)): p[:30] for p in EVENT_KIMI_PROMPTS}
        results = {}
        for fut in as_completed(futures):
            label = futures[fut]
            try:
                results[label] = fut.result()
            except Exception as e:
                results[label] = f"（搜索出错: {e}）"
    return "\n\n---\n\n".join(results.values())

def build_events_report(date: str) -> str:
    print("  [Events] 启动爬虫...")
    meetup_events, hdx_urls = collect_all_events()

    print("  [Events] Kimi 补充搜索（并发3个 prompt）...")
    kimi_raw = kimi_search_events(date)

    # 序列化爬虫数据
    meetup_text = "\n".join(
        f"- [{e.city}] {e.title} | {e.date_str} | {e.venue} | {e.url}"
        for e in meetup_events
    )
    hdx_text = "\n".join(f"- {u}" for u in hdx_urls[:30])  # 最多30条URL

    # 最终汇总提示
    final_prompt = f"""今天是 {date}。

以下是从多个来源收集到的线下科技活动原始数据，请帮我整理、去重、格式化，输出一份漂亮的活动日报。

【数据来源一：Meetup（结构化，亚洲多城市）】
{meetup_text or "（无数据）"}

【数据来源二：活动行（仅 URL，请结合已知或联网查询详情）】
{hdx_text or "（无数据）"}

【数据来源三：Kimi 联网搜索补充（广深/云厂商/香港+东南亚）】
{kimi_raw}

---

整理要求：
1. 去重（同一活动只保留一条）
2. 排序：广深优先 → 香港 → 亚洲其他城市
3. 剔除过去的活动，只保留未来15天内
4. 每条格式：
   📅 活动名称
   🕐 时间：（格式：YYYY-MM-DD HH:mm）
   📍 地点：场馆, 城市
   🏢 主办：
   📝 简介：（1-2句）
   🔗 报名链接：
5. 最多展示15条最相关的（AI/软件/云/创业优先）
6. 如某条信息不完整，保留已有内容，不要编造"""

    print("  [Events] Kimi 最终汇总整理...")
    try:
        return kimi_ask(final_prompt)
    except KimiContentFilterError as e:
        print(f"  [Events] 最终汇总被风控拦截，回退到原始数据：{str(e)[:120]}")
        return (
            "⚠️ AI 汇总被内容风控拦截，以下为原始抓取数据（截断展示）：\n\n"
            f"📅 Meetup 活动：\n{meetup_text[:2000] or '（无）'}\n\n"
            f"🔗 活动行 URL：\n{hdx_text[:1500] or '（无）'}\n\n"
            f"🔍 联网搜索原文：\n{kimi_raw[:2500] or '（无）'}"
        )

# ══════════════════════════════════════════════════════════════════════════════
# 任务二：投融资日报
# ══════════════════════════════════════════════════════════════════════════════

FUNDING_KIMI_PROMPTS = [
    # 华南中文（15天窗口）
    "今天是{date}，截止日期是{date}，请只返回{cutoff}到{date}之间（即过去15天内）发布的融资新闻，"
    "超出此时间范围的一律不收录。"
    "请联网搜索深圳、广州、广东省、华南地区的 AI公司、软件/SaaS公司、云计算公司 的融资新闻。"
    "搜索来源：36kr、IT桔子、创业邦、虎嗅、36氪、钛媒体。"
    "每条必须注明新闻发布日期。返回原始信息：公司名、融资金额/轮次、业务描述、投资方、官网、来源链接、发布日期。",

    # 亚洲英文（15天窗口）
    "Today is {date}. Return ONLY funding news published between {cutoff} and {date} (past 15 days). "
    "Strictly exclude any news older than {cutoff}. "
    "Search for AI, software, SaaS, and cloud tech startup funding from "
    "East/Southeast Asia: Hong Kong, Singapore, Japan, South Korea, Taiwan, Vietnam, Indonesia. "
    "Each item must include the publication date. "
    "Return: company name, amount/round, business (mark ☁️ if cloud/AI), investors, "
    "official website, contact info if available, source URL, publication date.",

    # 全球AI/云（15天窗口）
    "Today is {date}. Return ONLY AI and cloud tech startup funding news published between {cutoff} and {date} (past 15 days). "
    "Strictly exclude anything older than {cutoff}. "
    "Focus on companies where AI or cloud is the core business (not just a feature). "
    "Each item must include the publication date. "
    "Return: company, amount/round, geography, business description, investors, website, source URL, publication date.",
]

def kimi_search_funding(date: str, cutoff: str) -> str:
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            pool.submit(kimi_ask, p.format(date=date, cutoff=cutoff)): p[:30]
            for p in FUNDING_KIMI_PROMPTS
        }
        results = {}
        for fut in as_completed(futures):
            label = futures[fut]
            try:
                results[label] = fut.result()
            except Exception as e:
                results[label] = f"（搜索出错: {e}）"
    return "\n\n---\n\n".join(results.values())

def build_funding_report(date: str) -> str:
    from datetime import timedelta
    cutoff = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=15)).strftime("%Y-%m-%d")

    print(f"  [Funding] 时间窗口：{cutoff} → {date}（15天）")
    print("  [Funding] 启动爬虫...")
    raw_items = collect_all_funding()

    # RSS 条目也按15天过滤
    from datetime import timezone
    cutoff_dt = datetime.strptime(cutoff, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    filtered_items = []
    for it in raw_items:
        if not it.published:
            filtered_items.append(it)
            continue
        try:
            import email.utils
            pub_dt = datetime(*email.utils.parsedate(it.published)[:6], tzinfo=timezone.utc)
            if pub_dt >= cutoff_dt:
                filtered_items.append(it)
        except Exception:
            filtered_items.append(it)

    rss_text = "\n".join(
        f"- [{it.source}] {it.title} | {it.published[:16]} | {it.url}"
        for it in filtered_items
    )

    print("  [Funding] Kimi 补充搜索（并发3个 prompt）...")
    kimi_raw = kimi_search_funding(date, cutoff)

    final_prompt = f"""今天是 {date}，数据时间窗口为 {cutoff} 至 {date}（过去15天）。

以下是从多个来源收集到的科技融资原始数据，请帮我整理、去重、格式化，输出一份专业的投融资周报。

【数据来源一：TechCrunch RSS（英文）】
{rss_text or "（无数据）"}

【数据来源二：Kimi 联网搜索（华南/亚洲/全球AI云）】
{kimi_raw}

---

整理要求：
1. 严格只保留 {cutoff} 之后发布的融资新闻，超出时间范围的一律剔除
2. 只保留 AI公司、软件/SaaS公司、云计算公司（剔除纯生物医药、消费品、房产等）
3. 每条必须标注融资新闻的发布日期
4. 按优先级排序：深圳 → 广东 → 华南 → 亚洲其他 → 全球
5. 每条格式：
   💰 公司名（中英文）
   📅 新闻日期：
   💵 融资：金额 / 轮次
   📍 地区：
   🏭 业务：（AI/云加 ☁️ 或 🤖）
   🌐 官网：
   📬 联系：（邮箱/微信公众号/LinkedIn，如有）
   🤝 投资方：
   📰 来源：
6. 无法确认的字段留空，不要编造
7. 最多展示20条，优先展示华南地区"""

    print(f"  [Funding] RSS过滤后 {len(filtered_items)} 条，Kimi 最终汇总整理...")
    try:
        return kimi_ask(final_prompt)
    except KimiContentFilterError as e:
        print(f"  [Funding] 最终汇总被风控拦截，回退到原始数据：{str(e)[:120]}")
        return (
            "⚠️ AI 汇总被内容风控拦截，以下为原始抓取数据（截断展示）：\n\n"
            f"📰 TechCrunch RSS（{len(filtered_items)} 条，已按15天过滤）：\n{rss_text[:2500] or '（无）'}\n\n"
            f"🔍 联网搜索原文：\n{kimi_raw[:2500] or '（无）'}"
        )

# ══════════════════════════════════════════════════════════════════════════════
# 任务三：A股科技股市场日报 + 要闻快讯
# ══════════════════════════════════════════════════════════════════════════════

def _fmt_sectors(sectors: list) -> str:
    if not sectors:
        return "（无数据）"
    lines = []
    for s in sectors:
        arrow = "📈" if s["change_pct"] >= 0 else "📉"
        leader = f" 领涨:{s['leader']}" if s.get("leader") else ""
        breadth = f" 涨/跌:{s['stocks_up']}/{s['stocks_down']}" if s.get("stocks_up") else ""
        lines.append(f"{arrow} {s['name']}  {s['change_pct']:+.2f}%{leader}{breadth}")
    return "\n".join(lines)

def _fmt_leaders(leaders: dict) -> str:
    if not leaders:
        return "（无数据）"
    blocks = []
    for sector, stocks in leaders.items():
        items = " · ".join(
            f"{s['name']}({s['code']}) {s['change_pct']:+.2f}%" for s in stocks
        )
        blocks.append(f"【{sector}】{items}")
    return "\n".join(blocks)

def _fmt_news(items: list) -> str:
    if not items:
        return "（无数据）"
    return "\n".join(
        f"- [{it.get('time','')}] {it['title']}"
        + (f"\n  {it['content']}" if it.get("content") else "")
        for it in items
    )

def build_market_report(date: str) -> str:
    print("  [Market] 启动 akshare 数据采集...")
    market = collect_a_stock_tech_market()
    cls_news = collect_cls_tech_news()

    sectors_text = _fmt_sectors(market.get("sectors", []))
    leaders_text = _fmt_leaders(market.get("leaders", {}))
    north = market.get("north_flow")
    north_text = (
        f"今日北向资金净流入 {north['net_inflow_yi']:+.2f} 亿元"
        if north else "（数据缺失）"
    )
    news_text = _fmt_news(cls_news)

    final_prompt = f"""你是一位资深的中国 A 股科技板块策略分析师。今天是 {date}。

以下是当日盘后采集到的结构化数据，请你输出一份精炼专业的「A股科技股日报」。

【一、科技概念板块涨跌幅（已筛选 AI/算力/半导体/云/软件/机器人/智能驾驶 等）】
{sectors_text}

【二、热点板块领涨个股】
{leaders_text}

【三、北向资金】
{north_text}

【四、24h 科技/资本要闻快讯（财联社/东财）】
{news_text}

---

输出要求：
1. 直接给可读日报，不要寒暄、不要重复数据列。
2. 结构（用 emoji 小标题）：
   📊 板块异动：列出涨幅 TOP 3 板块 + 跌幅 TOP 2 板块，配 1 句驱动逻辑
   🚀 个股聚焦：3-5 只今日科技龙头股，简评（业务/催化/资金）
   💰 资金面：北向资金动向 + 任何有用的资金信号
   📰 要闻速读：从要闻快讯中提炼 5-8 条最关键的事件（AI/算力/芯片/政策/融资优先）
   🔮 明日观察：1-3 个值得跟踪的板块或事件（基于数据推断，不编造）
3. 数字保留 2 位小数。涨跌符号清晰（+/-）。
4. 不要编造未在数据中出现的公司/事件。
5. 全部用中文，总长度控制在 1500 字以内。"""

    print("  [Market] Kimi 最终汇总分析...")
    try:
        return kimi_ask(final_prompt, enable_search=False)
    except KimiContentFilterError as e:
        print(f"  [Market] 最终汇总被风控拦截，回退到原始数据：{str(e)[:120]}")
        return (
            "⚠️ AI 汇总被内容风控拦截，以下为原始抓取数据：\n\n"
            f"📊 板块异动：\n{sectors_text}\n\n"
            f"🚀 领涨个股：\n{leaders_text}\n\n"
            f"💰 资金面：{north_text}\n\n"
            f"📰 要闻快讯：\n{news_text[:2000]}"
        )

# ══════════════════════════════════════════════════════════════════════════════
# 任务四：宏观数据前瞻（今日 + 本周将发布的重要经济数据）
# ══════════════════════════════════════════════════════════════════════════════

# 日报定时在北京时间 08:00 发出，但 GitHub Actions runner 时区为 UTC，
# 所有"今天/本周/昨天"的日期计算必须显式使用北京时间。
BEIJING_TZ = timezone(timedelta(hours=8))

def _fmt_calendar(items: list) -> str:
    if not items:
        return "（无条目）"
    lines = []
    for it in items:
        stars = "⭐" * min(it["importance"], 5)
        tail = "".join([
            f" | 预期 {it['expected']}" if it["expected"] else "",
            f" | 前值 {it['previous']}" if it["previous"] else "",
            f" | 已公布 {it['actual']}" if it["actual"] else "",
        ])
        lines.append(f"- {it['time']} [{it['region']}] {it['event']} {stars}{tail}")
    return "\n".join(lines)

def build_macro_calendar_report() -> str:
    now_bj = datetime.now(BEIJING_TZ)
    today = now_bj.strftime("%Y-%m-%d")
    # 窗口：今天 → 本周日；周六/周日发报时数据已出尽，扩展到下周日预览下周
    days_to_sunday = 6 - now_bj.weekday()
    if days_to_sunday < 2:
        days_to_sunday += 7
    dates = [now_bj + timedelta(days=i) for i in range(days_to_sunday + 1)]
    start, end = dates[0].strftime("%Y-%m-%d"), dates[-1].strftime("%Y-%m-%d")

    print(f"  [Macro] 日历窗口：{start} → {end}（北京时间）")
    items = collect_macro_calendar([d.strftime("%Y%m%d") for d in dates], start, end)

    today_items = [it for it in items if it["time"][:10] == today]
    later_items = [it for it in items if it["time"][:10] > today]

    # akshare 日历失效/数据过少时，用 Kimi 联网搜索兜底
    kimi_raw = ""
    if len(items) < 5:
        print("  [Macro] 日历数据不足，Kimi 联网搜索兜底...")
        try:
            kimi_raw = kimi_ask(
                f"今天是{today}。请联网搜索{start}至{end}期间的全球重要经济数据发布日历，"
                "重点：美联储FOMC利率决议/会议纪要/美联储主席及官员讲话、美国非农就业报告、"
                "CPI、PCE、PPI、GDP、初请失业金、ISM PMI、零售销售，"
                "以及中国的官方PMI/CPI/PPI/社融信贷/LPR/进出口数据。"
                "每条注明发布日期、北京时间、预期值、前值。只返回原始信息列表。"
            )
        except Exception as e:
            print(f"  [Macro] 兜底搜索失败: {e}")
            kimi_raw = ""

    final_prompt = f"""你是宏观策略分析师。今天是 {today}（北京时间）。以下是 {start} 至 {end} 的宏观经济日历原始数据，时间均为北京时间，⭐数量代表重要性。

【今日（{today}）条目】
{_fmt_calendar(today_items)}

【后续条目（明天起至 {end}）】
{_fmt_calendar(later_items)}

【联网搜索兜底数据】
{kimi_raw or "（未启用）"}

---

请输出「宏观数据前瞻与影响分析」，要求：

一、筛选：只保留对股市有明显影响的条目，优先级从高到低：美联储（利率决议/会议纪要/主席及官员讲话）→ 美国非农与失业率 → 美国通胀（CPI/PCE/PPI）→ 美国 GDP/ISM/初请失业金/零售销售 → 中国宏观（PMI/CPI/PPI/社融/LPR/进出口/外储）→ 欧日等主要央行 → 重大财经事件（关税听证、重要峰会等）。剔除个股与公司层面新闻、产品涨价类条目、钻井数等低影响数据。

二、对每一条重要数据做情景分析，格式：

🕐 MM-DD周X HH:MM [地区] 数据名称 ⭐⭐⭐
   前值 xx | 预期 xx（已公布的补充实际值和偏离方向）
   ↑ 超预期：倾向利好/利空 + 一句传导逻辑（对美股/A股/美元的方向）
   ↓ 不及预期：倾向利好/利空 + 一句传导逻辑
（讲话/会议/事件类无前值预期，改为一行：鹰派→…；鸽派→…，或"关注点：…"）

三、输出结构：
🎯 今日关注（{today}）
逐条情景分析；若无重要数据写「今日无重磅数据」。
📅 本周后续
按日期分组，逐条情景分析；若无写「后续暂无重磅数据」。
💡 焦点
2-3 句：本期最关键的 1-2 个数据；点明当前市场的主要交易框架（例如处于"降息预期交易"还是"衰退担忧交易"，这决定了强数据是利好还是利空）。

四、分析准则：
1. 结合数据性质判断方向：通胀类（CPI/PCE/PPI）超预期通常压制降息预期→利空股市；就业类（非农/初请）超预期→经济强但降息预期降温，方向取决于当前市场敏感点，要说清楚；PMI 类以 50 为荣枯线。
2. 中国数据需同时给出对 A 股相关板块的影响方向。
3. 判断用"倾向利好/利空"的谨慎表述，双向可能时明确说明，不构成投资建议。
4. 严格基于上面提供的数据条目，不要编造。总长度控制在 2000 字以内。
5. 输出为发往 Telegram 的纯文本：禁止使用 HTML 标签和 &nbsp; 等 HTML 实体，缩进用普通空格。"""

    print("  [Macro] Kimi 最终汇总整理...")
    try:
        result = kimi_ask(final_prompt, enable_search=False)
        if result.strip():
            return result
        print("  [Macro] 汇总返回为空，回退原始数据")
    except KimiContentFilterError as e:
        print(f"  [Macro] 汇总被风控拦截，回退原始数据：{str(e)[:120]}")
    return (
        "⚠️ AI 汇总不可用，以下为原始日历数据：\n\n"
        f"🎯 今日：\n{_fmt_calendar(today_items)[:1500]}\n\n"
        f"📅 后续：\n{_fmt_calendar(later_items)[:2000]}"
    )

# ══════════════════════════════════════════════════════════════════════════════
# 任务五：中国政策快报（昨天 00:00 → 发报时刻；无重磅政策则不发送）
# ══════════════════════════════════════════════════════════════════════════════

def build_china_policy_report() -> str | None:
    """返回政策快报文本；窗口内无重磅政策时返回 None（调用方跳过发送）。"""
    now_bj = datetime.now(BEIJING_TZ)
    yesterday = (now_bj - timedelta(days=1)).strftime("%Y-%m-%d")
    now_str = now_bj.strftime("%Y-%m-%d %H:%M")

    cls_items = collect_cls_policy_news()
    cls_text = _fmt_news(cls_items)

    print("  [Policy] Kimi 联网搜索政策新闻...")
    try:
        kimi_raw = kimi_ask(
            f"现在是北京时间 {now_str}。请联网搜索 {yesterday} 00:00 至今中国官方新发布的重磅政策，"
            "来源限定：国务院/国常会、中共中央/中央政治局会议、中国人民银行、发改委、财政部、证监会、"
            "工信部、商务部、金融监管总局、网信办等国家部委的正式政策文件、会议决定、监管新规、"
            "货币政策操作（降准/降息/LPR/大额流动性投放）。"
            "重点关注影响股市、科技产业、宏观经济的政策。每条注明发布机构、发布时间、来源链接。"
            f"只要 {yesterday} 00:00 之后正式发布的，旧政策及其解读文章不要。"
            "如果确实没有新发布的重磅政策，直接回答：无重磅政策。"
        )
    except Exception as e:
        print(f"  [Policy] 联网搜索失败: {str(e)[:120]}")
        kimi_raw = "（联网搜索失败，无结果）"

    final_prompt = f"""现在是北京时间 {now_str}。以下是过去约24-32小时（{yesterday} 00:00 至今）采集到的中国政策相关信息。

【要闻快讯（政策相关，过去约32h，财联社/东财）】
{cls_text}

【联网搜索结果】
{kimi_raw}

---

判断标准——只有"重磅政策"才值得播报：国家级政策文件发布、国常会/政治局会议重要决定、央行货币政策操作（降准/降息/LPR调整/大额流动性投放）、重要行业监管新规、重大产业扶持政策，且发布时间在 {yesterday} 00:00 之后。

如果没有满足标准的重磅政策，只输出这一串字符，不要输出其他任何内容：NO_POLICY

如果有，则输出「中国政策快报」，要求：
1. 每条格式：
   🏛 政策/事件名称
   🕐 发布时间 + 发布机构
   📌 核心内容（2-3句）
   📈 影响分析：利好哪些板块/资产 + 一句传导逻辑；如有受损方也点明；并给力度判断（重磅/中性/边际）。表述客观谨慎，不构成投资建议
2. 按重要性排序，最多5条。
3. 普通新闻、旧政策解读、专家观点、媒体评论均不算新政策，不要收录；无法确认发布时间的不要收录。
4. 总长度控制在 1000 字以内。
5. 输出为发往 Telegram 的纯文本：禁止使用 HTML 标签和 &nbsp; 等 HTML 实体，缩进用普通空格。"""

    print("  [Policy] Kimi 最终汇总判定...")
    try:
        result = kimi_ask(final_prompt)
    except KimiContentFilterError as e:
        print(f"  [Policy] 汇总被风控拦截：{str(e)[:120]}")
        # 无法完成重磅性判定；若快讯源有政策类条目则播报原文，否则跳过
        if cls_items:
            return (
                "⚠️ AI 汇总被内容风控拦截，以下为政策相关快讯原文（未经重磅性筛选）：\n\n"
                + cls_text[:2500]
            )
        return None

    if not result or "NO_POLICY" in result[:100]:
        return None
    return result

# ══════════════════════════════════════════════════════════════════════════════
# 主程序
# ══════════════════════════════════════════════════════════════════════════════
def main():
    date_str = datetime.now().strftime("%Y-%m-%d")
    ts = lambda: datetime.now().strftime("%H:%M:%S")

    print(f"\n{'='*60}")
    print(f"广深科技日报 v2.1  {date_str}")
    print(f"{'='*60}\n")

    tg_send(f"🤖 *广深科技日报 v2.1 — {date_str}*\n正在多源采集中，请稍候…")

    # ── 活动日报 ─────────────────────────────────────────────────────────────
    print(f"[{ts()}] === 任务一：线下活动 ===")
    events_report = build_events_report(date_str)
    tg_send(f"🏙️ *广深/亚洲 AI科技线下活动*\n_{date_str}_\n\n{events_report}")
    print(f"[{ts()}] 活动日报已发送")

    time.sleep(3)

    # ── 融资日报 ─────────────────────────────────────────────────────────────
    print(f"\n[{ts()}] === 任务二：投融资 ===")
    funding_report = build_funding_report(date_str)
    from datetime import timedelta
    cutoff_str = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=15)).strftime("%Y-%m-%d")
    tg_send(f"💼 *科技投融资周报*\n📅 {cutoff_str} → {date_str}（过去15天）\nAI / 软件 / 云计算 | 华南优先 ☁️🤖\n\n{funding_report}")
    print(f"[{ts()}] 投融资日报已发送")

    time.sleep(3)

    # ── A股科技股市场日报 ────────────────────────────────────────────────────
    print(f"\n[{ts()}] === 任务三：A股科技股市场日报 ===")
    try:
        if not is_a_stock_open_today():
            skip_msg = f"📊 *A股科技股市场日报*\n_{date_str}_\n\n今日非 A 股交易日，跳过市场日报。"
            tg_send(skip_msg)
            print(f"[{ts()}] 非交易日，跳过市场日报")
        else:
            market_report = build_market_report(date_str)
            tg_send(f"📊 *A股科技股市场日报*\n_{date_str}_\nAI / 半导体 / 算力 / 云 / 软件 🤖☁️📈\n\n{market_report}")
            print(f"[{ts()}] 市场日报已发送")
    except Exception as e:
        print(f"[{ts()}] 市场日报任务失败: {e}")
        traceback.print_exc()
        tg_send(f"⚠️ 市场日报任务失败：{str(e)[:300]}")

    time.sleep(3)

    # ── 宏观数据前瞻 ─────────────────────────────────────────────────────────
    print(f"\n[{ts()}] === 任务四：宏观数据前瞻 ===")
    try:
        macro_report = build_macro_calendar_report()
        tg_send(f"🌍 *宏观数据前瞻*\n_{date_str}_\n美联储 / 非农 / 通胀 / 中国宏观 📊\n\n{macro_report}")
        print(f"[{ts()}] 宏观数据前瞻已发送")
    except Exception as e:
        print(f"[{ts()}] 宏观前瞻任务失败: {e}")
        traceback.print_exc()
        tg_send(f"⚠️ 宏观数据前瞻任务失败：{str(e)[:300]}")

    time.sleep(3)

    # ── 中国政策快报（有重磅政策才发送）──────────────────────────────────────
    print(f"\n[{ts()}] === 任务五：中国政策快报 ===")
    try:
        policy_report = build_china_policy_report()
        if policy_report:
            tg_send(f"🏛 *中国政策快报*\n_{date_str}_\n过去24小时新出重磅政策 🇨🇳\n\n{policy_report}")
            print(f"[{ts()}] 政策快报已发送")
        else:
            print(f"[{ts()}] 过去24小时无重磅政策，跳过发送")
    except Exception as e:
        print(f"[{ts()}] 政策快报任务失败: {e}")
        traceback.print_exc()
        tg_send(f"⚠️ 中国政策快报任务失败：{str(e)[:300]}")

    print(f"\n[{ts()}] 全部完成！")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        msg = f"❌ 广深日报运行出错：{e}"
        print(msg)
        traceback.print_exc()
        try:
            tg_send(msg)
        except Exception:
            pass
        sys.exit(1)
