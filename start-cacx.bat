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
rem Priority (deliberately the machine FIRST, the bundled runtime second):
rem   1. a Python on THIS machine, >= 3.10, and a working connection to the
rem      package index  -> use it, and install the dependencies into it
rem      (this is the long-standing behaviour, kept intact on purpose)
rem   2. otherwise the portable Python shipped in runtime\  -> nothing to
rem      install, nothing to configure, no network needed
rem   3. otherwise any local Python >= 3.8  (only reachable when runtime\
rem      is missing, which almost always means a partial extraction)
rem Rationale for putting 3.10 on the local branch: below 3.10 the optional
rem MCP server cannot be installed at all, so a machine that could run
rem everything else would be left as the only one missing a feature.
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
rem So: probe several entry points. Candidate order puts `py -3` FIRST on
rem purpose -- when the Store stub owns the `python` name, only py can
rem still find a real one.
set "PY="
set "PYDESC="
set "PYSEEN="
set "PYBUNDLED="
set "PYKIND="
set "PY310="
set "PY310D="
set "PY8="
set "PY8D="

rem ---- 1. what does this machine already have? ----
call :probe "py -3"
if not defined PY310 call :probe "python"
if not defined PY310 call :probe "python3"
if not defined PY310 call :probe "py -3.13"
if not defined PY310 call :probe "py -3.12"
if not defined PY310 call :probe "py -3.11"
if not defined PY310 call :probe "py -3.10"
if not defined PY8 call :probe "py -3.9"
if not defined PY8 call :probe "py -3.8"

rem ---- 2. does a good-enough local Python have a network to install with? ----
set "NETOK="
if defined PY310 call :net_ok "%PY310%"
if defined PY310 if not errorlevel 1 set "NETOK=1"

rem ---- 3. decide ----
if defined PY310 if defined NETOK goto :use_local310
if exist "runtime\python.exe" goto :use_bundled
if defined PY8 goto :use_local8
goto :python_bad

:use_local310
set "PY=%PY310%"
set "PYDESC=%PY310D%"
set "PYKIND=local"
goto :python_ok
:use_local8
set "PY=%PY8%"
set "PYDESC=%PY8D%"
set "PYKIND=local-old"
goto :python_ok
:use_bundled
rem Only reached when the machine has no >=3.10 Python, or it has one but
rem cannot reach the index. Why it still exists: that is exactly the
rem classroom-with-no-wifi case, and there the install is a guaranteed
rem failure while this folder is a guaranteed success.
call :probe_bundle
if not defined PY goto :python_bad
goto :python_ok

:python_bad
echo [ERROR] No usable Python, AND this package's bundled runtime is missing.
echo.
echo   Looked for : py -3, python, python3, py -3.13 ... py -3.8, runtime\python.exe
if defined PYSEEN echo   Found    : %PYSEEN%
if not defined PYSEEN echo   Found    : nothing runnable. Either no Python is installed,
if not defined PYSEEN echo               or the only "python" on PATH is the Microsoft Store
if not defined PYSEEN echo               placeholder under AppData\Local\Microsoft\WindowsApps.
if defined PY310 echo   Note      : found %PY310D%, but the package index was
if defined PY310 echo               unreachable, so its dependencies cannot be
if defined PY310 echo               installed. Plug in a network, or restore runtime\.
echo.
echo   This package ships its own Python in the runtime\ folder, so a machine
echo   with nothing installed should still work. That folder being absent means
echo   the zip was almost certainly not extracted completely.
echo.
echo   What to do:
echo     1. Re-extract the WHOLE zip to a short path, e.g. D:\cacx
echo        It is over 500 MB; a partial extract silently loses folders.
echo     2. Check that D:\cacx\runtime\python.exe exists.
echo     3. If runtime\ IS there but still not found, this copy is damaged --
echo        ask for a fresh one.
echo     4. Or install Python 3.10+ from https://www.python.org/downloads/
echo        and tick "Add python.exe to PATH", then run this again.
echo.
pause
exit /b 1
:python_ok
echo Using %PYDESC%
if "%PYKIND%"=="local" (
    echo   Machine Python ^(%PY310%^) with a working connection, so the
    echo   dependencies will be installed into it -- the usual behaviour.
) else if "%PYKIND%"=="local-old" (
    echo   Machine Python is older than 3.10 and the bundled runtime is
    echo   missing, so this is the only interpreter available.
) else (
    echo   Using the portable Python in runtime\: nothing to install,
    echo   nothing to configure, no network needed.
    echo   Optional MCP server, if you want it ^(needs network^):
    echo     %PY% -m pip install -r requirements-mcp.txt
)
echo.
rem ---- install deps on first run ----
rem The graph page also needs pyvis (inlines vis-network) / networkx (analysis)
rem / jieba (tokenizer + org mining) / scipy (networkx pagerank calls into
rem _pagerank_scipy UNCONDITIONALLY, and networkx does not declare scipy).
rem Checking only fastapi+uvicorn would skip the install, then the graph
rem page fails to load its vis assets; leaving scipy out of this list means
rem a machine missing ONLY scipy never triggers an install and instead
rem explodes minutes later, on the first PageRank run.
rem KEEP THIS COMMENT IN ASCII: see the note in the Chinese-named wrapper.
rem Measured on this machine, a UTF-8 Chinese comment line inside a LF-only
rem .bat gets executed as a command by cmd.exe.
%PY% -c "import fastapi, uvicorn, requests, bs4, pyvis, jieba, networkx, scipy" >nul 2>nul
if not errorlevel 1 goto :deps_ready

echo [1/3] First run - installing dependencies ^(incl. graph libs pyvis/networkx/jieba/scipy^), one time only.
echo.
%PY% -m pip install -r requirements.txt
if errorlevel 1 goto :install_failed
echo.
echo [2/3] Dependencies ready.
echo.
goto :deps_ready

:install_failed
echo.
echo [ERROR] install failed on this interpreter.
rem Preferring the machine's Python over the bundled one only makes sense
rem while the install can actually succeed. If it cannot, the bundled
rem runtime is the thing that still works -- so fall back to it rather
rem than stopping a machine that could have started fine.
if "%PYKIND%"=="bundled" goto :install_dead
if not exist "runtime\python.exe" goto :install_dead
echo     Falling back to the portable Python in runtime\ instead.
call :probe_bundle
if not defined PY goto :install_dead
set "PYKIND=bundled"
%PY% -c "import fastapi, uvicorn, requests, bs4, pyvis, jieba, networkx, scipy" >nul 2>nul
if not errorlevel 1 goto :deps_ready
:install_dead
echo.
echo   Manual retry:
echo     %PY% -m pip install -r requirements.txt
echo.
echo   If runtime\python.exe exists in this folder, delete the runtime\
echo   folder and re-extract the whole zip -- a partial extraction is the
echo   usual cause.
echo.
pause
exit /b 1

:deps_ready
rem ---- ask the interpreter, not pip, whether we are actually ready ----
rem "pip exited 0" is NOT "the app can start": a wheel can install cleanly
rem and still fail to import on this platform, and a half-installed package
rem can leave pip reporting success. The only trustworthy check is the same
rem import the app will do. Printing the real ImportError here is the whole
rem point -- a generic "install failed" sends the reader to the wrong place.
%PY% -c "import fastapi, uvicorn, requests, bs4, pyvis, jieba, networkx, scipy" >nul 2>nul
if not errorlevel 1 goto :deps_ok
echo [ERROR] Dependencies are installed but not importable. Real reason:
echo.
%PY% -c "import fastapi, uvicorn, requests, bs4, pyvis, jieba, networkx, scipy"
echo.
echo   If that says "No module named ..." then reinstall with:
echo     %PY% -m pip install --force-reinstall -r requirements.txt
echo   On older Python some packages ship no prebuilt wheel for Windows,
echo   so pip has to build them. That is usually fine, but if it keeps
echo   failing, Python 3.12 is the smooth path:
echo     https://www.python.org/downloads/
echo.
pause
exit /b 1
:deps_ok

rem ---- MCP is optional, so its absence is information, not an error ----
rem mcp is the ONLY package that needs Python 3.10+. Keeping it out of
rem requirements.txt is what lets an old interpreter install at all.
rem The check below must therefore never abort the launcher: it reports
rem what is missing and what it costs, then gets out of the way.
rem The reason it is missing depends on WHICH branch chose the
rem interpreter, so do not print one canned sentence for all three --
rem "expected on 3.8 or 3.9" is simply false when the machine has 3.12.
%PY% -c "import mcp" >nul 2>nul
if not errorlevel 1 goto :mcp_yes
if "%PYKIND%"=="bundled" goto :mcp_bundled
if "%PYKIND%"=="local" goto :mcp_local
goto :mcp_old
:mcp_yes
echo [3/3] Optional MCP server: available -- %PY% -m cacx.mcp_server
echo.
goto :mcp_done
:mcp_bundled
echo [3/3] Optional MCP server: NOT installed. Everything else works.
echo        Nothing to fix -- the bundled runtime deliberately ships
echo        without it. To enable it later ^(needs a network^):
echo          %PY% -m pip install -r requirements-mcp.txt
echo.
goto :mcp_done
:mcp_local
echo [3/3] Optional MCP server: NOT installed. Everything else works.
echo        This interpreter is new enough for it, but it is an optional
echo        extra and is never installed automatically. To enable it:
echo          %PY% -m pip install -r requirements-mcp.txt
echo.
goto :mcp_done
:mcp_old
echo [3/3] Optional MCP server: NOT installed. Everything else works.
echo        This interpreter is older than 3.10, which is the only thing
echo        MCP needs -- so on this machine it is expected, not a
echo        problem. Everything else in the site works normally.
echo.
:mcp_done

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
        echo.
        echo   cacx is ALREADY RUNNING as PID %OLD_PID%. Nothing was started
        echo   and nothing was stopped. The page above is the live site.
        echo.
        echo   Leave this window alone -- closing it does NOT stop that
        echo   service. To stop it for real, close the window that is
        echo   running it, or run:  taskkill /f /pid %OLD_PID%
        echo.
        rem No 'timeout' here on purpose: 'timeout' reads stdin
        rem and fails with "Input redirection is not supported" the moment
        rem this bat runs with stdin redirected -- which is how it gets run
        rem when a human pipes a keypress in. 'pause' already gives the
        rem browser time to come up AND holds the window, so the timeout was
        rem a second mechanism doing the same job with the worse failure.
        pause
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
rem This routine only COLLECTS; it no longer decides. It fills:
rem   PY310/PY310D  first candidate >= 3.10   (preferred machine branch)
rem   PY8/PY8D      first candidate >= 3.8    (last-resort branch)
rem   PYSEEN        everything it saw, so the failure message can report
rem                  facts instead of just "your python is too old" -- an
rem                  error that does not say what was actually found leaves
rem                  the next step (and the screenshot they send back) useless.
rem A >= 3.10 candidate also fills the 3.8 slot when that is still empty,
rem otherwise a machine with 3.12 + no network + no runtime\ would fall
rem through every branch and report "no usable Python" while 3.12 was
rem sitting right there on PATH.
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
rem Compare minor numerically ONLY for major 3 -- a plain string compare
rem would reject "3.10" as smaller than "3.8".
if %FMJ% GTR 3 goto :take310
if %FMJ% EQU 3 if %FMN% GEQ 10 goto :take310
goto :take8

:take310
if not defined PY310D set "PY310D=%CAND% %FVER% at %FEXE%"
set "PY310=%CAND%"
if defined PY8D goto :eof
set "PY8D=%CAND% %FVER% at %FEXE%"
set "PY8=%CAND%"
goto :eof

:take8
if defined PY8D goto :eof
set "PY8D=%CAND% %FVER% at %FEXE%"
set "PY8=%CAND%"
goto :eof

rem ---- is the package index reachable from THIS interpreter? ----
rem %1 = a command prefix.
rem Why not `ping`: ICMP is routinely blocked while HTTPS works fine, so a
rem ping-based check would push perfectly good machines onto the offline
rem branch. Why not just run pip and see: with no route to the network,
rem pip retries for minutes before giving up, and that is exactly the
rem classroom case where a fast answer matters.
rem Why urllib and not pip: this runs the same TLS and certificate path
rem the real install will use, so "reachable" means "reachable the way pip
rem will see it" rather than "some ICMP echo came back".
:net_ok
set "NCAND=%~1"
%NCAND% -c "import urllib.request as u; u.urlopen('https://pypi.org/simple/', timeout=6)" >nul 2>nul
goto :eof
rem ---- probe the bundled portable runtime ----
:probe_bundle
set "CAND=runtime\python.exe"
%CAND% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul
if errorlevel 1 goto :eof
set "PYBUNDLED=1"
set "PYKIND=bundled"
set "PY=%CAND%"
set "PYDESC=bundled portable runtime ^(%CAND%^)"
rem Belt-and-braces only -- read this before "simplifying" it away:
rem   I originally claimed the embeddable build ships no certificate root
rem   and that https fetching would therefore fail. **That was wrong**, and
rem   measuring it is why:
rem     - Windows embeddable Python, given no CA file, enumerates the
rem       WINDOWS root store (ssl -> EnumCertificates). Measured on the
rem       bundled 3.12.10: create_default_context() loads 91 CA certs, and
rem       urllib over https completes the TLS handshake fine.
rem       (The 403 from a government site afterwards is UA rejection,
rem       not a certificate problem.)
rem     - requests does not consult the system store at all -- it ships
rem       certifi inside site-packages and uses that.
rem   So these two lines fix nothing today. They stay because they cost
rem   nothing and cover the one case where a machine has no readable
rem   Windows root store, but do not believe the comment above about a
rem   bug they are not fixing.
set "SSL_CERT_FILE=%CD%\runtime\cacert.pem"
set "REQUESTS_CA_BUNDLE=%CD%\runtime\cacert.pem"
goto :eof
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
