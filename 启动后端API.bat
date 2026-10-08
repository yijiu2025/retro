@echo off
chcp 65001 >nul
title RetroChimera API
rem === 本文件位于仓库根目录，双击即用 ===
rem 所有路径统一以 %~dp0（仓库根）为基准，换盘符 / 换目录都不用改。
cd /d "%~dp0"

set "LOGFILE=%~dp0_startup_log.txt"

rem === Python 解释器探测（换机器时不用改代码） ===
rem 优先级：环境变量 RETRO_PYTHON > 当前激活的 conda 环境 > 仓库内 .venv > 默认 conda 位置 > PATH 里的 python
if defined RETRO_PYTHON (
    set "ENV_PY=%RETRO_PYTHON%"
    goto GOT_PYTHON
)
if defined CONDA_PREFIX (
    if exist "%CONDA_PREFIX%\python.exe" (
        set "ENV_PY=%CONDA_PREFIX%\python.exe"
        goto GOT_PYTHON
    )
)
if exist "%~dp0.venv\Scripts\python.exe" (
    set "ENV_PY=%~dp0.venv\Scripts\python.exe"
    goto GOT_PYTHON
)
if exist "C:\ProgramData\miniconda3\envs\retrochimera\python.exe" (
    set "ENV_PY=C:\ProgramData\miniconda3\envs\retrochimera\python.exe"
    goto GOT_PYTHON
)
for /f "delims=" %%P in ('where python 2^>nul') do (
    set "ENV_PY=%%P"
    goto GOT_PYTHON
)

:NO_PYTHON_FOUND
echo [ERROR] Could not find any python.exe.
echo.
echo   Options:
echo     1) Activate your env first:  conda activate retrochimera
echo     2) Or set it explicitly:     set RETRO_PYTHON=C:\path\to\python.exe
echo.
pause
exit /b 1

:GOT_PYTHON

echo [%date% %time%] bat started > "%LOGFILE%"
echo cwd=%CD% >> "%LOGFILE%"
echo python=%ENV_PY% >> "%LOGFILE%"

rem === CONFIG: rule-application worker processes (1-16) ===
rem 默认 16 会吃满所有核心 ≈ 70 GB，直接崩（WinError 1455）。
rem 4 ≈ 18 GB，够用且安全；内存紧就降到 2。
rem 🔴 这些子进程现在归**模型服务**起（web/model_server.py），不再由 API 起。
if not defined RETRO_RULE_WORKERS set "RETRO_RULE_WORKERS=4"
echo workers=%RETRO_RULE_WORKERS% >> "%LOGFILE%"

echo ==================================================================
echo   RetroChimera API
echo ==================================================================
echo.
echo   This window  = API service   (:8000, ready in ~1s)
echo   Other window = model service (:8001, loads in the background)
echo.
echo   The UI only ever talks to the API; the model service is internal.
echo.

if not exist "%ENV_PY%" goto NO_ENV
echo [ok] python found >> "%LOGFILE%"

echo [1/3] Check dependencies ...
"%ENV_PY%" -c "import flask" 2>>"%LOGFILE%"
if errorlevel 1 goto INSTALL_FLASK
echo [ok] flask present >> "%LOGFILE%"
goto START

:INSTALL_FLASK
echo [warn] flask missing, installing >> "%LOGFILE%"
echo       Installing Flask from local wheels ...
"%ENV_PY%" -m pip install --no-index --find-links "%~dp0deploy\wheels" flask werkzeug blinker itsdangerous "importlib-metadata==8.5.0" -q >>"%LOGFILE%" 2>&1
if errorlevel 1 (
    echo       Local install failed, trying online ...
    "%ENV_PY%" -m pip install flask -q >>"%LOGFILE%" 2>&1
)
goto START

:START
if not defined RETRO_PORT set "RETRO_PORT=8000"
if not defined RETRO_MODEL_PORT set "RETRO_MODEL_PORT=8001"

rem === 端口占用检测（换机器 / 重复双击时最容易踩的坑） ===
netstat -an | findstr /c:":%RETRO_PORT% " | findstr /i "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo.
    echo [ERROR] Port %RETRO_PORT% is already in use.
    echo.
    echo   Another RetroChimera API is probably still running.
    echo   Either close that window, or start this one on free ports:
    echo.
    echo       set RETRO_PORT=8011
    echo       set RETRO_MODEL_PORT=8012
    echo.
    netstat -ano ^| findstr /i "LISTENING" ^| findstr /c:":%RETRO_PORT% "
    echo.
    pause
    exit /b 1
)

rem === 模型服务是否已在跑？ ===
rem 在跑 ⇒ API 起来后会**复用它**，模型不重载（这是拆分的最大好处）。
rem 🔴 :%RETRO_MODEL_PORT% 被占不是错误，别在这里拦 —— 那正是「复用」的前提。
netstat -an | findstr /c:":%RETRO_MODEL_PORT% " | findstr /i "LISTENING" >nul 2>&1
if errorlevel 1 goto MODEL_COLD
set "MODEL_RUNNING=1"
goto MODEL_INFO

:MODEL_COLD
set "MODEL_RUNNING=0"

:MODEL_INFO

echo [2/3] Starting API server ...
echo.
echo   API    http://127.0.0.1:%RETRO_PORT%
if "%MODEL_RUNNING%"=="1" echo   Model  REUSING the one already up on 127.0.0.1:%RETRO_MODEL_PORT% -- NOT reloaded.
if "%MODEL_RUNNING%"=="0" echo   Model  will open a SEPARATE window and load in the background.
echo.
echo   Model loading progress:
echo     - the model window's own log, or
echo     - the UI panel:  运行参数 - 预测模型, or
echo     - http://127.0.0.1:%RETRO_MODEL_PORT%/status
echo.
echo   NOTE: this is the API only -- it serves no web page.
echo         The UI lives in the retroweb repo (npm run dev, port 5177).
echo         Opening the root URL shows the endpoint list as JSON.
echo.
echo   Closing THIS window stops the API only. The model service is
echo   PERSISTENT by design -- that is what makes restarting the API
echo   cheap. To stop it together with the API:
echo       set RETRO_MODEL_STOP_WITH_API=1
echo.
echo [start] launching app.py >> "%LOGFILE%"
"%ENV_PY%" -u "%~dp0web\app.py" 2>>"%LOGFILE%"

echo.
echo [3/3] API stopped.
echo       A model-service window may still be open -- that is expected:
echo       it keeps the model in memory for your next start.
echo       Details: _startup_log.txt
echo.
echo   Tips:
echo     - Red BrokenPipeError lines on exit are normal, not a crash.
echo     - If the API does not respond, check _startup_log.txt.
pause
exit /b 0

:NO_ENV
echo [ERROR] Python not found: %ENV_PY% >> "%LOGFILE%"
echo [ERROR] The interpreter exists but failed to run:
echo         %ENV_PY%
echo.
echo   Options:
echo     1) Activate the env and re-run:  conda activate retrochimera
echo     2) Point at a working exe:       set RETRO_PYTHON=C:\path\to\python.exe
echo.
pause
exit /b 1
