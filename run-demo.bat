@echo off
REM ============================================================
REM  cacx - full-auto 5 minute demo launcher
REM  NOTE: this file is ASCII-only ON PURPOSE.
REM        cmd.exe decodes .bat using the OEM codepage (GBK here);
REM        any non-ASCII byte in the body turns into mojibake and
REM        can break the script. Keep it English.
REM ============================================================
setlocal
cd /d "%~dp0"

chcp 65001 >nul
set PYTHONIOENCODING=utf-8

where py >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python launcher not found. Install Python 3.10+ and tick "Add Python to PATH".
    pause
    exit /b 1
)

REM ---- is the server already up? -------------------------------------
curl -s -m 3 "http://127.0.0.1:8770/api/health" >nul 2>nul
if errorlevel 1 (
    echo [INFO] Server not running - starting it now, please wait about 30 seconds...
    start "" py -3 -m cacx serve --no-browser
    set /a tries=0
    :waitloop
    set /a tries+=1
    timeout /t 3 /nobreak >nul
    curl -s -m 3 "http://127.0.0.1:8770/api/health" >nul 2>nul
    if not errorlevel 1 goto :ready
    if %tries% lss 20 goto :waitloop
    echo [ERROR] Server did not come up. Open a terminal and run: py -3 -m cacx serve
    pause
    exit /b 2
)

:ready
echo [INFO] Server is up. Starting the auto demo...
echo [INFO] The browser window will open on screen. Do NOT minimize it.
echo.
py -3 autodemo.py %*
set RC=%ERRORLEVEL%

echo.
if %RC%==0 (
    echo [OK] Demo finished. Screenshots are in: （测试）\演示产物\截图
) else (
    echo [FAIL] Demo stopped early, exit code %RC%. Read the message above.
)
echo.
pause
exit /b %RC%