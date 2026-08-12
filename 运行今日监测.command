#!/bin/zsh
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
