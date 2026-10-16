@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo   cacx  -  double click to start
echo   Chinese: cacx  Web UI  (port 8770)
echo ============================================================
echo.

rem ---- pick a usable Python ----
rem The floor is 3.8, and that number is measured, not guessed:
rem   _pyfloor_probe.py  -> the shipped code has no 3.9/3.10-only construct
rem                         (no match statement, no runtime | annotations,
rem                         no removeprefix and friends)
rem   _resolve_floor.py  -> pip really resolves requirements.txt on 3.8
rem                         (dry-run with --python-version, all four pass)
rem The only package that ever demanded 3.10 was mcp, and it now lives in
rem requirements-mcp.txt as an OPTIONAL extra. Nothing about the website
rem needs 3.10, so nothing about the website should demand it.
rem
rem Two DIFFERENT mistakes hide behind "python not found":
rem   1. there is no python at all
rem   2. python exists but is 3.6 / 3.7
rem The old launcher only asked `where python`, so case 2 slipped
rem through, `pip install` then died, and the message we printed was
rem "install failed, try the same command manually" -- which fails
rem identically. A dead end pointing the wrong way.
rem
rem `python` on PATH is also not the only entry point:
rem   - the Windows `py` launcher knows about interpreters that are
rem     NOT on PATH, which is the commonest "but I already installed
rem     Python" case;
rem   - the `python` that `where` finds may be the Microsoft Store
rem     placeholder stub in ...\AppData\Local\Microsoft\WindowsApps\,
rem     which is not an interpreter at all.
rem So: probe several entry points, keep the first one >= 3.8.
rem Candidate order puts `py -3` FIRST on purpose -- when the Store
rem stub owns the `python` name, only py can still find a real one.
set "PY="
set "PYDESC="
set "PYSEEN="

call :probe "py -3"
if not defined PY call :probe "python"
if not defined PY call :probe "python3"
if not defined PY call :probe "py -3.13"
if not defined PY call :probe "py -3.12"
if not defined PY call :probe "py -3.11"
if not defined PY call :probe "py -3.10"
if not defined PY call :probe "py -3.9"
if not defined PY call :probe "py -3.8"

if defined PY goto :python_ok
goto :python_bad

:python_bad
echo [ERROR] Python 3.8 or newer is required, and no usable one was found.
echo.
echo   Looked for : py -3, python, python3, py -3.13 ... py -3.8
if defined PYSEEN echo   Found    : %PYSEEN%
if not defined PYSEEN echo   Found    : nothing runnable. Either no Python is installed,
if not defined PYSEEN echo               or the only "python" on PATH is the Microsoft Store
if not defined PYSEEN echo               placeholder under AppData\Local\Microsoft\WindowsApps.
echo.
echo   What to do:
echo     1. List every Python already on this PC:   py -0
echo     2. If a 3.8+ shows up there but was not picked up above, close this
echo        window and open a NEW one. PATH is read when the window starts.
echo     3. If there is no usable Python at all, install 3.12 from
echo        https://www.python.org/downloads/
echo        and tick "Add python.exe to PATH" during setup.
echo.
echo   3.8 and 3.9 run everything except the optional MCP server.
pause
exit /b 1

:python_ok
echo Using %PYDESC%
echo.

rem ---- install deps on first run ----
rem The graph page also needs pyvis (inlines vis-network) / networkx (analysis)
rem / jieba (tokenizer + org mining). Checking only fastapi+uvicorn would skip
rem the install, then the graph page fails to load its vis assets.
rem KEEP THIS COMMENT IN ASCII: see the note in the Chinese-named wrapper.
rem Measured on this machine, a UTF-8 Chinese comment line inside a LF-only
rem .bat gets executed as a command by cmd.exe.
%PY% -c "import fastapi, uvicorn, requests, bs4, pyvis, jieba, networkx" >nul 2>nul
if errorlevel 1 (
    rem DO NOT put a bare "(" or ")" in an echo line inside an if-block.
    rem cmd.exe must scan the whole block to find its matching ")", and an
    rem unescaped bracket breaks that scan: it aborts with
    rem   "xxx was unexpected at this time." -- even when the condition is
    rem FALSE and the branch never runs. Escape them as ^( ^) instead.
    echo [1/3] First run - installing dependencies ^(incl. graph libs pyvis/networkx/jieba^), one time only.
    echo.
    %PY% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [ERROR] install failed. Try manually:
        echo   %PY% -m pip install -r requirements.txt
        echo.
        pause
        exit /b 1
    )
    echo.
    echo [2/3] Dependencies ready.
    echo.
)

rem ---- ask the interpreter, not pip, whether we are actually ready ----
rem "pip exited 0" is NOT "the app can start": a wheel can install cleanly
rem and still fail to import on this platform, and a half-installed package
rem can leave pip reporting success. The only trustworthy check is the same
rem import the app will do. Printing the real ImportError here is the whole
rem point -- a generic "install failed" sends the reader to the wrong place.
%PY% -c "import fastapi, uvicorn, requests, bs4, pyvis, jieba, networkx" >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Dependencies are installed but not importable. Real reason:
    echo.
    %PY% -c "import fastapi, uvicorn, requests, bs4, pyvis, jieba, networkx"
    echo.
    echo   If that says "No module named ..." then reinstall with:
    echo     %PY% -m pip install --force-reinstall -r requirements.txt
    echo   On Python 3.8 / 3.9 some packages ship no prebuilt wheel for
    echo   Windows, so pip has to build them. That is usually fine, but if
    echo   it keeps failing, Python 3.12 is the smooth path:
    echo     https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

rem ---- MCP is optional, so its absence is information, not an error ----
rem mcp is the ONLY package that needs Python 3.10+. Keeping it out of
rem requirements.txt is what lets a 3.8/3.9 machine install at all.
rem The check below must therefore never abort the launcher: it reports
rem what is missing and what it costs, then gets out of the way.
%PY% -c "import mcp" >nul 2>nul
if errorlevel 1 (
    echo [3/3] Optional MCP server: NOT installed. Everything else works.
    echo        MCP is the only feature that needs Python 3.10+, so on 3.8
    echo        or 3.9 this is expected, not a problem. To enable it later:
    echo          %PY% -m pip install -r requirements-mcp.txt
    echo.
) else (
    echo [3/3] Optional MCP server: available -- %PY% -m cacx.mcp_server
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
    echo   "%PY% -m cacx sync" yourself, press Y to continue anyway.
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
rem gets chopped and "%PY% -m cacx serve" never runs. Point at the
rem docs/ path only.
echo   Startup guide: docs\STARTUP-GUIDE-zh.md
echo   Classmate FAQ: README.md
echo.
echo ============================================================
echo.

%PY% -m cacx serve

echo.
echo Server stopped.
pause
exit /b 0

rem ---- probe one candidate interpreter ----
rem %1 = a command prefix, e.g. "python" or "py -3.12".
rem Sets PY / PYDESC on success. Always records what it saw into
rem PYSEEN, so the failure message can report facts instead of
rem just saying "your python is too old" -- an error that does not
rem tell the reader what was actually found leaves the next step
rem (and the screenshot they send back) useless.
:probe
set "CAND=%~1"
set "FMJ="
set "FMN="
set "FEXE="
rem '#' as the separator, and `delims=` must say so explicitly:
rem `tokens=` only says WHICH tokens to hand to %%a/%%b/... -- it does
rem NOT define where tokens are cut. I first wrote `tokens=1-3` with no
rem `delims=`, so the whole line "PV#3#13#C:\...\python.exe" arrived as
rem ONE token (it contains no spaces), %%b/%%c stayed empty, and the very
rem next line -- `if %FMJ% GTR 3 set "FOK=1"` -- became
rem `if PV#3#13#C:\... GTR 3 ...`, which cmd rejects with
rem "8 was unexpected at this time." The launcher died before printing
rem anything. Batch syntax is not something to eyeball; it gets run.
for /f "usebackq tokens=1-4 delims=#" %%a in (`%CAND% -c "import sys;print('PV',sys.version_info[0],sys.version_info[1],sys.executable,sep='#')" 2^>nul`) do (
    set "FMJ=%%b"
    set "FMN=%%c"
    set "FEXE=%%d"
)
if not defined FMJ goto :eof
set "FVER=%FMJ%.%FMN%"
if not defined PYSEEN goto :seen_first
set "PYSEEN=%PYSEEN%, %CAND% %FVER%"
goto :seen_done
:seen_first
set "PYSEEN=%CAND% %FVER% at %FEXE%"
:seen_done
rem 3.8 is the floor. Compare minor numerically ONLY for major 3 --
rem a plain string compare would reject "3.10" as smaller than "3.8".
set "FOK=0"
if %FMJ% GTR 3 set "FOK=1"
if %FMJ% EQU 3 if %FMN% GEQ 8 set "FOK=1"
if "%FOK%"=="0" goto :eof
if defined PY goto :eof
set "PY=%CAND%"
rem NEVER put "->" or any ">" "<" "|" "&" in here. These variables get
rem echoed by the caller, and cmd.exe parses redirection BEFORE echo runs:
rem   echo Using py -3 3.13 -> C:\...\python.exe
rem redirects stdout INTO that path -- it would TRUNCATE the interpreter.
rem Use the word "at" as the separator. Plain spaces only.
set "PYDESC=%CAND% %FVER% at %FEXE%"
goto :eof
