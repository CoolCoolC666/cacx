@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem ============================================================
rem   cacx  -  dev clean
rem   Tidy the project root:
rem     - move stray dev scripts / logs / screenshots into the
rem       test-archive folder
rem     - send __pycache__ to the Recycle Bin (recoverable)
rem     - run the full regression to prove nothing broke
rem
rem   NOTE: this file is intentionally pure ASCII.
rem   cmd decodes .bat using the OEM code page (GBK on a
rem   Chinese Windows); UTF-8 Chinese in here turns into
rem   mojibake and breaks the command line.
rem ============================================================

echo.
echo ============================================================
echo   cacx  -  dev clean
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] python not found in PATH.
    echo   Please install Python 3.10+ and tick "Add Python to PATH".
    echo.
    pause
    exit /b 1
)

python _clean.py
set RC=%ERRORLEVEL%

echo.
if %RC%==0 (
    echo Done. Nothing changed in cacx\ itself.
) else (
    echo [WARN] exit code %RC% - read the messages above before committing.
)
echo.
pause
exit /b %RC%
