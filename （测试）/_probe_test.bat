@echo off
rem Test harness for the interpreter-probe block in start-cacx.bat.
rem It copies that block verbatim so a syntax regression shows up here
rem instead of on a classmate's machine. Test only -- never shipped.
rem Run it from a cmd window (the full path contains non-ASCII characters,
rem so do NOT put that path inside this file -- pass it on the command line).
rem ASCII only; see the note above.
rem cd to the project root: case 7 needs to find runtime\python.exe,
rem which sits one level up from this file.
cd /d "%~dp0.."
setlocal

echo ==== case 1: normal PATH (expect a usable PY) ====
call :t1
echo.

echo ==== case 2: PATH with no python at all (expect NO PY + honest report) ====
call :t2
echo.

echo ==== case 3: `python` absent but `py` present (must still find PY) ====
call :t3
echo.

echo ==== case 4: optional module present (expect the else branch) ====
call :mcp_branch "json"
echo.

echo ==== case 5: optional module MISSING (must NOT abort) ====
call :mcp_branch "zzz_no_such_module_zzz"
echo.

echo ==== case 6: after a missing optional module, can we still continue? ====
%PY% -c "import sys" >nul 2>nul
if errorlevel 1 (echo   FAIL interpreter unusable) else (echo   OK   launcher would carry on and start the server)
echo.

echo ==== case 7: bundled portable runtime (must win over system python) ====
call :t7
echo.

goto :eof

:t7
set "PY="
set "PYDESC="
set "PYSEEN="
set "PYBUNDLED="
if exist "runtime\python.exe" call :probe_bundle
if defined PY (echo   OK   bundled runtime wins: PY=[%PY%]  BUNDLED=[%PYBUNDLED%]) else (echo   note  no runtime\ here, launcher would fall back to a system Python)
if defined PYBUNDLED (
    echo   SSL_CERT_FILE    =[%SSL_CERT_FILE%]
    echo   CA bundle        =[%REQUESTS_CA_BUNDLE%]
    if not exist "runtime\cacert.pem" (echo   FAIL cacert.pem missing) else (echo   OK   cacert.pem present)
) else (
    echo   SKIP  no bundled runtime in this checkout
)
goto :eof

:probe_bundle
set "CAND=runtime\python.exe"
%CAND% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul
if errorlevel 1 goto :eof
set "PYBUNDLED=1"
set "PY=%CAND%"
set "PYDESC=bundled portable runtime ^(%CAND%^)"
set "SSL_CERT_FILE=%CD%\runtime\cacert.pem"
set "REQUESTS_CA_BUNDLE=%CD%\runtime\cacert.pem"
goto :eof

:mcp_branch
rem Mirrors the optional-MCP block in start-cacx.bat, with the module
rem name swapped in. The point being tested is NOT "is mcp installed" but
rem "does a missing optional package change the exit path".
set "MOD=%~1"
%PY% -c "import %MOD%" >nul 2>nul
if errorlevel 1 (
    echo   branch: MISSING -- reports and gets out of the way, no exit /b
) else (
    echo   branch: PRESENT -- prints the how-to-run line
)
goto :eof

:t1
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
if defined PY (echo   OK   PY=[%PY%]  DESC=[%PYDESC%]) else (echo   FAIL PY is empty)
echo   SEEN=[%PYSEEN%]
if defined PY goto :eof
echo   FAIL expected PY to be defined on this machine
goto :eof

:t2
setlocal
set "SAVEPATH=%PATH%"
set "PATH=C:\Windows\System32"
set "PY="
set "PYDESC="
set "PYSEEN="
call :probe "py -3"
if not defined PY call :probe "python"
if not defined PY call :probe "python3"
if defined PY (echo   FAIL unexpectedly found PY=[%PY%]) else (echo   OK   no PY, as intended)
echo   SEEN=[%PYSEEN%]
set "PATH=%SAVEPATH%"
goto :eof

:t3
setlocal
set "PY="
set "PYDESC="
set "PYSEEN="
call :probe "py -3"
if defined PY (echo   OK   found via py launcher: [%PY%]) else (echo   note  py -3 not usable here)
goto :eof

:probe
set "CAND=%~1"
set "FMJ="
set "FMN="
set "FEXE="
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
set "FOK=0"
if %FMJ% GTR 3 set "FOK=1"
if %FMJ% EQU 3 if %FMN% GEQ 8 set "FOK=1"
if "%FOK%"=="0" goto :eof
if defined PY goto :eof
set "PY=%CAND%"
set "PYDESC=%CAND% %FVER% at %FEXE%"
goto :eof
