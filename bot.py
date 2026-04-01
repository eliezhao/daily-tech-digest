#!/usr/bin/env python3
"""
广深科技日报机器人 v2.0

流程：
  1. scrapers.py  — 爬虫采集 Meetup / 活动行 URL / TechCrunch RSS / 36kr
  2. Kimi 多轮联网搜索 — 补充中文活动 & 融资信息（华南+亚洲+全球）
  3. 合并全部原始数据 → Kimi 最终整理去重格式化
  4. 发送两条 Telegram 消息（活动 + 融资）

密钥从环境变量读取（本地用 .env 文件，GitHub Actions 用 Secrets）
"""

import os
import time
import sys
import traceback
from datetime import datetime
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

from scrapers import collect_all_events, collect_all_funding

# ── 配置（全部从环境变量读取）──────────────────────────────────────────────
def _require(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"环境变量 {key} 未设置，请检查 .env 或 GitHub Secrets")
    return val

KIMI_API_KEY  = _require("KIMI_API_KEY")
KIMI_BASE_URL = "https://api.moonshot.cn/v1"
KIMI_MODEL    = "moonshot-v1-128k"

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
def kimi_ask(prompt: str, max_rounds: int = 10) -> str:
    tools = [{"type": "builtin_function", "function": {"name": "$web_search"}}]
    messages = [{"role": "user", "content": prompt}]
    for _ in range(max_rounds):
        resp = client.chat.completions.create(
            model=KIMI_MODEL,
            messages=messages,
            tools=tools,
            temperature=0.3,
            max_tokens=4096,
        )
        choice = resp.choices[0]
        msg = choice.message
        if choice.finish_reason == "tool_calls":
            messages.append(msg)
            for tc in msg.tool_calls:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tc.function.name,
                    "content": tc.function.arguments,
                })
        else:
            return msg.content or ""
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
    return kimi_ask(final_prompt)

# ══════════════════════════════════════════════════════════════════════════════
# 任务二：投融资日报
# ══════════════════════════════════════════════════════════════════════════════

FUNDING_KIMI_PROMPTS = [
    # 深圳/广东中文
    "今天是{date}。请联网搜索过去48小时内深圳、广州、广东省的科技企业融资新闻。"
    "搜索 36kr、IT桔子、创业邦、虎嗅等来源，关键词：深圳融资、广州AI融资、广东科技投资 等。"
    "返回原始信息：公司名、融资金额/轮次、业务、投资方、官网/联系方式（如有）、来源链接。",

    # 亚洲英文（排除中国大陆，避免重复）
    "Today is {date}. Search for tech startup funding news in the past 48 hours "
    "from East/Southeast Asia: Hong Kong, Singapore, Japan, South Korea, Taiwan, Vietnam, Indonesia. "
    "Focus on AI, SaaS, cloud, deep tech companies. "
    "Return: company name, amount/round, business (note ☁️ if cloud/AI), investors, "
    "official website, contact info (if available), source URL.",

    # 全球AI/云（仅大额或知名）
    "Today is {date}. Search for notable AI and cloud tech startup funding rounds announced in the past 48 hours globally. "
    "Focus on companies with significant cloud or AI components (not just pure biotech/fintech). "
    "Return: company, amount/round, geography, business description, investors, website, source.",
]

def kimi_search_funding(date: str) -> str:
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(kimi_ask, p.format(date=date)): p[:30] for p in FUNDING_KIMI_PROMPTS}
        results = {}
        for fut in as_completed(futures):
            label = futures[fut]
            try:
                results[label] = fut.result()
            except Exception as e:
                results[label] = f"（搜索出错: {e}）"
    return "\n\n---\n\n".join(results.values())

def build_funding_report(date: str) -> str:
    print("  [Funding] 启动爬虫...")
    raw_items = collect_all_funding()

    # 序列化爬虫数据
    rss_text = "\n".join(
        f"- [{it.source}] {it.title} | {it.published[:16]} | {it.url}"
        for it in raw_items
    )

    print("  [Funding] Kimi 补充搜索（并发3个 prompt）...")
    kimi_raw = kimi_search_funding(date)

    final_prompt = f"""今天是 {date}。

以下是从多个来源收集到的科技融资原始数据，请帮我整理、去重、格式化，输出一份专业的投融资日报。

【数据来源一：TechCrunch RSS + 36kr 抓取（英文/中文）】
{rss_text or "（无数据）"}

【数据来源二：Kimi 联网搜索（广深/亚洲/全球AI云）】
{kimi_raw}

---

整理要求：
1. 去重（同一公司/融资只保留最完整的一条）
2. 只保留科技类：AI、软件、SaaS、云服务、硬件科技（剔除纯生物医药、房产等）
3. 按优先级排序：深圳 → 广东 → 华南 → 亚洲其他 → 全球
4. 每条格式：
   💰 公司名（中英文都写）
   💵 融资：金额 / 轮次
   📍 地区：
   🏭 业务：（涉及云/AI加 ☁️ 或 🤖 标注）
   🌐 官网：（如能找到）
   📬 联系：（邮箱/微信公众号/LinkedIn，如能找到）
   🤝 投资方：
   📰 来源：
5. 无法确认的字段留空，不要编造
6. 最多展示15条"""

    print("  [Funding] Kimi 最终汇总整理...")
    return kimi_ask(final_prompt)

# ══════════════════════════════════════════════════════════════════════════════
# 主程序
# ══════════════════════════════════════════════════════════════════════════════
def main():
    date_str = datetime.now().strftime("%Y-%m-%d")
    ts = lambda: datetime.now().strftime("%H:%M:%S")

    print(f"\n{'='*60}")
    print(f"广深科技日报 v2.0  {date_str}")
    print(f"{'='*60}\n")

    tg_send(f"🤖 *广深科技日报 v2.0 — {date_str}*\n正在多源采集中，请稍候…")

    # ── 活动日报 ─────────────────────────────────────────────────────────────
    print(f"[{ts()}] === 任务一：线下活动 ===")
    events_report = build_events_report(date_str)
    tg_send(f"🏙️ *广深/亚洲 AI科技线下活动*\n_{date_str}_\n\n{events_report}")
    print(f"[{ts()}] 活动日报已发送")

    time.sleep(3)

    # ── 融资日报 ─────────────────────────────────────────────────────────────
    print(f"\n[{ts()}] === 任务二：投融资 ===")
    funding_report = build_funding_report(date_str)
    tg_send(f"💼 *科技投融资日报*\n_{date_str}_ | 华南优先 → 亚洲 → 全球 ☁️🤖\n\n{funding_report}")
    print(f"[{ts()}] 投融资日报已发送")

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
