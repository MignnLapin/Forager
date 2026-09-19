@echo off
chcp 936 >nul
cd /d "%~dp0"
setlocal
echo 正在安装依赖...
pip install --no-index --find-links=src\libs -r requirements.txt
if errorlevel 1 goto :fail
echo 正在解压 OCR 引擎...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -Path 'src\libs\tesseract-ocr.zip' -DestinationPath 'src\ocr' -Force"
if errorlevel 1 goto :fail
echo 安装成功，正在清理安装文件...
rmdir /s /q src\libs
del requirements.txt
del "%~f0"
echo 清理完成！
exit /b 0
:fail
echo 安装失败，保留安装文件以便重试。
pause
exit /b 1