@echo off
rem 双击启动语音桥接。需要与本文件同级存在 voice_bridge.py 和 venv-voice\ 目录
title voice_bridge
cd /d %~dp0
venv-voice\Scripts\python.exe voice_bridge.py --run
pause
