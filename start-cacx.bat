@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo   cacx  -  double click to start
echo   Chinese: cacx  Web UI  (port 8770)
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] python not found in PATH.
    echo   Please install Python 3.10+ and tick "Add Python to PATH".
    echo   https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

rem ---- version check: "python exists" is NOT "python is new enough" ----
rem Found python, but never checked its version -> a 3.8/3.9 install sails
rem past the check above, then pip install fails on mcp>=2.3 (needs 3.10+),
rem and the error we print is "install failed, try running the same command
rem manually" -- which fails the same way. A dead end pointing the wrong way.
rem Check it here, where the diagnosis is still cheap and actionable.
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python 3.10 or newer is required ^(this one is older^).
    echo   pyproject.toml also declares requires-python ^>=3.10^.
    echo   Fix: install Python 3.10+ from https://www.python.org/downloads/
    echo   and tick "Add Python to PATH", then run this file again.
    echo.
    pause
    exit /b 1
)

rem ---- install deps on first run ----
rem The graph page also needs pyvis (inlines vis-network) / networkx (analysis)
rem / jieba (tokenizer + org mining). Checking only fastapi+uvicorn would skip
rem the install, then the graph page fails to load its vis assets.
rem KEEP THIS COMMENT IN ASCII: see the note in the Chinese-named wrapper.
rem Measured on this machine, a UTF-8 Chinese comment line inside a LF-only
rem .bat gets executed as a command by cmd.exe.
python -c "import fastapi, uvicorn, requests, bs4, pyvis, jieba, networkx" >nul 2>nul
if errorlevel 1 (
    rem DO NOT put a bare "(" or ")" in an echo line inside an if-block.
    rem cmd.exe must scan the whole block to find its matching ")", and an
    rem unescaped bracket breaks that scan: it aborts with
    rem   "xxx was unexpected at this time." -- even when the condition is
    rem FALSE and the branch never runs. Escape them as ^( ^) instead.
    echo [1/2] First run - installing dependencies ^(incl. graph libs pyvis/networkx/jieba^), one time only.
    echo.
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [ERROR] install failed. Try manually:
        echo   python -m pip install -r requirements.txt
        echo.
        pause
        exit /b 1
    )
    echo.
    echo [2/2] Dependencies ready.
    echo.
)

rem ---- is the database there? ----
rem Database.__init__ does mkdir + sqlite3.connect, and sqlite3.connect
rem CREATES an empty file when it is missing. So a pack that failed to
rem extract (or an extraction that skipped data/) starts up perfectly
rem happily and shows an EMPTY tool -- no error, no explanation, just
rem "0 articles" everywhere. That is the single most likely first-run
rem failure for a 429 MB zip, and the old launcher never mentioned it.
rem Check it here, where a human is still reading.
if not exist "data\cacx.db" (
    echo [ERROR] data\cacx.db is missing.
    echo   The pack ships the database, so this almost always means the
    echo   zip was not extracted completely, or data\ was skipped.
    echo.
    echo   Fix: re-extract the whole zip to a short path, e.g. D:\cacx
    echo         Then run this file again.
    echo   If you deliberately want an empty library and will run
    echo   "python -m cacx sync" yourself, press Y to continue anyway.
    echo.
    choice /c YN /n /m "Continue without the database? (Y/N) "
    if errorlevel 2 (
        pause
        exit /b 1
    )
    echo.
    echo   Continuing. The site will open but contain NO articles.
    echo.
)

rem ---- port already in use? ----
set OLD_PID=
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8770" ^| findstr "LISTENING"') do set OLD_PID=%%p
if defined OLD_PID (
    echo Port 8770 is already in use by PID %OLD_PID%.
    echo.
    choice /c YN /n /m "Stop it and restart? (Y/N) "
    if errorlevel 2 (
        echo Opening browser for the running service.
        start "" "http://127.0.0.1:8770/"
        timeout /t 3 >nul
        exit /b 0
    )
    taskkill /f /pid %OLD_PID% >nul 2>nul
    timeout /t 2 >nul
)

echo Starting server. Browser will open automatically.
echo.
echo   Web UI  : http://127.0.0.1:8770/
echo   API docs: http://127.0.0.1:8770/docs
echo   Stop    : close this window (or press Ctrl+C)
echo.
rem Do NOT put the Chinese folder/file name here. This whole file must
rem stay pure ASCII -- see the note in the Chinese-named wrapper:
rem cmd.exe decodes .bat with the OEM codepage, a UTF-8 Chinese line
rem gets chopped and "python -m cacx serve" never runs. Point at the
rem docs/ path only.
echo   Startup guide: docs\STARTUP-GUIDE-zh.md
echo   Classmate FAQ: README.md
echo.
echo ============================================================
echo.

python -m cacx serve

echo.
echo Server stopped.
pause