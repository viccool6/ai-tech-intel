#!/bin/bash
cd "$(dirname "$0")"
cp data/news.db "data/news.db.bak-$(date +%Y%m%d)" 2>/dev/null
/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14 run.py --hours 24
echo ""
echo "===== 抓取完成，按回车关闭 ====="
read
