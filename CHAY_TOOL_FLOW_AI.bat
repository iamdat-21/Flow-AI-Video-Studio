@echo off
title FLOW AI VIDEO STUDIO - VEO 3.1 MULTI-ACCOUNT AND MULTI-IP (8S/SCENE)
cd /d "D:\Flow-AI-Video-Studio"
set PYTHONIOENCODING=utf-8
for /f "tokens=5" %%a in ('netstat -aon ^| findstr "127.0.0.1:8686" ^| findstr "LISTENING"') do taskkill /F /PID %%a >nul 2>&1
echo ============================================================================
echo   FLOW AI VIDEO STUDIO - TAO VIDEO TU DONG 8S/CANH (DA ACC - DA IP)
echo   Giao dien dieu khien Web: http://127.0.0.1:8686
echo ============================================================================
start "" "http://127.0.0.1:8686"
python app.py
pause
