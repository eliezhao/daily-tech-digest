# 服务器部署指南（阿里云/腾讯云 香港轻量服务器）

两个独立进程：
1. **每日日报** `bot.py` — crontab 定时（北京时间 08:00），替代 GitHub Actions
2. **指令机器人** `tg_bot.py` — systemd 常驻（长轮询，无需公网入站端口）

> 为什么选香港：Telegram API 被墙，大陆机房直连不通；香港在墙外可直连
> Telegram，同时访问 Kimi/DeepSeek/东财/华尔街见闻等国内数据源延迟极低，
> 且免 ICP 备案。规格 2核2G 即可，Ubuntu 22.04/24.04。

## 0. 前置：代码先推到仓库

服务器用 `git clone` 拉代码，部署前确认本地改动已 commit + push。
若仓库是私有的，clone 时需要凭据：最简单是 GitHub → Settings → Developer settings
→ Personal access tokens 生成只读 token，然后
`git clone https://<token>@github.com/<user>/<repo>.git /opt/daily-tech-digest`。

## 1. 初始化（venv 方案，绕开 Ubuntu 的 PEP 668 限制）

```bash
sudo apt update && sudo apt install -y python3 python3-venv git
sudo git clone <你的仓库地址> /opt/daily-tech-digest
cd /opt/daily-tech-digest
sudo python3 -m venv venv
sudo ./venv/bin/pip install -r requirements.txt
```

> 不要用 `sudo pip3 install` 装到系统 Python——Ubuntu 22.04+ 会报
> `externally-managed-environment`。全文统一用 `./venv/bin/python`。

## 2. 配置密钥

```bash
sudo cp .env.example .env
sudo vim .env
# 必填：KIMI_API_KEY / TG_TOKEN / TG_CHAT_ID
# 问答功能：DEEPSEEK_API_KEY（不填则 /ask 提示未启用）
sudo chmod 600 .env
```

`.env` 不在 git 里（已 gitignore），必须在服务器上手动创建。

## 3. 时区设为北京时间（crontab 直观）

```bash
sudo timedatectl set-timezone Asia/Shanghai
```

## 4. 每日日报 crontab

```bash
sudo crontab -e
# 添加：
0 8 * * * cd /opt/daily-tech-digest && ./venv/bin/python bot.py >> /var/log/daily-digest.log 2>&1
```

## 5. 指令机器人 systemd

```bash
sudo cp deploy/tg-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tg-bot
journalctl -u tg-bot -f        # 跟日志，应看到"机器人 @xxx 已启动"
```

## 6. 部署后验证

```bash
cd /opt/daily-tech-digest
./venv/bin/python market_analysis.py zt   # 涨停集中度（首次构建成分缓存约3-5分钟）
./venv/bin/python market_analysis.py qs   # 强势板块（约2-4分钟）
./venv/bin/python bot.py                  # 手动跑一次完整日报（会真实发 Telegram）
```

然后在 TG 群里测试：
- `/zt` 或 `/qs@infomation_assistant_bot` — 板块分析
- `/ask 用一句话介绍非农数据` — DeepSeek 问答
- 回复机器人的任意消息发问题 — 也会触发问答

## 7. 停用 GitHub Actions 定时（避免每天发两份）

服务器跑通后，删除 `.github/workflows/daily_digest.yml` 中的 `schedule:` 段
（保留 `workflow_dispatch:` 可手动触发当备用）。

## 8. 让 @提及 触发问答（可选）

BotFather → `/setprivacy` → 选择机器人 → `Disable`，然后把机器人**移出群再拉回**
才生效。不关隐私模式时：斜杠命令、"回复机器人消息"提问不受影响。

## 注意事项

- **tg_bot 全网只能跑一个实例**：Telegram getUpdates 同一时间只允许一个消费者，
  本地调试和服务器同时跑会互相报 409 Conflict。
- **首次 /zt 较慢**：要构建 500+ 板块成分缓存（`data/board_members.json`），
  约3-5分钟；之后秒出，缓存 7 天自动重建。
- **东财接口风控**：对海外 IP 有限制，香港 IP 一般正常；代码已内置超时+重试，
  偶发失败稍后重试即可。
- **防火墙**：两个进程都是纯出站连接，入站只需放行 SSH(22)，其余端口全关。
- **日志**：`/var/log/daily-digest.log` 会缓慢增长，每几个月清一次，或配 logrotate。
- **升级流程**：`cd /opt/daily-tech-digest && sudo git pull && sudo systemctl restart tg-bot`。
