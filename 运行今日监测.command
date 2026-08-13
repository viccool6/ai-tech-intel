#!/bin/zsh
# macOS-only launcher — double-click in Finder to run today's ingestion
# locally. NOT for the Linux server: the server uses systemd
# (deploy/ai-tech-intel.timer + .service). Do not copy this file into a
# server deployment.
#
# Note: the trailing `open "档案库/index.html"` below is legacy — after
# html_generator was removed (round 5A) that file is a stale historical
# snapshot and no longer regenerates. The live UI is Flask
# (`python app.py` → http://127.0.0.1:5000/).
cd "$(dirname "$0")"
if [ ! -d ".venv" ]; then
  echo "请先按README建立.venv并安装requirements.txt"
  read -n 1
  exit 1
fi
source .venv/bin/activate
python3 run.py --hours 24 --max-items 3
open "档案库/index.html"
echo ""
echo "运行结束，索引已打开。按任意键关闭窗口。"
read -n 1
