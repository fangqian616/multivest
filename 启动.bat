@echo off
chcp 65001 >nul 2>&1
setlocal EnableDelayedExpansion
title 智能多维投资系统 · 家庭资产配置多智能体投顾

cd /d "%~dp0"

set "PORT=8760"
set "PYEXE="

echo.
echo ==================================================================
echo    智能多维投资系统
echo ==================================================================
echo.

REM ── 1. 找 Python ────────────────────────────────────────────────────
where python >nul 2>&1
if %errorlevel%==0 (
    for /f "delims=" %%i in ('where python') do (
        if not defined PYEXE set "PYEXE=%%i"
    )
)
if not defined PYEXE (
    echo [错误] 未找到 Python。请先安装 Python 3.10+ 并加入 PATH。
    echo        下载：https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)
echo [1/5] Python: !PYEXE!

REM ── 2. 检查关键依赖 ─────────────────────────────────────────────────
"!PYEXE!" -c "import fastapi, uvicorn, requests" >nul 2>&1
if not %errorlevel%==0 (
    echo [2/5] 缺少依赖，正在安装...
    "!PYEXE!" -m pip install -q fastapi uvicorn requests qrcode
    if not !errorlevel!==0 (
        echo [错误] 依赖安装失败。请手动执行：
        echo        "!PYEXE!" -m pip install fastapi uvicorn requests qrcode
        pause
        exit /b 1
    )
) else (
    echo [2/5] 依赖检查通过
)

REM ── 3. 检查内置数据集 ───────────────────────────────────────────────
if not exist "backend\data\dataset\market_history.json" (
    echo [3/5] 内置数据集缺失，正在抓取历史行情（约 1 分钟）...
    "!PYEXE!" tools\fetch_market_data.py --build
) else (
    echo [3/5] 内置数据集就绪
)

REM ── 4. 检查 API Key ─────────────────────────────────────────────────
"!PYEXE!" -c "import sys;sys.path.insert(0,'backend');from engine.llm import resolve_api_key;k,s=resolve_api_key();sys.exit(0 if k else 1)" >nul 2>&1
if not %errorlevel%==0 (
    echo.
    echo [警告] 未找到 DEEPSEEK_API_KEY，审议功能将不可用。
    echo        请在项目根目录创建 .env 文件，写入：
    echo            DEEPSEEK_API_KEY=sk-你的密钥
    echo        （可参考 .env.example）
    echo.
) else (
    echo [4/5] API Key 已就绪
)

REM ── 5. 启动服务 ─────────────────────────────────────────────────────
echo [5/5] 启动本地服务...
start "智能多维投资系统-服务" /min cmd /c ""!PYEXE!" backend\app.py --host 0.0.0.0 --port %PORT%"

REM 等待端口就绪（最多 30 秒）
set /a TRIES=0
:WAIT
set /a TRIES+=1
timeout /t 1 /nobreak >nul
"!PYEXE!" -c "import socket,sys;s=socket.socket();s.settimeout(0.4);sys.exit(0 if s.connect_ex(('127.0.0.1',%PORT%))==0 else 1)" >nul 2>&1
if not %errorlevel%==0 (
    if !TRIES! LSS 30 goto WAIT
    echo.
    echo [错误] 服务启动超时。请查看「智能多维投资系统-服务」窗口中的报错信息。
    pause
    exit /b 1
)

echo.
echo ==================================================================
echo    服务已启动
echo ==================================================================
for /f "delims=" %%i in ('"!PYEXE!" -c "import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect(('8.8.8.8',80));print(s.getsockname()[0])"') do set "LANIP=%%i"
echo    本机访问：  http://127.0.0.1:%PORT%
echo    手机访问：  http://!LANIP!:%PORT%      （需同一 WiFi）
echo.
echo    手机连接：在上面地址用手机浏览器打开，或扫描页面内「手机连接」的二维码
echo ==================================================================
echo.

REM ── 打开窗口：优先独立应用，退化到 Edge 应用模式，再退化到默认浏览器 ──
if exist "dist\智能多维投资系统.exe" (
    start "" "dist\智能多维投资系统.exe"
    goto DONE
)
if exist "dist\IMIS.exe" (
    start "" "dist\IMIS.exe"
    goto DONE
)

set "EDGE1=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
set "EDGE2=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
set "EDGE3=%LocalAppData%\Microsoft\Edge\Application\msedge.exe"
if exist "!EDGE1!" (
    start "" "!EDGE1!" --app=http://127.0.0.1:%PORT% --window-size=1480,980
    goto DONE
)
if exist "!EDGE2!" (
    start "" "!EDGE2!" --app=http://127.0.0.1:%PORT% --window-size=1480,980
    goto DONE
)
if exist "!EDGE3!" (
    start "" "!EDGE3!" --app=http://127.0.0.1:%PORT% --window-size=1480,980
    goto DONE
)

start "" "http://127.0.0.1:%PORT%"

:DONE
echo.
echo 提示：关闭「智能多维投资系统-服务」窗口即可停止服务。
echo.
timeout /t 6 /nobreak >nul
exit /b 0
