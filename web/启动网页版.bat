@echo off
chcp 65001 >nul
title RetroChimera Web UI
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
if exist "%~dp0..\.venv\Scripts\python.exe" (
    set "ENV_PY=%~dp0..\.venv\Scripts\python.exe"
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
rem Default 16 uses all CPU cores = ~70 GB, which crashes (WinError 1455).
rem 4 = about 18 GB, safe and sufficient. Lower to 2 if memory is tight.
if not defined RETRO_RULE_WORKERS set "RETRO_RULE_WORKERS=4"
echo workers=%RETRO_RULE_WORKERS% >> "%LOGFILE%"

echo ==================================================================
echo   RetroChimera Web UI
echo ==================================================================
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
"%ENV_PY%" -m pip install --no-index --find-links "%~dp0wheels" flask werkzeug blinker itsdangerous "importlib-metadata==8.5.0" -q >>"%LOGFILE%" 2>&1
if errorlevel 1 (
    echo       Local install failed, trying online ...
    "%ENV_PY%" -m pip install flask -q >>"%LOGFILE%" 2>&1
)
goto START

:START
if not defined RETRO_PORT set "RETRO_PORT=8000"

rem === 端口占用检测（换机器 / 重复双击时最容易踩的坑） ===
netstat -an | findstr /c:":%RETRO_PORT% " | findstr /i "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo.
    echo [ERROR] Port %RETRO_PORT% is already in use.
    echo.
    echo   Another RetroChimera server is probably still running.
    echo   Either close that window, or start this one on a free port:
    echo.
    echo       set RETRO_PORT=8011
    echo.
    netstat -ano ^| findstr /i "LISTENING" ^| findstr /c:":%RETRO_PORT% "
    echo.
    pause
    exit /b 1
)

echo [2/3] Starting server ...
echo.
echo   Opening browser: http://127.0.0.1:%RETRO_PORT%
echo   Close this window to stop the server.
echo.
echo   NOTE: closing the server may print red "BrokenPipeError" lines.
echo         That is NORMAL (worker cleanup) and does not affect results.
echo.
echo [start] launching app.py >> "%LOGFILE%"
start "" http://127.0.0.1:%RETRO_PORT%
"%ENV_PY%" -u "%~dp0app.py" 2>>"%LOGFILE%"

echo.
echo [3/3] Server stopped.
echo       Details: _startup_log.txt
echo.
echo   Tips:
echo     - Red BrokenPipeError lines on exit are normal, not a crash.
echo     - If the page cannot open, check _startup_log.txt.
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
