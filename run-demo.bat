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
REM BUG FIXED 2026-10-13. The loop used to live inside an if (...) block:
REM     if %tries% lss 20 goto :waitloop
REM cmd expands %tries% when it PARSES the block, not when it runs the line,
REM so it was always empty and every launch died with
REM     "20 was unexpected at this time."
REM The block is gone entirely -- flat labels only, so there is nothing left
REM to expand early. Do not put a loop counter back inside parentheses here.
:check_up
curl -s -m 3 "http://127.0.0.1:8770/api/health" >nul 2>nul
if not errorlevel 1 goto :ready

echo [INFO] Server not running - starting it now, please wait about 30 seconds...
start "" py -3 -m cacx serve --no-browser
set /a tries=0

:waitloop
set /a tries+=1
timeout /t 3 /nobreak >nul
curl -s -m 3 "http://127.0.0.1:8770/api/health" >nul 2>nul
if not errorlevel 1 goto :ready
echo [INFO] still waiting for the server... %tries%/20
if %tries% lss 20 goto :waitloop

echo [ERROR] Server did not come up. Open a terminal and run: py -3 -m cacx serve
pause
exit /b 2

:ready
echo [INFO] Server is up. Starting the auto demo...
echo [INFO] The browser window will open on screen. Do NOT minimize it.
echo.
REM -u = unbuffered stdout. 2026-10-13: without it, a crash lost every
    REM buffered log line -- the output stopped dead after "Server is up"
    REM with an empty stderr, which looks exactly like "it just didn't run".
    REM Keep it. Diagnostics are worth losing a little speed.
py -3 -u autodemo.py %*
set RC=%ERRORLEVEL%

echo.
if %RC%==0 (
    REM Keep this line ASCII. The artifact folder name contains CJK
    REM characters; printing it here put non-ASCII bytes in the body and
    REM desynced cmd's parser, killing the script before it ever ran.
    REM autodemo.py prints the real (UTF-8 capable) path on stdout.
    echo [OK] Demo finished. Screenshots folder path is printed above.
) else (
    echo [FAIL] Demo stopped early, exit code %RC%. Read the message above.
)
echo.
pause
exit /b %RC%