#!/bin/bash
cd "$(dirname "$0")"
open http://127.0.0.1:5000
/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14 app.py
