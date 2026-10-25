@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PY="
set "PYARGS="

rem ---- 1) 环境变量指定（最高优先级）----
if defined FORKSYNC_PYTHON if exist "%FORKSYNC_PYTHON%" set "PY=%FORKSYNC_PYTHON%"

rem ---- 2) 项目内虚拟环境 ----
if not defined PY if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"

rem ---- 3) 常见安装位置 ----
if not defined PY call :probe "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PY call :probe "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY call :probe "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY call :probe "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if not defined PY call :probe "C:\Python313\python.exe"
if not defined PY call :probe "C:\Python312\python.exe"
if not defined PY call :probe "C:\Python311\python.exe"
if not defined PY call :probe "C:\Python310\python.exe"

rem ---- 4) py 启动器 ----
if not defined PY call :probe "py" "-3"

rem ---- 5) PATH 里的 python / python3（会自动跳过微软商店假壳）----
if not defined PY call :probe "python"
if not defined PY call :probe "python3"

if not defined PY (
  echo.
  echo [x] 找不到可用的 Python 3。
  echo.
  echo     常见原因：系统里只有微软商店的 python 占位程序，
  echo     它不是真正的 Python，运行只会打印 "Python was not found"。
  echo.
  echo     解决办法（任选其一）：
  echo       1. 到 https://www.python.org/downloads/ 安装 Python 3.9+，
  echo          安装时务必勾选 "Add python.exe to PATH"
  echo       2. 或手动指定解释器后再运行本脚本：
  echo          set FORKSYNC_PYTHON=C:\Path\to\python.exe
  echo          start.bat
  echo.
  pause
  exit /b 1
)

echo [*] 解释器: %PY% %PYARGS%
"%PY%" %PYARGS% app.py --port 8770 --open %*
pause
exit /b 0

rem ====================================================================
rem :probe <可执行文件> [额外参数]
rem   只认「退出码为 0 且输出版本号 3」的解释器。
rem   微软商店假壳虽然文件存在，但退出码是 9009 且输出提示文字，会被排除。
rem   用临时文件接收输出，避免 for /f 的嵌套引号被 cmd 解析坏。
rem ====================================================================
:probe
set "CAND=%~1"
set "EXTRA=%~2"
if "%CAND%"=="" exit /b 0
if /i not "%CAND%"=="py" if /i not "%CAND%"=="python" if /i not "%CAND%"=="python3" if not exist "%CAND%" exit /b 0

set "VER="
set "VFILE=%TEMP%\forksync_probe_%RANDOM%%RANDOM%.txt"
if "%EXTRA%"=="" (
  "%CAND%" -c "import sys;print(sys.version_info[0])" >"%VFILE%" 2>nul
) else (
  "%CAND%" %EXTRA% -c "import sys;print(sys.version_info[0])" >"%VFILE%" 2>nul
)
if not !ERRORLEVEL! EQU 0 goto :probe_done
set /p VER=<"%VFILE%"

:probe_done
if exist "%VFILE%" del "%VFILE%" >nul 2>&1
if "!VER!"=="3" (
  set "PY=%CAND%"
  set "PYARGS=%EXTRA%"
)
exit /b 0
