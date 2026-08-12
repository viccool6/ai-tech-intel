
## V4.4新增：通用AI接口与历史重处理

配置好 `.env` 后，可直接重处理过去因未配置Key而进入fallback的文章：

```bash
python3 run.py --reprocess-ai --reprocess-only-fallback
```

先测试3篇：

```bash
python3 run.py --reprocess-ai --reprocess-only-fallback --reprocess-limit 3
```

AI失败时，网页将显示“原始摘要/规则回退”，不再将英文回退内容标成“AI中文摘要”。接口兼容OpenRouter、OpenAI、DeepSeek、硅基流动等OpenAI格式服务。

# AI科技产业情报系统 V3

## 功能

RSS / Google News → 正文提取 → URL与标题相似度去重 → AI中文标题、约200字摘要、重要性打分、标签和公司 → SQLite → HTML归档索引。

AI接口采用 OpenAI-compatible Chat Completions。默认配置为 OpenRouter，也可以换成其他兼容服务，只需要修改 `.env`：

```text
AI_BASE_URL=
AI_API_KEY=
AI_MODEL=
```

## 首次安装

```bash
cd ~/Downloads/AI-Tech-Intel-V3
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
open -e .env
```

在 `.env` 填写：

```text
AI_API_KEY=你的OpenRouter_Key
```

默认模型：

```text
AI_MODEL=openrouter/free
```

免费模型路由的模型会动态变化，适合测试和轻量使用，不适合保证生产级稳定性。

## 首次测试

不调用AI，先验证本地流程：

```bash
python3 tests/self_test.py
python3 run.py --source nvidia_blog --hours 168 --max-items 1 --no-ai
open 档案库/index.html
```

配置Key后重新测试AI摘要：

```bash
python3 run.py --reset --source nvidia_blog --hours 168 --max-items 1
open 档案库/index.html
```

## 日常运行

```bash
cd ~/Downloads/AI-Tech-Intel-V3
source .venv/bin/activate
python3 run.py --hours 24 --max-items 3
open 档案库/index.html
```

## 常用命令

```bash
python3 run.py --dry-run --source nvidia_blog --hours 168 --max-items 1
python3 run.py --no-ai --hours 24 --max-items 2
python3 run.py --rebuild-index
```

测试阶段清空：

```bash
python3 run.py --reset --source nvidia_blog --hours 168 --max-items 1
```

正式使用后不要随便加 `--reset`。

## 输出

- `档案库/index.html`：总索引
- `档案库/YYYY-MM-DD/*.html`：单篇归档
- `data/news.db`：SQLite数据库
- `data/logs/latest.log`：运行日志

## V3.5：本地搜索与关键词监控

首页现在支持以下检索：

- 全文搜索：中文标题、英文标题、中文摘要、公司、产业标签、信源
- 监控关键词：显示每个关键词累计命中文章数，点击关键词直接筛选
- 组合筛选：信源、最低重要性评分、最近1/7/30/90天
- 关键词标记：索引和单篇归档页显示命中的关键词

搜索完全在本机浏览器中完成，不会把数据库上传到任何服务器。

### 管理关键词（推荐方法）

查看当前关键词：

```bash
python3 run.py --list-keywords
```

新增关键词，例如“光模块”：

```bash
python3 run.py --add-keyword "光模块"
```

删除关键词：

```bash
python3 run.py --remove-keyword "光模块"
```

以上新增/删除命令会自动重建索引，无需重新抓新闻。

也可以直接编辑：

```bash
open -e config/watch_keywords.json
```

保存后运行：

```bash
python3 run.py --rebuild-index
open 档案库/index.html
```

### 搜索使用方法

1. 打开 `档案库/index.html`。
2. 在搜索框输入 `HBM`、`NVIDIA`、`先进封装` 等内容。
3. 可同时选择信源、评分和时间范围。
4. 点击某个监控关键词，再点一次即可取消该关键词筛选。
5. 搜索结果会即时更新，不需要重新运行 Python。

### 建议的投研关键词

可按研究方向维护，例如：

```text
HBM、HBM4、CoWoS、先进封装、CPO、光模块、液冷、GPU、ASIC、AI服务器、资本开支、NVIDIA、TSMC、SK Hynix、Micron、OpenAI、Anthropic
```

关键词只负责监控和筛选，不会直接改变AI重要性评分。若要改变评分权重，需要另外修改 `config/scoring_rules.json`。

## V4：详情页、原文链接与真实发布日期

首页标题可点击进入本地详情页，首页和详情页均可打开原始网页。系统将日期拆分为：

- 原文发布时间：仅在原网页 JSON-LD、Meta 或 `<time>` 中确认后显示；
- 原文更新时间；
- RSS 提供/收录日期；
- 本地抓取时间。

升级旧数据库后，运行以下命令重新访问历史文章并修复日期：

```bash
python3 run.py --repair-dates
```

只重新生成详情页和索引，不联网抓取：

```bash
python3 run.py --rebuild-index
```


## V4.2 日期规则

RSS只负责发现新闻。发布日期从原网页的文章级结构化数据、明确发布元数据，或标题附近带发布标签的可见日期中识别。无法可靠识别时页面不显示发布日期。RSS时间只用于内部排序和归档。历史数据可运行：

```bash
python3 run.py --repair-dates
```


## V4.4 补抓旧文章正文

```bash
python3 run.py --refetch-content --refetch-only-summary
```

该命令会重新解析 Google News 原文链接，补抓正文；正文成功后自动重做 AI 摘要和评分。
