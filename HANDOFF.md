# 项目交接文档（粘贴给新 chat 即可接续进度）

> 用法：把本文件全文粘贴到新会话开头，并说明你要继续的具体任务。
> 这是一个中文项目，请用中文回复用户。

## 项目是什么

`daily-tech-digest` —— 一个广深/亚洲科技日报 Telegram 机器人。定时抓取多源数据（活动/融资/A股行情/宏观日历/政策），用 Kimi 整理后推送到 Telegram。近期新增了指令触发的 A 股板块分析和 DeepSeek 问答。

- 工作目录：`/Users/elie/dev/daily-tech-digest`，git 分支 `main`，作者 C-B-Elite
- 用户身份：中文用户，正准备把项目从 GitHub Actions 迁移到自有服务器
- 运行环境：本机 **必须用 `python3.11`**（不是 `python3`，后者缺 feedparser/akshare 依赖）

## 文件结构与职责

- `bot.py` — 每日日报主程序（一次性跑完退出，供 cron/Actions 调用）。含 5 个任务：
  活动、投融资、A股科技市场、**宏观数据前瞻**、**中国政策快报**（后两个是近期新增）
- `scrapers.py` — 多源采集：Meetup/活动行/TechCrunch RSS/36kr/akshare 行情/宏观日历/快讯
- `market_analysis.py` — **新增**：A股板块分析引擎（供 tg_bot 调用），两个功能见下
- `tg_bot.py` — **新增**：常驻 Telegram 指令机器人（长轮询），指令 /qs /zt /ask
- `deploy/` — **新增**：香港服务器部署文档 + systemd service 文件
- `.github/workflows/daily_digest.yml` — GitHub Actions 定时（每天 00:00 UTC = 北京 08:00）
- `.env`（不入 git）：`KIMI_API_KEY` `TG_TOKEN` `TG_CHAT_ID`，问答需加 `DEEPSEEK_API_KEY`

## 本轮会话做了什么（4 批功能，全部未提交）

### 1. 宏观数据前瞻（bot.py `build_macro_calendar_report`）
- 数据源：华尔街见闻宏观日历 `akshare.macro_info_ws`（北京时间、带预期/前值、重要性分级）
- 窗口：今天→本周日（周六/日发报时自动延到下周日预览下周）
- **对每条数据做情景分析**：前值/预期、超预期→利好还是利空+传导逻辑、不及预期→如何，
  美联储讲话给鹰派/鸽派分支，中国数据细化到 A 股板块，结尾"焦点"点明当前交易框架
- 日历失效时用 Kimi 联网搜索兜底；空结果回退原始日历

### 2. 中国政策快报（bot.py `build_china_policy_report`）
- 窗口：昨天 00:00→发报时刻；双源（快讯+Kimi 联网搜索限定国务院/央行/发改委等）
- Kimi 判定重磅性：**无重磅政策输出 `NO_POLICY`，程序跳过发送**（不发空消息）
- 每条政策带影响分析（利好板块+传导逻辑+力度判断）

### 3. A股板块指令机器人（market_analysis.py + tg_bot.py）
- `/qs` 强势板块：全部东财概念+行业板块（约590个）中，**上周收涨 且 日线MA10>MA20** 的，
  按上周涨幅排序，输出含东财板块代码 `BKxxxx`（约2-4分钟）
- `/zt` 涨停集中度：最近交易日涨停股 TOP10 板块 + 涨停股占成分股比例（首次建成分缓存3-5分钟，
  存 `data/board_members.json`，7天自动刷新）
- 触发方式：/命令、@机器人+关键词、回复机器人消息、私聊。耗时任务先回"⏳"再后台跑；
  当日结果缓存；同指令防并发；只响应白名单会话

### 4. DeepSeek 问答（tg_bot.py `ask_deepseek`）
- `/ask 问题`、@机器人+非关键词文字、回复机器人消息、私聊 → 调 DeepSeek 回答
- 模型 `deepseek-v4-pro`（官方文档确认为当前在售旗舰，1M上下文）

## 关键决策与踩过的坑（这些是非显然的，别重新踩）

1. **Kimi 模型分工**：`KIMI_MODEL=kimi-k3`（分析/整理）+ `KIMI_SEARCH_MODEL=kimi-k2.6`（联网搜索）。
   **k3 目前不支持 `$web_search`**：原样回传 tool_calls 报 `tokenization failed`；把 type 改成
   'function' 则搜索结果不注入、模型凭记忆瞎答（最隐蔽）。回传 assistant 消息必须手工重建 dict
   剔除 reasoning_content，但**原样保留 `tc.type`**（见 bot.py `_assistant_msg_to_dict`）。
2. **模型参数差异**：kimi-k2.x 强制 `temperature=1`；k3 允许低温。kimi-k 系列有思维链会占满输出额度，
   长任务 max_tokens 要给足 16384（见 `_temp_for`/`_max_tokens_for`）。
3. **历史事故**：`kimi-k2-thinking` 被 Moonshot 下线，导致线上日报每天崩溃。**上新模型前必须先用
   `client.models.list()` 核实该 key 可用、再实测搜索链路**，别直接改默认值。
4. **akshare 挂起**：其内部 requests 不设超时会永久阻塞。所有 akshare 调用已用守护线程墙钟超时
   包裹（scrapers.py `_call_with_timeout`）。
5. **财联社快讯接口 2026-07 起 404/挂起**：已加东方财富快讯 `stock_info_global_em` 自动降级。
6. **本机访问不了东方财富行情接口**（代理出口是境外 IP 被东财风控拒绝）。所以 market_analysis 的
   数据链路只能在香港服务器上实测；算法逻辑已用合成数据单测覆盖（30项全过）。
7. **LIST0464 那种板块代码是开盘啦 App 私有 ID**（需登录 token，公开源没有），所以板块代码用
   东财 `BKxxxx` 口径。用户若坚持开盘啦口径需提供抓包 Token。
8. **时区**：GitHub Actions runner 是 UTC，所有"今天/昨天/本周"计算显式用 `BEIJING_TZ`(UTC+8)。

## 当前状态

- ✅ 4 批功能全部实测通过：宏观/政策报告真实生成、K3分析+K2.6搜索双路径验证、
  DeepSeek 问答冒烟测试、板块算法与指令解析 30 项回归全过、4 模块编译通过
- ✅ 部署审计已完成，修了 2 个问题：tg_bot 主循环加单条消息异常保护；涨停回溯窗口 8→12天（覆盖春节）
- ⚠️ **所有改动仍未 git commit**。线上 GitHub Actions 日报仍在每天因旧模型报错
- ⚠️ 本地 `.env` 里**还没有 `DEEPSEEK_API_KEY`**，问答功能要加了才生效

## 下一步（用户的推进方向）

1. **提交推送**：`git add -A && git commit && git push`（推完 Actions 日报即恢复正常）
2. **部署到香港服务器**：阿里云/腾讯云轻量2核2G，Ubuntu 22.04+，见 `deploy/README.md`。
   要点：用 venv（Ubuntu 22.04+ 系统 pip 有 PEP668 限制）；日报走 crontab，机器人走 systemd；
   tg_bot 全网只能一个实例（长轮询 409）；部署后必须实测东财接口在香港 IP 可用；
   跑通后删 Actions 的 schedule 段避免发两份

## 记忆文件

`/Users/elie/.claude/projects/-Users-elie-dev-daily-tech-digest/memory/` 有持久记忆，
`kimi-model-preference.md` 记录了模型分工与 k3 不支持搜索的坑。
