@echo off
setlocal
title Forager - Recognition
cd /d "%~dp0"

set "PY="
if exist "C:\Program Files\Python\python.exe" set "PY=C:\Program Files\Python\python.exe"
if not defined PY if exist "%LocalAppData%\Programs\Python\python.exe" set "PY=%LocalAppData%\Programs\Python\python.exe"
if not defined PY set "PY=python"

echo Interpreter: %PY%
echo Starting recognition, no web server.
echo In game: Ctrl+Alt+D = start, A = stop, F = full auto.

"%PY%" -c "import mss,cv2,numpy,pynput" >nul 2>&1
if errorlevel 1 (
  echo.
  echo [ERROR] Missing dependency. Run:
  echo   "%PY%" -m pip install mss opencv-python numpy pynput
  echo.
  pause
  exit /b 1
)

"%PY%" src\web_serve.py --bare
echo.
echo Recognition stopped.
pause
exit /b 0
