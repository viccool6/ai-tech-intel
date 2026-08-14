# 部署手册

服务器只做展示（Flask + nginx），所有抓取/AI/翻译在 Mac 上跑，跑完把 SQLite 快照 rsync/scp 到服务器原子替换。

## 架构

```
Mac（数据生产端）                             Server: Ubuntu 24.04（展示端）
┌──────────────────────────────┐             ┌──────────────────────────────┐
│ launchd 03:15 触发            │             │ gunicorn (systemd)           │
│ └ deploy/mac-daily.sh        │             │ └ app.py (Flask)             │
│    ├ run.py --hours 24       │             │    ├ 只读 SQLite             │
│    │  ├ fetch (Bing+Google+  │   scp新库    │    └ Basic Auth（nginx层）    │
│    │  │        直连6源)      │─────────▶  │ nginx :80                    │
│    │  ├ AI 摘要 (Flash)      │  ssh mv     │ └ 反代 127.0.0.1:5000        │
│    │  └ 翻译 (Pro)           │  原子替换    └──────────────────────────────┘
│    ├ VACUUM INTO snapshot    │
│    └ scp + ssh mv            │
│ data/news.db (WAL)           │
└──────────────────────────────┘
```

Google News 在服务器上不通、OpenAI 有 Cloudflare 403，这些坑在 Mac 上完全没有——Mac 上四张网都通（Bing、Google、DeepSeek、GitHub）。服务器只是把 SQLite 里的东西呈现出来。

## 一、Mac 端配置

### 1.1 前置：确认 SSH 免密到服务器可用

```zsh
ssh <SERVER_SSH_USER>@<SERVER_HOST> "echo ok"
# 期望：ok
```

如果还没配 key：
```zsh
ssh-keygen -t ed25519 -C "$USER@$(hostname -s)"        # 一路回车
ssh-copy-id <SERVER_SSH_USER>@<SERVER_HOST>
```

launchd 触发 mac-daily.sh 时**不会**继承交互式 shell 的 ssh-agent。两种解法：
- **推荐**：把 SSH key 加进 macOS Keychain：`ssh-add --apple-use-keychain ~/.ssh/id_ed25519`（一次生效）
- 或者用无密码 key（安全性略弱，看你）

### 1.2 编辑 `deploy/mac-daily.sh`

替换三处占位符：
```bash
SERVER="<SERVER_SSH_USER>@<SERVER_HOST>"
REMOTE_DATA="<APP_PATH_ON_SERVER>/data"
```

改完手动跑一次验证：
```zsh
./deploy/mac-daily.sh
tail -30 data/logs/mac-daily.log
# 期望最后一行：SUCCESS 2026-08-14 15:30:12
```

服务器上确认数据到位：
```zsh
ssh <SERVER_SSH_USER>@<SERVER_HOST> \
  "sqlite3 <APP_PATH_ON_SERVER>/data/news.db 'SELECT COUNT(*) FROM articles'"
```

### 1.3 装 launchd 定时

编辑 `deploy/com.viceng.aitechintel.plist`，把三处 `<APP_PATH_ON_MAC>` 替换成 `pwd` 结果。

```zsh
# 装载
cp deploy/com.viceng.aitechintel.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.viceng.aitechintel.plist

# 立即试跑（不等 03:15）
launchctl start com.viceng.aitechintel
tail -f data/logs/mac-daily.log

# 查看下一次触发时间
launchctl list com.viceng.aitechintel
```

改脚本或 plist 后要重新装：
```zsh
launchctl unload ~/Library/LaunchAgents/com.viceng.aitechintel.plist
cp deploy/com.viceng.aitechintel.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.viceng.aitechintel.plist
```

## 二、服务器端首次部署

### 2.1 系统包
```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3-pip \
                    sqlite3 nginx apache2-utils git
```

### 2.2 拉代码 + venv
```bash
# 目录结构（示例）
sudo mkdir -p /var/www/ai-tech-intel
sudo chown $USER:$USER /var/www/ai-tech-intel
cd /var/www/ai-tech-intel

git clone <REPO_URL> .

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

### 2.3 首份 DB 从 Mac 传过去
```zsh
# 在 Mac 上
sqlite3 data/news.db "VACUUM INTO 'data/news.db.snapshot'"
scp data/news.db.snapshot <SERVER_SSH_USER>@<SERVER_HOST>:/var/www/ai-tech-intel/data/news.db
```

### 2.4 .env（服务器版本可以很短）
```bash
cd /var/www/ai-tech-intel
cp .env.example .env
```

服务器不抓取，不调 AI，`.env` 只需要保证 Flask 能起来。**AI_API_KEY / TRANSLATE_API_KEY 可以留空**——app.py 只读 DB，不发起任何 AI 请求。核心一行：
```
FLASK_DEBUG=false
```
其它字段照抄 `.env.example` 即可，不需要真实的 key。

### 2.5 gunicorn systemd 单元
```bash
sudo cp deploy/gunicorn.service /etc/systemd/system/ai-tech-intel-web.service
sudo nano /etc/systemd/system/ai-tech-intel-web.service
# 替换 <SYSTEM_USER> 和 <APP_PATH> 占位符
sudo systemctl daemon-reload
sudo systemctl enable --now ai-tech-intel-web
sudo systemctl status ai-tech-intel-web
```

**注意**：`deploy/ai-tech-intel.service` 和 `deploy/ai-tech-intel.timer`（抓取定时）**不在服务器上安装**。这两个是备用（比如以后服务器网络能直连 Google News 了，可以启用），当前架构下 launchd 在 Mac 上负责调度。

### 2.6 nginx 反代 + Basic Auth
```bash
# 建密码文件（首个用户用 -c，后续加人不加 -c）
sudo htpasswd -c /etc/nginx/.htpasswd-ai-tech-intel <USERNAME>
# 输入密码

# 装 server block
sudo cp deploy/nginx.conf /etc/nginx/sites-available/ai-tech-intel
sudo ln -s /etc/nginx/sites-available/ai-tech-intel /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

浏览器打开 `http://<SERVER_HOST>/`，输入 htpasswd 里的用户名密码。应看到检索页 + 目前的文章。

## 三、Flask 连接 DB 的行为（为什么不用重启）

- Flask 用 `g.db` 每请求打开新连接
- Mac 的 `mv news.db.new news.db` 在同一文件系统内是**原子**操作
- 已在处理请求的 gunicorn worker 继续用旧 inode（fd 已锁着），请求完成后连接关闭
- 新请求打开新 inode → 立刻看到新数据
- `Storage.__init__` 对新文件运行 `PRAGMA journal_mode=WAL`（一次性生效并持久化到 DB 文件）
- 所以：mac-daily.sh 结尾不需要 `systemctl reload`

如果想更保守，脚本末尾加：
```bash
ssh "$SERVER" "sudo systemctl reload ai-tech-intel-web"
```
这需要 sudoers 免密配置（`<SYSTEM_USER> ALL=(root) NOPASSWD: /bin/systemctl reload ai-tech-intel-web`），一般用不上。

## 四、诊断：出问题怎么查

### 症状：网站里看到的文章几天没更新
```zsh
# Mac 上 - launchd 上次触发时间和退出码
launchctl list com.viceng.aitechintel
# LastExitStatus=0 = 成功；非 0 需要看日志

# 脚本日志
tail -80 data/logs/mac-daily.log

# launchd 自己的日志（如果脚本压根没跑到 exec 那行）
tail data/logs/launchd.err

# Mac 本地 DB 时间戳
ls -la data/news.db

# 服务器上 DB 时间戳（应该和 Mac 差不多）
ssh <SERVER_SSH_USER>@<SERVER_HOST> "ls -la /var/www/ai-tech-intel/data/news.db"
```

### 症状：Flask 500 / 502
```bash
# 服务器上
sudo journalctl -u ai-tech-intel-web -n 100 --no-pager
sudo journalctl -u ai-tech-intel-web -f  # 实时
```
常见原因：
- **DB 权限**：mac-daily.sh 用你的 SSH 账户传，`<SYSTEM_USER>` 是否能读那个文件？`ls -la data/news.db`
- **DB 损坏**：`sqlite3 data/news.db "PRAGMA integrity_check"` 应返回 `ok`
- **SQLite 版本**：FTS5 trigram tokenizer 需要 SQLite ≥3.34，Ubuntu 24.04 装的是 3.45，够
- **模块缺失**：`.venv/bin/pip list | grep -E "Flask|dotenv"`

### 症状：Basic Auth 一直弹窗
```bash
sudo ls -la /etc/nginx/.htpasswd-ai-tech-intel   # 存在？
sudo tail /var/log/nginx/ai-tech-intel.error.log # 报什么错？
```

### 症状：Mac 上 DeepSeek API 拒 400
```zsh
tail -100 data/logs/latest.log | grep -E "HTTP|preflight|拒"
```
`ai_client.py` 的 preflight 会在发送前拦下"提示词无 json 字面"的错误。真的是 API 返回 400 的话，ai_error 字段会有完整 body。查：
```zsh
sqlite3 data/news.db "SELECT id, substr(ai_error,1,200) FROM articles WHERE ai_status='fallback' ORDER BY id DESC LIMIT 5"
```

### 症状：翻译预算突然涨
```zsh
# 单日新增翻译量
sqlite3 data/news.db "SELECT DATE(archived_at), COUNT(*) FROM articles WHERE translate_status='success' GROUP BY DATE(archived_at) ORDER BY 1 DESC LIMIT 7"
```
可能原因：
- 抓到批量新内容（Bing/Google 突然爆发）
- 手工跑了 `--reprocess-ai` 或 `--translate` 没加 `--dry-run` 或 `--only-missing`
- DeepSeek 涨价（不常见）

DeepSeek 后台账单页可以核对。

## 五、周维护

**每周一（10 分钟）**
- 检查 launchd 上周成功率：`ls -la data/logs/` 看每天有没有 launchd.err 增量
- 抽看一次网站：搜关键词、看新条数是否合理
- 查付费墙命中：`sqlite3 data/news.db "SELECT COUNT(*) FROM articles WHERE fetch_status='paywalled'"` — 突然升高可能有新站要加 marker

**每月一次**
- 清 90 天前手动备份：`ls data/news.db.bak-* | ...`（自动化可选）
- 检查 Bing 源命中率：`sqlite3 data/news.db "SELECT source_id, COUNT(*) FROM articles WHERE source_id LIKE 'bing_%' AND archived_at > DATE('now','-30 days') GROUP BY source_id"` — 长期 0 的可以删
- 服务器磁盘：`ssh server "df -h /var/www"`
- DeepSeek 账单页对账：目标场景 B ≤ ¥20/月

**每季度**
- 升级依赖：`.venv/bin/pip install -U -r requirements.txt`（先在 Mac 试，通过后同步到服务器）
- 复查 `config/paywall_markers.json` 是否要补 markers（观察 fetch_error 里 paywalled 的实际字符串分布）
- 复查关键词表（watch_keywords.json）是否要补概念/删旧词

## 六、备份与恢复

Mac 侧：
- `data/news.db` 是唯一权威副本
- 大改（比如 `python -m src.storage reindex`）前手动：`cp data/news.db data/news.db.bak-$(date +%Y%m%d)`

服务器侧：
- 服务器上的 DB 只是 Mac 的复制品，丢了不心疼——从 Mac 重传一份即可
- 但可以在服务器上加个周备份：`ssh server "sqlite3 data/news.db \"VACUUM INTO 'data/news.db.weekly-\$(date +%V).snapshot'\""`

恢复：
```zsh
# 从任何一份 .bak 恢复
cp data/news.db.bak-YYYYMMDD data/news.db
./deploy/mac-daily.sh   # 立即同步到服务器
```

## 七、变更工作流（改了代码怎么上）

1. Mac 本地改代码 + 测试
2. `git commit` + `git push`
3. 服务器：
   ```bash
   cd /var/www/ai-tech-intel
   git pull
   .venv/bin/pip install -r requirements.txt  # 如依赖变了
   sudo systemctl restart ai-tech-intel-web
   ```
4. 浏览器验证

**配置文件（sources.json / watch_keywords.json / paywall_markers.json）** 只在 Mac 上有意义——它们决定抓什么。服务器上的这些文件不生效（因为服务器不跑 run.py）。但仍要 pull 保证代码引用一致。

## 八、TLS 后续

现在只跑 HTTP。域名和 TLS 什么时候加：
- **域名到位后**：`nginx.conf` 里改 `server_name`，去掉 `default_server`
- **TLS**：`sudo apt install certbot python3-certbot-nginx && sudo certbot --nginx -d your.example.com`——自动改 nginx 配 + 自动续期
- Basic Auth 仍然保留（防搜索引擎爬 + 防随手扫）

## 九、一次性问题清单（第一次装的时候会碰到）

- [ ] Mac SSH key 免密到服务器 OK
- [ ] `~/.ssh/config` 有 alias（可选，比裸 IP/user 好记）
- [ ] Mac 上 `ssh-add --apple-use-keychain` 加载了 key（launchd 需要）
- [ ] 服务器时区：`ssh server "timedatectl"`——检索页的日期依赖它
- [ ] 服务器 SQLite 版本：`ssh server "sqlite3 --version"` ≥ 3.34
- [ ] 服务器 Python 版本：3.12+ (Ubuntu 24.04 自带)
- [ ] nginx server block 里 `<DOMAIN>` 已改（当前用 `default_server`，无需域名）
- [ ] Basic Auth 密码复杂度：至少 16 字符（这是唯一防线）
- [ ] launchd plist 里 `<APP_PATH_ON_MAC>` 已替换
- [ ] mac-daily.sh 里 `<SERVER_SSH_USER>/<SERVER_HOST>/<APP_PATH_ON_SERVER>` 已替换
- [ ] gunicorn.service 里 `<SYSTEM_USER>/<APP_PATH>` 已替换
- [ ] 第一次 `./deploy/mac-daily.sh` 手动跑成功
- [ ] 浏览器打开 nginx 站点，Basic Auth 弹窗后能看到文章
