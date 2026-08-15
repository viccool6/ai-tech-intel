#!/bin/bash
# macOS 双击启动器 —— 起本地 Flask + 打开浏览器看检索页。
#
# ⚠ 本文件仅供 Mac 本机开发/预览使用，不要复制到服务器（服务器上 Flask
#   由 systemd + gunicorn 常驻，见 deploy/gunicorn.service）。
#
# ⚠ 下面写死的是本机 Python 路径：
#     /Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14
#   换机器/换 Python 版本时，用 `which python3.14` 或 `readlink $(which python3)`
#   查一下本机路径再改。
#
# 注意：`open` 会先弹浏览器，然后 Flask 才启动。第一次打开可能弹一次连不上，
# 5 秒后手动刷新即可。要关闭 Flask 直接按 Ctrl+C。
cd "$(dirname "$0")"
open http://127.0.0.1:5000
/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14 app.py
