# AI 产业链新闻监测系统 — 项目启动上下文

> 这份文件是新会话的入口。开始动手前先读一遍，别绕过已经踩过的坑。

## 项目定位

抓取国外 AI 产业链新闻 → 走 AI 做中文摘要 + 1–5 分重要性打分 → 4 分以上的高价值稿全文双语翻译 → 沉淀到 Flask 检索站供人查询。

**用户**：中信建投能源化工组券商研究实习生，用来做 AI 产业链情报跟踪。**不是**公开产品，是私有工具。

## 架构

**Mac 生产 / Server 展示 分离**（当前部署模型，见 deploy.md）：
- Mac 上 launchd 定时跑 `run.py`：抓取 + AI 摘要 + 翻译，写本地 SQLite
- Mac 跑完 `VACUUM INTO` 生成快照，scp 到服务器
- 服务器上一条 `mv` 原子替换 `data/news.db`；gunicorn 不用重启
- 服务器 = 只读 Flask 站点 + nginx Basic Auth
- 服务器上**不装** cron/timer；`deploy/ai-tech-intel.service+.timer` 留作备用（未来服务器网络通畅时可启用）

| 层 | 组件 | 说明 |
|---|---|---|
| 采集入口（Mac） | `run.py` | 抓取 → 解析 → AI → 翻译 → 落库。launchd 03:15 触发 `deploy/mac-daily.sh` |
| 数据同步 | `deploy/mac-daily.sh` | VACUUM INTO 快照 + scp + ssh 原子 mv |
| Web 站点（Server） | `app.py` (Flask) | 检索页 + 详情页；gunicorn 常驻，只监听 127.0.0.1:5000 |
| 模板（Server） | `templates/base.html` | 设计变量 + 报头 + 共用组件；`search.html` / `article.html` 都 `extends` 它。**没有 static/ 目录**，CSS/JS 全内联在模板里，nginx 不需要加 location 规则 |
| 存储 | SQLite (`data/news.db`) + FTS5 影子表 | trigram 分词；WAL 模式；触发器保 matched_keywords/FTS 同步 |
| AI | DeepSeek 官方 API（仅 Mac 侧） | 摘要 = V4-Flash（¥1/¥2 per M）；翻译 = V4-Pro（¥3/¥6 per M） |
| 反代（Server） | nginx | Basic Auth 鉴权（`/etc/nginx/.htpasswd-ai-tech-intel`） |
| 进程管理（Server） | systemd | 只装 `ai-tech-intel-web.service`（gunicorn） |
| 环境变量 | python-dotenv | app.py 和 run.py 都 `load_dotenv()`；systemd **不用** `EnvironmentFile`（USER_AGENT 有空格分号，systemd 解析会挂） |

## 已定决策及原因

每条都要连原因看，别只记结论。

### FTS5 分词用 trigram，不用 jieba
- trigram 是 SQLite 内置，零依赖
- jieba 要走 loadable extension，部署时得编译，运维成本高
- 代价：≤2 字 CJK 查询走不了 FTS，落到 LIKE 全表兜底——已按字段权重打分补偿，可接受

### `档案库/` 保留但不写入
- Round 5A 把 `html_generator.py` 删了，不再生成静态 HTML
- 目录留着做历史备份，方便未来回溯"某天生成的原始快照长什么样"
- 部署 systemd 单元的 `ReadWritePaths` **不列** `档案库/`——那目录不在 git 里，服务器上不存在，`ProtectSystem=strict` 会因为路径不存在启动失败

### 鉴权走 nginx Basic Auth，Flask 里零登录逻辑
- 是私有工具，不需要多用户/权限/密码找回
- Flask 里不写登录 = 零 session 管理、零密码 hash 代码 = 零攻击面
- nginx Basic Auth 一层就够，密码文件 `htpasswd -c` 手动管

### 部署 systemd + nginx，不用 Docker
- 服务器已有 nginx 和 Python，无需引入 Docker daemon
- 只有 2 个 Python 进程（web + cron），Docker 是过度工程
- systemd 的 `ProtectSystem=strict` + `ReadWritePaths=<APP_PATH>/data` 已把文件系统攻击面收紧到只允许写 DB 目录

### 翻译门槛：`score >= 4` **AND** `ai_status = 'success'`
- 4 分门槛：3 分及以下的新闻价值不足以花翻译 API 钱
- `ai_status = 'success'` 门槛：规则回退（fallback）打的分是关键词硬编码给的，**不可信**——花翻译钱去翻不可信打分是双重浪费
- 两处 gate 必须 lockstep：`run.py._should_translate()` 和 `storage.articles_needing_translation()` 的 SQL；改一处漏一处会静默漂移
- 阈值常量在 `src/config.py`：`SCORE_THRESHOLD_FULL_BODY = 4`，同一常量也决定详情页是否展示"抓取到的正文"

### 关键词表 20 个（9 中文概念 + 11 英文专有名词）
- 中文覆盖产业动向词：算力 / 芯片 / 数据中心 / 供应链 / 先进封装 / 资本开支 / 产能 / 出口管制 / 推理
- 英文覆盖厂商与技术缩写：HBM / GPU / ASIC / CoWoS / DRAM / NVIDIA / TSMC / SK Hynix / Micron / AMD / Broadcom
- 增删关键词后必须 `python run.py --add-keyword X` / `--remove-keyword X`——它会触发 `storage.reindex()` 把 matched_keywords 列和 FTS 影子表都重建

## 红线

### 绝不改历史提交
- 不用 `git commit --amend`，任何情况都不用
- 错了就发一个新 commit 修正，别改历史
- 用户明确要求过多次

### 每轮改动前后必须 git commit
- 一轮 = 一个明确的功能/修复目标
- commit message 写为什么，不只写做了什么
- 不要为了"更干净"合并成一个大 commit——粒度要求逐轮

### `--reprocess-ai` / `--translate` / `--refetch-content` + `--no-ai` 是破坏性组合
- 这些命令的常规行为是重新处理，`--no-ai` 让它们走**规则回退**——rules-only 分数 + rss_summary 顶替，**会静默覆盖已有的 AI 中文标题/摘要/评分/翻译**
- `run.py._guard_destructive_no_ai()` 已经拦截：命中就 exit 3 报错
- 要预览候选：加 `--dry-run`
- 明确要用规则回退覆盖历史：加 `--force`
- 我曾把这类命令写进"验证清单"造成用户 id=17 翻译被清空——永远不要再把它们标为安全命令

### DeepSeek 提示词必须含 "json" 字面（case-insensitive）
- `response_format={"type":"json_object"}` 模式下 DeepSeek 硬性要求 messages 里出现 "json"，否则 400
- `src/ai_client.py` 有 preflight：请求前本地检测，找不到 "json" 直接 fallback + 明确报错，不发请求
- 修改 `prompts/news_analysis.md` 时千万别把"JSON"字样去掉

## 已知问题与取舍

### 服务器网络：Google News 完全不通
- `news.google.com` 从服务器访问 HTTP 000（连不上），4 个 `google_*` 源在服务器上无法直接工作
- **应对方案**：引入 Bing News 备胎（`bing.com/news/search?q=X&format=RSS&mkt=en-us`）——服务器可达，字段结构类似
- 待办：给 Google 源加 `requires_proxy: true` 字段，fetcher 检测到该字段且当前网络不可达时**跳过并记录日志**，不让整轮抓取失败

### Cloudflare 403 阻断正文抓取
- OpenAI（openai.com/*）、Axios 等站前置 Cloudflare Bot Management，`requests` 拿 10KB 挑战页而非 HTML
- **应对方案**：`openai_news` / `deepmind_blog` / `nvidia_blog` 在 `sources.json` 标 `trust_rss_date: true`——网页抓不到时把 RSS 日期升为 `date_source='rss.trusted'`（视同 verified），正文只显示 RSS 摘要
- 详情页对 score<4 的低分文章本来就不展开正文，Cloudflare 站的低分稿实际不亏损

### 2 字 CJK 查询走 LIKE 兜底
- trigram 分词需要 3+ 字符
- 2 字如"液冷"/"算力"落到 `LIKE '%X%'` 全表扫，覆盖字段：title_cn/title_original/summary_cn/article_text/article_text_cn/publisher/source_name/industry_tags/companies/matched_keywords
- 相关度打分：title 命中 +3、summary +2、正文 +1，同一字段命中只算一次（长文不因篇幅刷分）
- 页面顶部有黄色 banner 提示"是 2 字词，已用全文模糊匹配"

### JS-driven 付费墙检测不了（KED Global 类）
- 付费墙检测（`config/paywall_markers.json` + `_paywall_marker_hit`）只对**文本型**付费墙有效——FT / WSJ / Bloomberg 等在 HTML 里写"members only / subscribe to continue"字样的
- KED Global 之类用 JavaScript overlay 实现的付费墙，正文没有关键词字面，检测不到，会以 `fetch_status='summary_only'` 落库（正文 < 500 字），AI 摘要基于导语生成——摘要可能带偏
- 尝试过的三个替代方案都不做：
  - "summary_only 且 <500 字 且 无 rss_summary → 跳 AI"——会误伤真短消息
  - 站点专属规则（`if host=='kedglobal.com' and len<500: paywalled`）——维护成本高
  - Playwright 渲染 JS → 太重的依赖
- 现状可接受：这类偏差在 `summary_only` 计数里显式可见，读者能看到"归档日期未核实"和低分标记，警觉性够

### `keyword_counts()` 是 O(N) SQL
- 每个关键词一次 `LIKE '%|X|%'` 全表扫描；20 个词 → 20 次扫描
- 当前 28 篇没问题；几千篇后会明显变慢
- **正确解法**（不现在做）：建 `article_keywords(article_id, keyword)` 桥接表，加 index，计数变 index scan
- 现在的实现优先了实现简单，代价可控

### 破坏性回退的坑（已修但记住）
- JSON-LD `publisher` 的 `@id` 字段是 Organization URL 标识符，不是名字——`_clean()` 已拿掉 `@id` 兜底，`_clean_name()` 额外拒 URL 形状字符串
- AI 从不写 `author`——写库路径只有 `article_parser`。别再猜"作者是不是 AI 生成的"，是**网页 JSON-LD 或 meta 标签给的**

## 当前进度和下一步

### 已完成
- 5 大轮改造：FTS5 索引层 / Flask 检索页 / 详情页+聚合 / 4-5 分翻译 / run.py 瘦身 + 部署配置
- 详情页：低分不展开、双语切换、ⓘ 日期溯源、规则回退明示、SVG 图标
- 关键修复：DeepSeek 400、破坏性 `--no-ai` 守卫、JSON-LD publisher URL 泄漏、trust_rss_date 白名单、author_status 三值区分（named / institutional / unknown）
- **第 5B 轮 服务器可达信源改造**（`b5f4400`）：Bing 4 源（HBM / CapEx / CoWoS / people）+ TrendForce Insights + SemiWiki；Google 4 源标 `requires_proxy: true`，`fetchers.py` 遇到不通自动跳过并 `[requires_proxy]` 日志；`article_parser` 支持 Bing wrapper 解包；publisher 黑名单 10 域（seekingalpha / invezz / msn 等散户资讯站）
- Deploy 配置：`deploy/gunicorn.service` + `deploy/ai-tech-intel.service+.timer` + `deploy/nginx.conf`，全用 `<SYSTEM_USER>` / `<APP_PATH>` / `<DOMAIN>` 占位符
- **第 6 轮 UI 改造**（纯模板层，未动 `app.py` / `src/`）：抽出 `base.html`；检索页表格改卡片流；新增当前条件回显、密度切换、`/` 聚焦、排序/密度 sticky 工具条；详情页新增阅读进度条、滚动吸顶标题、中英切换改前端即时切换（保留链接兜底）、正文字号调节、复制摘要/链接。旧模板留在 `templates/*.html.orig` 作对照，确认后可删

### UI 层已定决策
- **不建 `static/` 目录**：CSS/JS 继续内联在模板里。理由是 nginx 现在把 `/` 全量 proxy 给 gunicorn，加静态目录就要同步改 nginx location + 部署手册，收益不值
- **卡片流取代表格**：旧 5 列表格在窄屏要 `min-width:850px` 横向滚动；卡片把评分放左轨、标题摘要走阅读列、信源/产业链/公司压到脚注条
- **所有 JS 都是渐进增强**：密度、字号、中英切换、复制在 JS 关掉时页面依然可读可用（中英切换保留 `?view=` 链接兜底）
- **动效统一走 `--dur` / `--ease` 两个变量**，并有 `prefers-reduced-motion` 兜底
- **改完模板必须重启 Flask**：`app.py` 默认 `debug=False`，Jinja 的 `auto_reload` 跟着是 False，改磁盘上的模板不重启进程不会生效。服务器上同理——换 `news.db` 不用重启 gunicorn，改模板必须重启
- **UI 验收优先用 Chrome**：Safari 的「网站 → 页面缩放」若不是 100%，会触发 WebKit 写死的 9px 最小逻辑字号（50% 缩放下所有小于 18px 的字号被顶平），导致排版层次全部失真，且这个下限关不掉

### 正在推进：服务器首次上线部署
按 `deploy.md` 一次性清单执行：
- Mac 端：SSH 别名 + Keychain（私钥 `~/.ssh/seventharea_ed25519` 已配）、`deploy/mac-daily.sh` 里填 `SERVER=seventharea` + `REMOTE_DATA=<APP_PATH>/data`、装载 `com.viceng.aitechintel.plist`
- 服务器端：**只装** `ai-tech-intel-web.service`（gunicorn），配 `nginx.conf` + Basic Auth（`/etc/nginx/.htpasswd-ai-tech-intel`）
- 抓取/AI/翻译全部在 Mac 上跑，服务器 `data/news.db` 由 `mac-daily.sh` scp 快照 + 原子 `mv` 更新
- 阻塞项：等项目负责人给出正式 `<APP_PATH_ON_SERVER>` 目录 + 安全组白名单

### 后续
- 观察 1-2 周 launchd 稳定性 + Bing 各源命中率 + DeepSeek 月账单
- 视命中情况决定是否给 bing_people 拆成三个人物源（Bing OR 语法有限，pipe OR 返回结果偏旧，b5f4400 干跑统计里 24h/168h 命中都是 0）
- 若 Bing/直连源长期偏低，回头评估给服务器加代理让 Google 4 源也能跑（现在 `requires_proxy: true` 会自动跳过）

## Mac 端双击启动器

项目根目录有两个 `.command` 文件，Finder 双击即可运行（仅 Mac 用，路径写死了本机
Python，服务器上不用）：
- `抓取今日新闻.command` —— 备份 DB + `run.py --hours 24` + 等回车。装 launchd
  后（`deploy/com.viceng.aitechintel.plist`）日常抓取会自动跑，这个手动启动器
  只做补跑或调试
- `打开网站.command` —— 起本地 Flask + 打开浏览器看检索页

## 常用命令速查

```bash
# 本地开发
python app.py                            # 启动 Flask
python run.py --hours 24                 # 触发一次抓取

# 关键词
python run.py --list-keywords
python run.py --add-keyword X            # 会自动 reindex
python run.py --remove-keyword X

# 安全预览（不写库）
python run.py --reprocess-ai --dry-run --reprocess-limit 5
python run.py --translate --dry-run --translate-limit 5
python run.py --refetch-content --dry-run --refetch-limit 5

# 危险命令（会覆盖已有 AI 结果，需要 --force）
python run.py --reprocess-ai --no-ai --force --reprocess-only-fallback   # 只对已 fallback 的行重跑规则
python run.py --translate --translate-id 17                              # 强制重翻某篇（用于改完 prompt 后测试）

# 手动重建
python -m src.storage reindex            # 重算 matched_keywords + 重建 FTS
python -m src.storage promote-rss        # 白名单源 RSS 日期升级为 rss.trusted

# 备份
cp data/news.db data/news.db.bak-$(date +%Y%m%d)   # 大动作前手动备份
```

## 交接约定

- 每完成一轮重要改动 → 更新这个文件的「当前进度和下一步」段
- 决策改了或红线新增 → 同步更新对应段
- 别的段（架构、命令）如结构变化也要跟
