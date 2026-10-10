@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo   cacx  -  sync latest articles
echo   Chinese: CAC (www.cac.gov.cn) + Zhejiang (www.zjwx.gov.cn)
echo ============================================================
echo.
echo Reads public listing APIs only. No login, no scraping of private data.
echo First full run ~20 min. Daily incremental usually 1-3 min.
echo.
echo Extra args are passed through, e.g.:
echo   sync-latest.bat --year 2026
echo   sync-latest.bat --site cac
echo.
choice /c YN /n /m "Start now? (Y/N) "
if errorlevel 2 exit /b 0

echo.
python -m cacx sync --mode incremental %*

echo.
python -m cacx stats
echo.
pause