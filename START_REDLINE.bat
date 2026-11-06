@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ==========================================
echo          REDLINE - AUTO PORTAL
echo ==========================================
echo.
where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python 3.10+ and try again.
  pause
  exit /b 1
)
echo Starting the REDLINE server...
echo After startup, open: http://127.0.0.1:8000/listings.html
echo Keep this window open while using the site.
echo Stop the server with Ctrl+C.
echo.
python server.py
pause
