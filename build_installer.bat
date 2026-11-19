@echo off
REM ==========================================================================
REM  VU3INI ResearchMate - Build Installer  (By Sasanka Boruah -VU3INI)
REM  Stage 1: pytest test suite            (hard gate - no ZIP on failure)
REM  Stage 2: live default-browser check   ([Y] build / [N] abort)
REM  Stage 3: vendor offline deps + dist\VU3INI_ResearchMate_vX.Y.Z.zip
REM
REM  Options:  --auto-confirm  unattended build (no Y/N prompt)
REM            --refresh       re-download all vendored items
REM            --skip-ml       leave out torch / models / llama.cpp / Ollama
REM            --no-browser    do not open the browser in stage 2
REM  Needs internet on the BUILD machine only. The resulting ZIP installs offline.
REM ==========================================================================
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title VU3INI ResearchMate - Build Installer
set "ARGS=%*"
set "PYTHONUTF8=1"

call :find_python
if not defined PY (
  echo [ERROR] Python 3.10+ not found. Install Python from python.org and re-run.
  exit /b 1
)
echo Using build Python: %PY%

if not exist ".venv\Scripts\python.exe" (
  echo Creating build virtual environment .venv ...
  "%PY%" -m venv .venv || (echo [ERROR] venv creation failed & exit /b 1)
)
set "VPY=%~dp0.venv\Scripts\python.exe"

echo Installing build/test requirements ...
"%VPY%" -m pip install --disable-pip-version-check -q --upgrade pip
if exist "vendor\wheels" (
  "%VPY%" -m pip install --disable-pip-version-check -q --find-links=vendor\wheels -r requirements.txt
) else (
  "%VPY%" -m pip install --disable-pip-version-check -q -r requirements.txt
)
if errorlevel 1 (echo [ERROR] Could not install requirements. & exit /b 1)

"%VPY%" scripts\make_logo.py >nul

REM ---------------- Stage 1 + 2 (hard gates) ----------------
"%VPY%" scripts\verify_before_zip.py %ARGS%
if errorlevel 1 (
  echo.
  echo [BUILD HALTED] Verification failed or was aborted - no ZIP archive was created.
  exit /b 1
)

REM ---------------- Stage 3 ----------------
"%VPY%" scripts\build_offline_bundle.py %ARGS%
if errorlevel 1 (
  echo [ERROR] Packaging failed.
  exit /b 1
)
echo.
echo Build complete. Offline installer ZIP is in the dist folder:
dir /b dist\*.zip
endlocal
exit /b 0

:find_python
set "PY="
for %%V in (3.13 3.12 3.11 3.10) do (
  if not defined PY (
    py -%%V -c "import sys" >nul 2>&1 && for /f "delims=" %%P in ('py -%%V -c "import sys;print(sys.executable)"') do set "PY=%%P"
  )
)
if not defined PY (
  python -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1 && for /f "delims=" %%P in ('python -c "import sys;print(sys.executable)"') do set "PY=%%P"
)
exit /b 0
