@echo off
REM ==========================================================================
REM  VU3INI ResearchMate - Offline installer  (By Sasanka Boruah -VU3INI)
REM  Installs every dependency from vendor\ with NO internet:
REM   - Python: existing Python 3.10+ (a .venv is created), otherwise the portable
REM             Python 3.12 bundled in vendor\python-runtime (packages go into it)
REM   - Python packages from vendor\wheels
REM   - Tools already unpacked in vendor\bin\tools (Ollama, llama.cpp CUDA, Pandoc,
REM     Typst, Vale, Harper) - nothing to install for them
REM   - Local LLM: starts the bundled/installed Ollama and pulls llama3.1:8b
REM     (needs internet once; Step 4 retries automatically if you are offline)
REM ==========================================================================
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title VU3INI ResearchMate - Offline install
set "PYTHONUTF8=1"
set "BUNDLED=%~dp0vendor\python-runtime\python.exe"

REM Multi-part download: every part drops vendor\_partN_of_M.ok. Parts that Windows "Extract All" put into
REM their own folders, or ZIPs not yet extracted, are found and merged in automatically (same version only).
set "APPROOT=%~dp0"
set "APPROOT=%APPROOT:~0,-1%"
for %%I in ("%APPROOT%\..") do set "OUTER=%%~fI"
for %%I in ("%APPROOT%\..\..") do set "OUTER2=%%~fI"
set "VER="
if exist "VERSION" for /f "usebackq tokens=1 delims= " %%V in ("VERSION") do if not defined VER set "VER=%%V"
set "PARTFILE="
for %%F in ("vendor\_part*_of_*.ok") do set "PARTFILE=%%~nF"
if not defined PARTFILE goto parts_done
for /f "tokens=3 delims=_." %%T in ("!PARTFILE!") do set "NPARTS=%%T"
for /l %%N in (1,1,!NPARTS!) do if not exist "vendor\_part%%N_of_!NPARTS!.ok" call :fetch_part %%N !NPARTS!
set "MISSING="
for /l %%N in (1,1,!NPARTS!) do if not exist "vendor\_part%%N_of_!NPARTS!.ok" set "MISSING=!MISSING! %%N"
if defined MISSING (
  echo.
  echo [ERROR] Still missing part^(s^):!MISSING! of !NPARTS! ^(version !VER!^).
  echo         Put VU3INI_ResearchMate_v!VER!_partNof!NPARTS!.zip next to this folder or in Downloads,
  echo         or extract every part into the SAME folder, then run install_offline.bat again.
  pause & exit /b 1
)
echo All !NPARTS! bundle parts present.
:parts_done
if exist "vendor\_join.bat" (
  echo Re-joining large files that were split for download ...
  call "vendor\_join.bat" || (echo [ERROR] Could not re-join split files. & pause & exit /b 1)
  del "vendor\_join.bat"
)

call :find_python
if defined PY (
  echo Using installed Python: !PY!
  if not exist ".venv\Scripts\python.exe" (
    "!PY!" -m venv .venv || (echo [ERROR] Could not create .venv & pause & exit /b 1)
  )
  set "VPY=%~dp0.venv\Scripts\python.exe"
  if exist ".use-bundled-python" del ".use-bundled-python"
) else (
  if not exist "%BUNDLED%" (
    echo [ERROR] No Python 3.10+ found and vendor\python-runtime is missing from this bundle.
    pause & exit /b 1
  )
  echo No Python found - using the portable Python 3.12 bundled with ResearchMate.
  set "VPY=%BUNDLED%"
  echo bundled> ".use-bundled-python"
)

echo Installing core packages offline from vendor\wheels ...
"%VPY%" -m pip install --no-index --find-links=vendor\wheels --disable-pip-version-check -q --upgrade pip
"%VPY%" -m pip install --no-index --find-links=vendor\wheels --disable-pip-version-check -r requirements.txt
if errorlevel 1 (echo [ERROR] Offline install of core packages failed. & pause & exit /b 1)

echo Installing optional ML packages if bundled ...
for /f "usebackq eol=# tokens=1 delims=;" %%R in ("requirements-ml.txt") do (
  "%VPY%" -m pip install --no-index --find-links=vendor\wheels --disable-pip-version-check -q "%%R" >nul 2>&1 ^
    && echo   [ok]   "%%R" || echo   [skip] "%%R" - not bundled, built-in engine will be used
)

"%VPY%" scripts\make_logo.py >nul 2>&1

if exist "vendor\bin\tools\Library\bin\llama-server.exe" echo GPU drafting: bundled llama.cpp CUDA 12.9 found.

REM Local LLM for Step 4: start Ollama, pull llama3.1:8b (offline: import a bundled GGUF if present).
REM A failure here is not fatal - Step 4 retries it.
"%VPY%" scripts\setup_llm.py

echo.
echo ==========================================================
echo  VU3INI ResearchMate installed. Start it with run_ResearchMate.bat
echo ==========================================================
endlocal
exit /b 0

:fetch_part
REM %1 = part number, %2 = number of parts. 1) sibling folder from "Extract All"  2) the ZIP itself
for /d %%D in ("%OUTER2%\*v%VER%_part%1of%2*" "%OUTER%\*v%VER%_part%1of%2*") do (
  if exist "%%~D\VU3INI-ResearchMate\vendor\_part%1_of_%2.ok" if not exist "vendor\_part%1_of_%2.ok" (
    echo Merging part %1 of %2 from "%%~D" ...
    robocopy "%%~D\VU3INI-ResearchMate" "%APPROOT%" /E /NFL /NDL /NJH /NJS /NP /R:1 /W:1 >nul
  )
)
if exist "vendor\_part%1_of_%2.ok" exit /b 0
for %%Z in ("%OUTER%\VU3INI_ResearchMate_v%VER%_part%1of%2.zip" "%OUTER2%\VU3INI_ResearchMate_v%VER%_part%1of%2.zip" "%USERPROFILE%\Downloads\VU3INI_ResearchMate_v%VER%_part%1of%2.zip") do (
  if exist "%%~Z" if not exist "vendor\_part%1_of_%2.ok" (
    echo Extracting %%~nxZ ...
    tar -xf "%%~Z" -C "%OUTER%" 2>nul
    if not exist "vendor\_part%1_of_%2.ok" powershell -NoProfile -Command "Expand-Archive -LiteralPath '%%~Z' -DestinationPath '%OUTER%' -Force"
  )
)
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
REM ignore the Microsoft Store "python" stub
if defined PY echo !PY! | find /I "WindowsApps" >nul && set "PY="
exit /b 0
