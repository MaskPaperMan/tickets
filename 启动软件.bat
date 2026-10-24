@echo off
chcp 65001 >nul
cd /d "%~dp0"
python app.py
if errorlevel 1 (
  echo 启动失败，请检查是否安装 Python 3.10+ 并加入 PATH。
  pause
)
