@echo off
setlocal
title Forager - Order Editor
cd /d "%~dp0"

set "PY="
if exist "C:\Program Files\Python\python.exe" set "PY=C:\Program Files\Python\python.exe"
if not defined PY if exist "%LocalAppData%\Programs\Python\python.exe" set "PY=%LocalAppData%\Programs\Python\python.exe"
if not defined PY set "PY=python"

echo Interpreter: %PY%
echo Starting order editor, browser will open automatically.
echo This page is only for adjusting item order/groups, no recognition.

"%PY%" -c "import mss,cv2,numpy,pynput" >nul 2>&1
if errorlevel 1 (
  echo.
  echo [ERROR] Missing dependency. Run:
  echo   "%PY%" -m pip install mss opencv-python numpy pynput
  echo.
  pause
  exit /b 1
)

"%PY%" src\web_serve.py
echo.
echo Server stopped.
pause
exit /b 0
