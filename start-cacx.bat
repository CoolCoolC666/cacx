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

rem ---- install deps on first run ----
rem 关系图谱依赖 pyvis(内联 vis-network)/networkx(关联分析)/jieba(分词与机构挖掘)，
rem 首启自检必须一并检查，否则这 4 个装了而 pyvis 等缺失时，脚本会跳过安装、
rem 图谱页因缺少 vis 资源无法加载。
python -c "import fastapi, uvicorn, requests, bs4, pyvis, jieba, networkx" >nul 2>nul
if errorlevel 1 (
    echo [1/2] First run - installing dependencies (incl. graph libs pyvis/networkx/jieba), one time only.
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
echo   URL  : http://127.0.0.1:8770/
echo   API  : http://127.0.0.1:8770/docs
echo   Stop : close this window (or press Ctrl+C)
echo.
echo ============================================================
echo.

python -m cacx serve

echo.
echo Server stopped.
pause