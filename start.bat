@echo off
chcp 65001 >nul
cd /d "%~dp0"
start "" /min cmd /c "timeout /t 2 >nul & start http://127.0.0.1:5000"
python server.py
pause
