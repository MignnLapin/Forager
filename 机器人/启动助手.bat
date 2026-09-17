@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
py -3 -B -X utf8 src\hover_recognize.py
pause
