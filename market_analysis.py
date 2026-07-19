#!/usr/bin/env python3
"""
market_analysis.py — A股板块分析（供 tg_bot.py 指令调用）

功能一 get_strong_sectors()：
    全部板块（东财 概念+行业）中，上周收涨 且 日线 MA10>MA20 的板块，按上周涨幅排序。
功能二 get_zt_concentration()：
    最近一个交易日涨停股票的板块集中度 TOP N（涨停家数 + 占板块成分股比例）。

数据源：东方财富（akshare），板块代码为东财代码（如 BK1043），
可拼行情页链接 https://quote.eastmoney.com/bk/90.BK1043.html

命令行自测（部署后验证用）：
    python3 market_analysis.py qs   # 功能一
    python3 market_analysis.py zt   # 功能二
"""

import json
import os
import sys
import time
import random
import threading
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from scrapers import _call_with_timeout

BEIJING_TZ = timezone(timedelta(hours=8))

# 板块成分股本地缓存（构建一次约3-5分钟，之后按周刷新）
_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
MEMBERS_CACHE_FILE = os.path.join(_DATA_DIR, "board_members.json")
MEMBERS_CACHE_TTL_DAYS = 7

_print_lock = threading.Lock()

def _log(msg: str):
    with _print_lock:
        print(msg, flush=True)

# ── 基础数据获取 ─────────────────────────────────────────────────────────────

def list_all_boards() -> list[dict]:
    """全部板块：[{name, code, type}]，type ∈ {概念, 行业}"""
    import akshare as ak
    boards = []
    for type_, fn in [("概念", ak.stock_board_concept_name_em),
                      ("行业", ak.stock_board_industry_name_em)]:
        df = _call_with_timeout(fn, 30)
        for _, r in df.iterrows():
            name = str(r.get("板块名称", "")).strip()
            code = str(r.get("板块代码", "")).strip()
            if name and code:
                boards.append({"name": name, "code": code, "type": type_})
    return boards

def _board_hist(name: str, type_: str, start: str, end: str):
    """板块日K。注意：概念接口 period='daily'，行业接口 period='日k'（akshare 两接口不一致）"""
    import akshare as ak
    if type_ == "概念":
        return ak.stock_board_concept_hist_em(
            symbol=name, period="daily", start_date=start, end_date=end, adjust="")
    return ak.stock_board_industry_hist_em(
        symbol=name, start_date=start, end_date=end, period="日k", adjust="")

def _fetch_hist_with_retry(name: str, type_: str, start: str, end: str):
    for attempt in (1, 2):
        try:
            time.sleep(random.uniform(0.05, 0.25))  # 轻微抖动，礼貌限速
            return _call_with_timeout(lambda: _board_hist(name, type_, start, end), 25)
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1.0)

# ── 功能一：上周收涨 + MA10>MA20 ────────────────────────────────────────────

def _last_week_window(today) -> tuple:
    """最近一个已完整结束的交易周（周一~周日窗口）。
    周六/周日调用：`上周`=本自然周（周五已收官）；工作日调用：上一自然周。"""
    this_monday = today - timedelta(days=today.weekday())
    if today.weekday() >= 5:  # 周六/周日
        return this_monday, this_monday + timedelta(days=6)
    return this_monday - timedelta(days=7), this_monday - timedelta(days=1)

def _analyze_board_hist(df, week_start, week_end) -> Optional[dict]:
    """从板块日K计算：上周涨幅、最新 MA10/MA20。数据不足返回 None。"""
    import pandas as pd
    if df is None or df.empty or "日期" not in df.columns:
        return None
    df = df.copy()
    df["date"] = pd.to_datetime(df["日期"]).dt.date
    df = df.sort_values("date")
    closes = df["收盘"].astype(float)

    # 上周涨幅 = 上周最后一个交易日收盘 / 上周之前最后一个交易日收盘 - 1
    lw = df[(df["date"] >= week_start) & (df["date"] <= week_end)]
    prev = df[df["date"] < week_start]
    if lw.empty or prev.empty:
        return None
    weekly_pct = (float(lw["收盘"].astype(float).iloc[-1])
                  / float(prev["收盘"].astype(float).iloc[-1]) - 1) * 100

    # 最新日线 MA10 / MA20
    if len(closes) < 20:
        return None
    ma10 = float(closes.iloc[-10:].mean())
    ma20 = float(closes.iloc[-20:].mean())

    return {
        "weekly_pct": weekly_pct,
        "ma10": ma10,
        "ma20": ma20,
        "last_date": str(df["date"].iloc[-1]),
        "week_last_date": str(lw["date"].iloc[-1]),
    }

def get_strong_sectors(max_workers: int = 6) -> dict:
    """上周收涨 且 日线 MA10>MA20 的全部板块（概念+行业）"""
    today = datetime.now(BEIJING_TZ).date()
    week_start, week_end = _last_week_window(today)
    fetch_start = (week_start - timedelta(days=70)).strftime("%Y%m%d")
    fetch_end = today.strftime("%Y%m%d")

    boards = list_all_boards()
    _log(f"[qs] 板块总数 {len(boards)}，窗口 上周={week_start}~{week_end}，开始拉取日K...")

    items, failed = [], 0
    done = 0

    def work(b):
        df = _fetch_hist_with_retry(b["name"], b["type"], fetch_start, fetch_end)
        return _analyze_board_hist(df, week_start, week_end)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(work, b): b for b in boards}
        for fut in as_completed(futs):
            b = futs[fut]
            done += 1
            if done % 100 == 0:
                _log(f"[qs] 进度 {done}/{len(boards)}")
            try:
                r = fut.result()
            except Exception:
                failed += 1
                continue
            if not r:
                continue
            if r["weekly_pct"] > 0 and r["ma10"] > r["ma20"]:
                items.append({**b, **r})

    items.sort(key=lambda x: -x["weekly_pct"])
    return {
        "week_start": str(week_start),
        "week_end": str(week_end),
        "total_boards": len(boards),
        "failed": failed,
        "items": items,
    }

def format_strong_sectors(res: dict, limit: int = 60) -> str:
    items = res["items"]
    head = (
        f"📈 强势板块筛选（东财 概念+行业 共{res['total_boards']}个）\n"
        f"条件：上周({res['week_start']}~{res['week_end']})收涨 且 日线MA10>MA20\n"
        f"符合条件：{len(items)} 个"
        + (f"（{res['failed']}个板块数据获取失败）" if res["failed"] else "")
        + "\n" + "─" * 24
    )
    if not items:
        return head + "\n无符合条件的板块"
    lines = []
    for i, it in enumerate(items[:limit], 1):
        lines.append(
            f"{i}. {it['name']}({it['code']})[{it['type']}] "
            f"上周{it['weekly_pct']:+.2f}% | MA10 {it['ma10']:.1f} > MA20 {it['ma20']:.1f}"
        )
    tail = f"\n（共{len(items)}个，仅显示前{limit}）" if len(items) > limit else ""
    return head + "\n" + "\n".join(lines) + tail

# ── 功能二：涨停板块集中度 ───────────────────────────────────────────────────

def _load_members_cache() -> Optional[dict]:
    try:
        with open(MEMBERS_CACHE_FILE, encoding="utf-8") as f:
            cache = json.load(f)
        updated = datetime.fromisoformat(cache["updated"])
        if datetime.now() - updated > timedelta(days=MEMBERS_CACHE_TTL_DAYS):
            return None
        return cache
    except Exception:
        return None

def build_members_cache(max_workers: int = 8) -> dict:
    """构建 板块→成分股代码 缓存（概念+行业，约500+个板块，首次约3-5分钟）"""
    import akshare as ak
    boards = list_all_boards()
    _log(f"[zt] 构建板块成分缓存：{len(boards)} 个板块...")
    result = {"updated": datetime.now().isoformat(), "boards": {}}

    def work(b):
        fn = (ak.stock_board_concept_cons_em if b["type"] == "概念"
              else ak.stock_board_industry_cons_em)
        time.sleep(random.uniform(0.05, 0.25))
        df = _call_with_timeout(lambda: fn(symbol=b["name"]), 25)
        codes = [str(c).zfill(6) for c in df["代码"].astype(str).tolist()]
        return codes

    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(work, b): b for b in boards}
        for fut in as_completed(futs):
            b = futs[fut]
            done += 1
            if done % 100 == 0:
                _log(f"[zt] 成分缓存进度 {done}/{len(boards)}")
            try:
                codes = fut.result()
            except Exception:
                continue
            if codes:
                key = f"{b['type']}|{b['name']}"
                result["boards"][key] = {"code": b["code"], "members": codes}

    os.makedirs(_DATA_DIR, exist_ok=True)
    with open(MEMBERS_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)
    _log(f"[zt] 成分缓存完成：{len(result['boards'])} 个板块")
    return result

def _latest_zt_pool(max_lookback: int = 12) -> tuple:  # 12天覆盖春节长假
    """从昨天起往回找最近一个有涨停数据的交易日，返回 (date_str, DataFrame)"""
    import akshare as ak
    d = datetime.now(BEIJING_TZ).date() - timedelta(days=1)
    for _ in range(max_lookback):
        ds = d.strftime("%Y%m%d")
        try:
            df = _call_with_timeout(lambda: ak.stock_zt_pool_em(date=ds), 30)
            if df is not None and not df.empty:
                return ds, df
        except Exception:
            pass
        d -= timedelta(days=1)
    raise RuntimeError(f"近{max_lookback}天未取到涨停数据（数据源异常？）")

def get_zt_concentration(top_n: int = 10, min_members: int = 12) -> dict:
    """最近交易日涨停股的板块集中度：按涨停家数排序，附占成分股比例"""
    date_str, zt = _latest_zt_pool()
    zt_codes = {str(c).zfill(6) for c in zt["代码"].astype(str).tolist()}
    code2name = {str(r["代码"]).zfill(6): str(r["名称"]) for _, r in zt.iterrows()}
    _log(f"[zt] {date_str} 涨停 {len(zt_codes)} 家")

    cache = _load_members_cache()
    if cache is None:
        cache = build_members_cache()

    rows = []
    for key, info in cache["boards"].items():
        type_, name = key.split("|", 1)
        members = info["members"]
        if len(members) < min_members:
            continue
        hits = zt_codes & set(members)
        if len(hits) < 2:
            continue
        rows.append({
            "name": name, "code": info["code"], "type": type_,
            "zt_count": len(hits),
            "member_count": len(members),
            "ratio_pct": len(hits) / len(members) * 100,
            "zt_names": [code2name.get(c, c) for c in sorted(hits)][:6],
        })

    rows.sort(key=lambda x: (-x["zt_count"], -x["ratio_pct"]))
    return {"trade_date": date_str, "zt_total": len(zt_codes), "items": rows[:top_n]}

def format_zt_concentration(res: dict) -> str:
    d = res["trade_date"]
    date_fmt = f"{d[:4]}-{d[4:6]}-{d[6:]}"
    head = (
        f"🔥 涨停板块集中度 TOP{len(res['items'])}（{date_fmt}）\n"
        f"当日涨停共 {res['zt_total']} 家 | 口径：东财概念+行业板块\n" + "─" * 24
    )
    if not res["items"]:
        return head + "\n无明显集中板块（单板块涨停数均<2）"
    lines = []
    for i, it in enumerate(res["items"], 1):
        names = "、".join(it["zt_names"])
        more = "…" if it["zt_count"] > len(it["zt_names"]) else ""
        lines.append(
            f"{i}. {it['name']}({it['code']})[{it['type']}]\n"
            f"   涨停 {it['zt_count']}/{it['member_count']} 家，占比 {it['ratio_pct']:.1f}%\n"
            f"   {names}{more}"
        )
    return head + "\n" + "\n".join(lines)

# ── 命令行自测 ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "zt"
    if cmd == "qs":
        r = get_strong_sectors()
        print(format_strong_sectors(r))
    elif cmd == "zt":
        r = get_zt_concentration()
        print(format_zt_concentration(r))
    else:
        print("用法: python3 market_analysis.py [qs|zt]")
