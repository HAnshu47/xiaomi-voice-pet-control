@echo off
REM Home Assistant Core 在 Windows 上原生启动
REM 把 C:\Python312 换成你自己的 Python 安装路径
REM --ignore-os-check 是必需的，HA 官方不支持 Windows
start /min "" "C:\Python312\Scripts\hass.exe" --config "D:\homeassistant" --ignore-os-check
