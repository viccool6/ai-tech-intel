#!/bin/bash
# macOS 双击启动器 —— 抓一次今日新闻。备份 DB + run.py --hours 24 + 等回车。
#
# ⚠ 本文件仅供 Mac 本机开发/手动跑使用，不要复制到服务器（服务器只跑 Flask
#   展示，抓取由 Mac 上的 launchd + deploy/mac-daily.sh 完成）。
#
# ⚠ 下面写死的是本机 Python 路径：
#     /Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14
#   换机器/换 Python 版本时，用 `which python3.14` 或 `readlink $(which python3)`
#   查一下本机路径再改。
#
# 建议：等 launchd（deploy/com.viceng.aitechintel.plist）装好后，日常抓取
# 会在 03:15 自动跑，这个手动启动器主要用于补跑或调试。
cd "$(dirname "$0")"
cp data/news.db "data/news.db.bak-$(date +%Y%m%d)" 2>/dev/null
/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14 run.py --hours 24
echo ""
echo "===== 抓取完成，按回车关闭 ====="
read
