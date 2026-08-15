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

### 1.1 前置：SSH 别名 + Keychain

服务器**不支持密码登录**。管理员已将 `~/.ssh/seventharea_ed25519.pub` 部署到服务器，本地私钥文件是 `~/.ssh/seventharea_ed25519`。以下步骤配好别名并让 launchd 触发的脚本也能用到它。

**a. 配 `~/.ssh/config`**（追加，如无此文件则新建；权限 `chmod 600`）：
```
Host seventharea
    HostName 106.12.173.165
    User jiantouhuagongshixi
    IdentityFile ~/.ssh/seventharea_ed25519
    IdentitiesOnly yes
```

**b. 验证连通**：
```zsh
ssh seventharea "echo ok && hostname"
# 期望：ok
#       <服务器主机名>
```

**c. Key 装进 macOS Keychain**（launchd 触发 mac-daily.sh 时不继承交互 shell 的 ssh-agent，不装进 Keychain 就会因为找不到 key 而失败）：
```zsh
ssh-add --apple-use-keychain ~/.ssh/seventharea_ed25519
```

之后所有命令统一用别名 `seventharea`，不要写裸 IP/用户名。

### 1.2 编辑 `deploy/mac-daily.sh`

配好 §1.1 别名后，`SERVER` 直接填 `seventharea`（ssh 会自动读 config 里的 HostName / User / IdentityFile）：
```bash
SERVER="seventharea"
REMOTE_DATA="<APP_PATH_ON_SERVER>/data"   # 待项目负责人指定正式目录后填入
```

改完手动跑一次验证：
```zsh
./deploy/mac-daily.sh
tail -30 data/logs/mac-daily.log
# 期望最后一行：SUCCESS 2026-08-15 15:30:12
```

服务器上确认数据到位：
```zsh
ssh seventharea \
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

### 2.2 目录 + 代码 + venv

> ⚠️ **正式部署目录待定**：`<APP_PATH_ON_SERVER>` 需由项目负责人指定，先确认再执行下面的命令，不要自作主张选 `/var/www` 或 `/opt`。以下用 `<APP_PATH_ON_SERVER>` 作占位符。

> ⚠️ **`<SYSTEM_USER>` 必须等于 SSH 登录用户**（即 `jiantouhuagongshixi`）。gunicorn.service 里的 `User=<SYSTEM_USER>` 要跟传数据库的用户一致，否则 Mac 上传过来的 `news.db` 属主是 `jiantouhuagongshixi`，而 Flask 以别的用户运行时读不到（权限 600）。

**a. 建目录**：
```bash
sudo mkdir -p <APP_PATH_ON_SERVER>
sudo chown jiantouhuagongshixi:jiantouhuagongshixi <APP_PATH_ON_SERVER>
cd <APP_PATH_ON_SERVER>
```

**b. 代码上传 — 二选一**：

**方案 A（推荐 · Git 私有仓库）**。适合以后代码有更新，`git pull` 即可。
```bash
# 前置：Mac 上先把本地仓库推到 GitHub 私有 repo
# 在 https://github.com/new 建 private repo；不要初始化 README
# 然后在 Mac 项目根：
git remote add origin git@github.com:<GITHUB_USER>/<REPO_NAME>.git
git branch -M main
git push -u origin main

# 服务器 deploy key（read-only）
ssh seventharea "ssh-keygen -t ed25519 -N '' -f ~/.ssh/gh_deploy -C 'deploy-key'"
ssh seventharea "cat ~/.ssh/gh_deploy.pub"
# 把输出的公钥贴到 GitHub repo 的 Settings → Deploy keys → Add key（只勾 Read access）

# 服务器 SSH 配置认 GitHub
ssh seventharea 'cat >> ~/.ssh/config <<EOF
Host github.com
    IdentityFile ~/.ssh/gh_deploy
    IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config'

# 服务器上 clone
ssh seventharea "cd <APP_PATH_ON_SERVER> && git clone git@github.com:<GITHUB_USER>/<REPO_NAME>.git ."
```

**方案 B（直接 rsync，不用 git）**。适合暂时不想开 GitHub 仓库、代码改动频率极低的场景。
```zsh
# Mac 上：一次性同步（排除 venv、DB、日志、缓存）
rsync -avz --delete \
  --exclude='.venv' --exclude='data/news.db*' --exclude='data/logs' \
  --exclude='__pycache__' --exclude='.git' --exclude='*.bak' \
  ./ seventharea:<APP_PATH_ON_SERVER>/
```
以后每次改代码要重新跑一次 rsync（等于手动"pull"）。这个路径没有版本历史保护，代码回滚只能靠 Mac 端 git。

**c. venv + 依赖**：
```bash
ssh seventharea "cd <APP_PATH_ON_SERVER> && \
  python3 -m venv .venv && \
  .venv/bin/pip install --upgrade pip && \
  .venv/bin/pip install -r requirements.txt"
```

**d. 建 data 目录**（`.gitignore` 里所以 clone 后不存在，scp DB 之前必须先建）：
```bash
ssh seventharea "mkdir -p <APP_PATH_ON_SERVER>/data/logs"
```

### 2.3 首份 DB 从 Mac 传过去
```zsh
# 在 Mac 上
sqlite3 data/news.db "VACUUM INTO 'data/news.db.snapshot'"
scp data/news.db.snapshot seventharea:<APP_PATH_ON_SERVER>/data/news.db
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

# 禁用 Ubuntu 默认站点。deploy/nginx.conf 用了 default_server，
# 与自带的 /etc/nginx/sites-enabled/default 冲突会让 nginx -t 直接失败。
sudo rm -f /etc/nginx/sites-enabled/default

# 装 server block
sudo cp deploy/nginx.conf /etc/nginx/sites-available/ai-tech-intel
sudo ln -s /etc/nginx/sites-available/ai-tech-intel /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

### 2.7 先本机 curl 验证

在公网 80 端口放行到位之前（见 §2.8），站点只能从服务器本机访问：
```bash
ssh seventharea "curl -sI http://127.0.0.1/ -u <USERNAME>:<PASSWORD>"
# 期望：HTTP/1.1 200 OK
ssh seventharea "curl -s http://127.0.0.1/ -u <USERNAME>:<PASSWORD> | head -30"
# 期望：检索页 HTML 开头
```
不带 `-u` 应收到 `HTTP/1.1 401 Unauthorized`——说明 Basic Auth 正在工作。

### 2.8 公网 80 端口放行（需项目负责人批准）

服务器所在网络的安全组/防火墙策略由项目负责人统一管理，**你不能自己 `ufw` 或 `iptables` 改规则**。

**申请内容**：
- 目标：`106.12.173.165`
- 端口：`80/tcp`
- 方向：入站（inbound）
- 源 IP：暂按"任意"申请，后续需要收紧再改；或先只白名单你办公 IP
- 备注：AI 产业链新闻监测系统私有检索站，nginx 层已启用 Basic Auth

**放行到位后**从浏览器打开 `http://106.12.173.165/`，弹 Basic Auth 输入 htpasswd 里的用户名密码，应看到检索页。

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
- 服务器磁盘：`ssh seventharea "df -h <APP_PATH_ON_SERVER>"`
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
- 但可以在服务器上加个周备份：`ssh seventharea "cd <APP_PATH_ON_SERVER> && sqlite3 data/news.db \"VACUUM INTO 'data/news.db.weekly-\$(date +%V).snapshot'\""`

恢复：
```zsh
# 从任何一份 .bak 恢复
cp data/news.db.bak-YYYYMMDD data/news.db
./deploy/mac-daily.sh   # 立即同步到服务器
```

## 七、变更工作流（改了代码怎么上）

1. Mac 本地改代码 + 测试 + `git commit`

2. 走 §2.2 定的部署方案：
   - **方案 A（Git）**：`git push`；服务器 `ssh seventharea "cd <APP_PATH_ON_SERVER> && git pull"`
   - **方案 B（rsync）**：Mac 上再跑一次 §2.2 里的 rsync 命令

3. 如依赖变了：
   ```bash
   ssh seventharea "cd <APP_PATH_ON_SERVER> && .venv/bin/pip install -r requirements.txt"
   ```

4. 重启 web 服务：
   ```bash
   ssh seventharea "sudo systemctl restart ai-tech-intel-web"
   ```

5. 浏览器验证

**配置文件（sources.json / watch_keywords.json / paywall_markers.json）** 只在 Mac 上有意义——它们决定抓什么。服务器上的这些文件不生效（因为服务器不跑 run.py）。但仍要同步保证代码引用一致。

## 八、TLS 后续

现在只跑 HTTP。域名和 TLS 什么时候加：
- **域名到位后**：`nginx.conf` 里改 `server_name`，去掉 `default_server`
- **TLS**：`sudo apt install certbot python3-certbot-nginx && sudo certbot --nginx -d your.example.com`——自动改 nginx 配 + 自动续期
- Basic Auth 仍然保留（防搜索引擎爬 + 防随手扫）

## 九、一次性问题清单（第一次装的时候会碰到）

### 前置沟通（不做完不要动手）
- [ ] 从项目负责人处确认 `<APP_PATH_ON_SERVER>` 正式目录（不要自己决定 `/var/www` 之类）
- [ ] 从项目负责人处提交 80 端口安全组放行申请（见 §2.8），拿到批准回执

### Mac 端
- [ ] `~/.ssh/config` 里配好 `Host seventharea` 别名（HostName / User / IdentityFile）
- [ ] `ssh seventharea "echo ok"` 正常返回
- [ ] `ssh-add --apple-use-keychain ~/.ssh/seventharea_ed25519` 已跑（launchd 需要 Keychain 里的 key）
- [ ] `deploy/mac-daily.sh` 里 `REMOTE_DATA` 已填正式路径，`SERVER="seventharea"`
- [ ] `deploy/com.viceng.aitechintel.plist` 里三处 `<APP_PATH_ON_MAC>` 已替换
- [ ] 第一次 `./deploy/mac-daily.sh` 手动跑成功，`tail data/logs/mac-daily.log` 最后一行是 `SUCCESS ...`

### 服务器端
- [ ] `<SYSTEM_USER>` **等于** SSH 登录用户 `jiantouhuagongshixi`（gunicorn.service 里的 User=）
- [ ] 服务器时区：`ssh seventharea "timedatectl"` 应显示 `Asia/Shanghai`
- [ ] 服务器 SQLite 版本：`ssh seventharea "sqlite3 --version"` ≥ 3.34
- [ ] 服务器 Python 版本：`ssh seventharea "python3 --version"` ≥ 3.12（Ubuntu 24.04 自带）
- [ ] `<APP_PATH_ON_SERVER>/data/logs` 已建（.gitignore 里所以 clone/rsync 不会带过来）
- [ ] `<APP_PATH_ON_SERVER>/.env` 已从 `.env.example` 复制并按 §2.4 精简
- [ ] `gunicorn.service` 里 `<SYSTEM_USER>` / `<APP_PATH>` 已替换、`sudo systemctl status ai-tech-intel-web` = active
- [ ] `/etc/nginx/sites-enabled/default` 已删（避免与 `default_server` 冲突）
- [ ] Basic Auth 密码复杂度：至少 16 字符（这是唯一防线）
- [ ] 本机 curl 验证通过：`ssh seventharea "curl -sI http://127.0.0.1/ -u <USER>:<PASS>"` = 200
- [ ] 安全组批准后，浏览器打开 `http://106.12.173.165/` 能看到检索页
