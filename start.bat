@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PY="

rem 1) 环境变量指定
if defined FORKSYNC_PYTHON if exist "%FORKSYNC_PYTHON%" set "PY=%FORKSYNC_PYTHON%"

rem 2) 项目内虚拟环境
if not defined PY if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"

rem 3) py 启动器
if not defined PY py -3 -c "import sys" >nul 2>&1
if not defined PY if %ERRORLEVEL% EQU 0 set "PY=py -3"

rem 4) PATH 里的 python
if not defined PY python -c "import sys" >nul 2>&1
if not defined PY if %ERRORLEVEL% EQU 0 set "PY=python"

if not defined PY (
  echo [x] 找不到可用的 Python 3。
  echo     安装 Python 3.9+ 后重试，或设置环境变量 FORKSYNC_PYTHON 指向 python.exe：
  echo     set FORKSYNC_PYTHON=C:\Path\To\python.exe
  pause
  exit /b 1
)

echo [*] 解释器: %PY%
%PY% app.py --port 8770 --open %*
pause
