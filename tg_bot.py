#!/usr/bin/env python3
"""
tg_bot.py — Telegram 指令机器人（常驻进程，长轮询，无需公网入站端口）

指令（群里 /qs@机器人名 或私聊 /qs 均可）：
  /qs        强势板块：上周收涨 且 日线MA10>MA20 的全部板块（含东财板块代码，约2-4分钟）
  /zt        涨停集中度：最近交易日涨停股 TOP10 板块及涨停股占成分股比例
  /ask 问题  用 DeepSeek（默认 deepseek-v4-pro）回答任意问题
  /help      帮助

问答的其他触发方式：
  - 群里 @机器人 + 问题（消息含"强势/MA"→/qs，"涨停/活跃"→/zt，其余任意文字→DeepSeek 问答）
  - 直接"回复"机器人的任意消息提问（隐私模式开着也能收到）
  - 私聊直接发问题
注意：要让机器人收到群里的普通 @ 消息，需在 BotFather 用 /setprivacy 关闭隐私模式；
斜杠命令和"回复机器人消息"不受此限制。

安全：仅响应 TG_CHAT_ID 对应会话；可用 TG_ALLOWED_CHATS=id1,id2 追加白名单。

部署：systemd 常驻（见 deploy/tg-bot.service），进程与每日日报 bot.py 相互独立。
"""

import os
import sys
import time
import threading
import traceback
from datetime import datetime, timedelta, timezone

import requests

# 本地开发：自动加载 .env（与 bot.py 相同逻辑）
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

TG_TOKEN = os.environ.get("TG_TOKEN") or sys.exit("环境变量 TG_TOKEN 未设置")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID") or sys.exit("环境变量 TG_CHAT_ID 未设置")
API = f"https://api.telegram.org/bot{TG_TOKEN}"

# DeepSeek 问答（可选功能：不配 key 则 /ask 提示未启用）
DEEPSEEK_API_KEY  = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL    = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro")

BEIJING_TZ = timezone(timedelta(hours=8))

# 允许响应的会话：TG_CHAT_ID + TG_ALLOWED_CHATS（逗号分隔，支持 -100xxx 群ID 或 @username）
ALLOWED_CHATS = {str(TG_CHAT_ID).strip().lower()}
ALLOWED_CHATS |= {
    x.strip().lower()
    for x in os.environ.get("TG_ALLOWED_CHATS", "").split(",") if x.strip()
}

HELP_TEXT = (
    "🤖 A股板块分析机器人\n"
    "/qs — 强势板块：上周收涨 且 日线MA10>MA20（约2-4分钟）\n"
    "/zt — 涨停集中度：最近交易日涨停股TOP10板块及占比（首次运行需构建缓存约3-5分钟）\n"
    "/ask 问题 — DeepSeek 回答任意问题\n"
    "/help — 本帮助\n"
    "问答还可以：群里 @我+问题、直接回复我的消息提问、私聊发问题\n"
    "（@触发需在 BotFather 关闭隐私模式；/命令和回复消息不受限）"
)

def log(msg: str):
    print(f"[{datetime.now(BEIJING_TZ).strftime('%m-%d %H:%M:%S')}] {msg}", flush=True)

# ── Telegram API ─────────────────────────────────────────────────────────────

def tg_call(method: str, http_timeout: int = 65, **params):
    """params 为 Telegram API 的 payload；http_timeout 为本地 HTTP 超时。"""
    r = requests.post(f"{API}/{method}", json=params, timeout=http_timeout)
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(f"{method} 失败: {str(j)[:200]}")
    return j["result"]

def send_text(chat_id, text: str, reply_to: int = None):
    """纯文本分段发送（4000字/段）。分析结果含 +/()| 等符号，不用 Markdown 最稳。"""
    MAX = 4000
    chunks = [text[i:i + MAX] for i in range(0, len(text), MAX)]
    for i, chunk in enumerate(chunks):
        payload = {"chat_id": chat_id, "text": chunk}
        if reply_to and i == 0:
            payload["reply_to_message_id"] = reply_to
            payload["allow_sending_without_reply"] = True
        try:
            r = requests.post(f"{API}/sendMessage", json=payload, timeout=20)
            if not (r.ok and r.json().get("ok")):
                log(f"  [TG WARN] {r.status_code} {r.text[:150]}")
        except Exception as e:
            log(f"  [TG ERROR] {e}")
        time.sleep(0.4)

# ── 指令解析 ─────────────────────────────────────────────────────────────────

KEYWORD_MAP = [
    (("强势", "ma10", "ma20", "周涨"), "qs"),
    (("涨停", "活跃", "集中"), "zt"),
    (("帮助", "help"), "help"),
]

def _keyword_cmd(low: str) -> str | None:
    for kws, cmd in KEYWORD_MAP:
        if any(k in low for k in kws):
            return cmd
    return None

def _is_reply_to_bot(msg: dict, bot_username: str) -> bool:
    frm = (msg.get("reply_to_message") or {}).get("from") or {}
    return bool(frm.get("is_bot")) and str(frm.get("username", "")).lower() == bot_username

def resolve_command(msg: dict, bot_username: str) -> tuple[str, str] | None:
    """解析消息 → (指令, 参数)。
    指令：qs / zt / help / ask（ask 的参数为问题文本）。
    触发方式：/命令（可带@机器人名）、@机器人+文字、回复机器人消息、私聊直接发问。"""
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if not text:
        return None
    low = text.lower()

    # 1) 斜杠命令（取第一个词，其余是参数）
    if low.startswith("/"):
        first = text.split()[0]
        rest = text[len(first):].strip()
        cmd, _, at = first[1:].partition("@")
        cmd = cmd.lower()
        if at and at.lower() != bot_username:   # 明确@了别的机器人
            return None
        if cmd in ("help", "start"):
            return ("help", "")
        if cmd in ("qs", "zt"):
            return (cmd, "")
        if cmd == "ask":
            return ("ask", rest) if rest else ("help", "")
        return None

    # 2) @机器人 + 文字：关键词→板块指令，其余→DeepSeek 问答
    mention = f"@{bot_username}"
    if mention in low:
        kw = _keyword_cmd(low)
        if kw:
            return (kw, "")
        # 去掉@提及后剩下的就是问题
        idx = low.find(mention)
        question = (text[:idx] + text[idx + len(mention):]).strip(" ，,、:：")
        return ("ask", question) if question else ("help", "")

    # 3) 回复机器人的消息 → 问答（隐私模式开启时也能收到）
    if _is_reply_to_bot(msg, bot_username):
        kw = _keyword_cmd(low)
        return (kw, "") if kw else ("ask", text)

    # 4) 私聊：关键词→指令，其余→问答
    if msg.get("chat", {}).get("type") == "private":
        kw = _keyword_cmd(low)
        return (kw, "") if kw else ("ask", text)
    return None

def chat_allowed(chat: dict) -> bool:
    cid = str(chat.get("id", "")).lower()
    uname = ("@" + str(chat.get("username", "")).lower()) if chat.get("username") else ""
    return cid in ALLOWED_CHATS or (uname and uname in ALLOWED_CHATS)

# ── DeepSeek 问答 ────────────────────────────────────────────────────────────

_ask_sem = threading.Semaphore(3)  # 最多3个问题并发

def ask_deepseek(question: str) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL, timeout=180)
    resp = client.chat.completions.create(
        model=DEEPSEEK_MODEL,
        messages=[
            {"role": "system",
             "content": "你是Telegram群里的中文助手。回答准确、简洁、结构清晰，"
                        "适合聊天窗口阅读；无必要不要超过500字。纯文本输出，不要用Markdown标记。"},
            {"role": "user", "content": question},
        ],
        max_tokens=8192,
    )
    content = (resp.choices[0].message.content or "").strip()
    return content or "（模型未返回内容）"

def run_ask(question: str, chat_id, reply_to: int):
    if not DEEPSEEK_API_KEY:
        send_text(chat_id, "⚠️ 未配置 DEEPSEEK_API_KEY，问答功能未启用。", reply_to)
        return

    def worker():
        if not _ask_sem.acquire(blocking=False):
            send_text(chat_id, "问答请求太多，稍后再试。", reply_to)
            return
        try:
            try:  # 显示"正在输入…"状态（失败不影响主流程）
                requests.post(f"{API}/sendChatAction",
                              json={"chat_id": chat_id, "action": "typing"}, timeout=10)
            except Exception:
                pass
            t0 = time.time()
            answer = ask_deepseek(question)
            log(f"[ask] 完成，用时 {time.time()-t0:.0f}s，问题: {question[:40]}")
            send_text(chat_id, answer, reply_to)
        except Exception as e:
            log(f"[ask] 失败: {e}")
            send_text(chat_id, f"⚠️ 问答失败：{str(e)[:200]}", reply_to)
        finally:
            _ask_sem.release()

    threading.Thread(target=worker, daemon=True).start()

# ── 任务执行（后台线程 + 当日缓存 + 防并发）────────────────────────────────────

_locks = {"qs": threading.Lock(), "zt": threading.Lock()}
_cache: dict = {}   # {(cmd, date): text}

ACK = {
    "qs": "⏳ 开始计算强势板块（全部概念+行业板块，约2-4分钟），完成后发出结果…",
    "zt": "⏳ 开始统计涨停板块集中度…（若是首次运行需构建板块成分缓存，约3-5分钟）",
}

def run_command(cmd: str, chat_id, reply_to: int):
    import market_analysis as ma
    today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
    key = (cmd, today)

    if key in _cache:
        send_text(chat_id, _cache[key] + "\n\n（今日缓存结果）", reply_to)
        return

    lock = _locks[cmd]
    if not lock.acquire(blocking=False):
        send_text(chat_id, "该指令正在计算中，请稍候，算完会直接发到群里。", reply_to)
        return

    def worker():
        try:
            send_text(chat_id, ACK[cmd], reply_to)
            t0 = time.time()
            if cmd == "qs":
                text = ma.format_strong_sectors(ma.get_strong_sectors())
            else:
                text = ma.format_zt_concentration(ma.get_zt_concentration())
            _cache[key] = text
            log(f"[{cmd}] 完成，用时 {time.time()-t0:.0f}s")
            send_text(chat_id, text, reply_to)
        except Exception as e:
            log(f"[{cmd}] 失败: {e}")
            traceback.print_exc()
            send_text(chat_id, f"⚠️ /{cmd} 执行失败：{str(e)[:200]}", reply_to)
        finally:
            lock.release()

    threading.Thread(target=worker, daemon=True).start()

# ── 主循环 ───────────────────────────────────────────────────────────────────

def main():
    me = tg_call("getMe", http_timeout=20)
    bot_username = str(me["username"]).lower()
    log(f"机器人 @{me['username']} 已启动，白名单会话: {ALLOWED_CHATS}")

    # 注册命令菜单（幂等）
    try:
        tg_call("setMyCommands", http_timeout=20, commands=[
            {"command": "qs", "description": "强势板块：上周收涨且MA10>MA20"},
            {"command": "zt", "description": "涨停集中度TOP10板块"},
            {"command": "ask", "description": "DeepSeek回答问题：/ask 你的问题"},
            {"command": "help", "description": "帮助"},
        ])
    except Exception as e:
        log(f"setMyCommands 失败（不影响运行）: {e}")

    # 跳过重启前积压的旧消息
    offset = 0
    try:
        backlog = tg_call("getUpdates", http_timeout=20, offset=-1, timeout=0)
        if backlog:
            offset = backlog[-1]["update_id"] + 1
            log(f"跳过积压消息，offset={offset}")
    except Exception:
        pass

    while True:
        try:
            # payload 的 timeout=50 是 Telegram 服务端长轮询时长
            updates = tg_call("getUpdates", http_timeout=65,
                              offset=offset, timeout=50,
                              allowed_updates=["message"])
        except Exception as e:
            log(f"getUpdates 异常: {str(e)[:150]}，5秒后重试")
            time.sleep(5)
            continue

        for upd in updates:
            offset = upd["update_id"] + 1
            # 单条消息的任何异常都不能拖垮常驻循环
            try:
                msg = upd.get("message") or {}
                chat = msg.get("chat") or {}
                if not chat:
                    continue
                if not chat_allowed(chat):
                    continue
                resolved = resolve_command(msg, bot_username)
                if not resolved:
                    continue
                cmd, arg = resolved
                log(f"收到指令 /{cmd} 来自 chat={chat.get('id')} "
                    f"user={msg.get('from', {}).get('username', '?')}"
                    + (f" 问题: {arg[:40]}" if arg else ""))
                if cmd == "help":
                    send_text(chat["id"], HELP_TEXT, msg.get("message_id"))
                elif cmd == "ask":
                    run_ask(arg, chat["id"], msg.get("message_id"))
                else:
                    run_command(cmd, chat["id"], msg.get("message_id"))
            except Exception as e:
                log(f"处理消息异常（已跳过该条）: {e}")
                traceback.print_exc()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("退出")
