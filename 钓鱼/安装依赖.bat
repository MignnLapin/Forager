@echo off
chcp 65001 >nul
echo 正在安装钓鱼项目的依赖包...
pip install --no-index --find-links=src\libs -r requirements.txt

if %errorlevel% equ 0 (
    echo.
    echo 依赖安装成功！正在清理安装文件...
    rmdir /s /q src\libs
    del requirements.txt
    del "%~f0"
    echo 清理完成！
) else (
    echo.
    echo 依赖安装失败，保留安装文件以便重试。
)
pause
