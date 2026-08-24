#!/bin/zsh
# 每日跑一次：抓取 → AI 摘要 → 翻译 → VACUUM 快照 → scp → 原子替换
#
# launchd 每天定时触发这个脚本（见 deploy/com.viceng.aitechintel.plist）。
# 服务器上 gunicorn 保持运行；scp 完之后一条 mv 命令原子替换 news.db，
# 已开的 gunicorn 请求继续用旧 inode，新请求打开新文件——无需重启服务。
#
# 用前：
#   - 确认 ~/.ssh/config 里已配 Host seventharea（见 deploy.md §1.1）
#   - 把 REMOTE_DATA 里的 <APP_PATH_ON_SERVER> 换成项目负责人指定的正式目录
#   - key 已用 ssh-add --apple-use-keychain 加进 macOS Keychain，
#     launchd 才能免密登录（不装 Keychain 会因找不到 key 失败）

set -euo pipefail

APP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_ROOT"

LOG="data/logs/mac-daily.log"
mkdir -p data/logs

# 一切输出都追加到日志（launchd 也会截获，但本地留一份便于 tail）
exec >>"$LOG" 2>&1
echo ""
echo "===== $(date '+%F %T') ====="

# --- 1. 抓取 + AI + 翻译（DeepSeek 平峰时段跑，成本按场景 B ¥1/次内） ---
echo "[step 1/4] run.py --hours 14"
./.venv/bin/python run.py --hours 14 || {
  echo "!! run.py 失败，abort（DB 未变化，下一轮再试）"
  exit 1
}

# --- 2. VACUUM INTO 生成干净快照 ---
# VACUUM INTO 产出的文件是 journal_mode=DELETE（无 WAL/SHM 副件），
# 便于原子 mv。原库仍是 WAL 模式，不受影响。
echo "[step 2/4] VACUUM INTO snapshot"
rm -f data/news.db.snapshot
sqlite3 data/news.db "VACUUM INTO 'data/news.db.snapshot'"
snap_size=$(stat -f%z data/news.db.snapshot)
echo "    snapshot 大小: $snap_size bytes"

# --- 3. scp 到服务器（先落到 .new，防止半传输被 Flask 读到）---
SERVER="seventharea"
REMOTE_DATA="/home/jiantouhuagongshixi/ai-tech-intel/data"

echo "[step 3/4] scp → $SERVER:$REMOTE_DATA/news.db.new"
scp -q data/news.db.snapshot "$SERVER:$REMOTE_DATA/news.db.new"

# --- 4. 服务器上原子替换 + 清 WAL 副件 ---
# mv 在同一文件系统内是原子操作。gunicorn 已开的请求继续用旧 inode；
# 新请求打开新 inode 立即看到新数据。清理旧 WAL/SHM 副件避免 SQLite
# 混用（快照本身没有 WAL 副件，但服务器上可能残留上一轮的）。
echo "[step 4/4] atomic mv + 清 WAL 副件"
ssh -q "$SERVER" "mv $REMOTE_DATA/news.db.new $REMOTE_DATA/news.db && rm -f $REMOTE_DATA/news.db-wal $REMOTE_DATA/news.db-shm"

echo "SUCCESS $(date '+%F %T')"
