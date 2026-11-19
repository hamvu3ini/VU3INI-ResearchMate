@echo off
REM  VU3INI ResearchMate - starts the local server and opens it in your DEFAULT web browser.
REM  By Sasanka Boruah -VU3INI
setlocal
cd /d "%~dp0"
title VU3INI ResearchMate
set "PYTHONUTF8=1"
set "VPY="
if exist ".venv\Scripts\python.exe" set "VPY=%~dp0.venv\Scripts\python.exe"
if not defined VPY if exist ".use-bundled-python" if exist "vendor\python-runtime\python.exe" set "VPY=%~dp0vendor\python-runtime\python.exe"
if not defined VPY (
  echo First run - installing offline dependencies ...
  call install_offline.bat || exit /b 1
  if exist ".venv\Scripts\python.exe" (set "VPY=%~dp0.venv\Scripts\python.exe") else (set "VPY=%~dp0vendor\python-runtime\python.exe")
)
"%VPY%" run.py %*
if errorlevel 1 pause
endlocal
