@echo off
rem IMURUN launcher for Windows: sets everything up on first run, then starts.
rem Usage: run.bat [start.py options, e.g. --browser --port 8800]
setlocal
cd /d "%~dp0"
set "VPY=%~dp0.venv\Scripts\python.exe"

"%VPY%" -c "import aiohttp, httpx, huggingface_hub, yaml" >nul 2>&1
if not errorlevel 1 goto start

echo Setting up IMURUN (first run, or the .venv is missing/broken)...
call :findpy
if defined PY goto install

rem ---- No suitable Python: download and install Python 3.12 -----------------
where winget >nul 2>&1
if errorlevel 1 goto nopython
echo.
echo Python 3.11+ was not found. Downloading Python 3.12 with winget...
winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
call :findpy
if defined PY goto install
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set "PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe""
    goto install
)

:nopython
echo.
echo Python 3.11+ is required but could not be installed automatically.
echo Download it from https://www.python.org/downloads/windows/
echo (tick "Add python.exe to PATH"), then run run.bat again.
pause
exit /b 1

:install
%PY% install.py
if errorlevel 1 (
    echo.
    echo Installation failed. See docs\setup\README.md
    pause
    exit /b 1
)

:start
"%VPY%" start.py %*
exit /b %errorlevel%

rem ---- Find Python 3.11-3.14 (never the Microsoft Store stub) ---------------
:findpy
set "PY="
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set "PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe""
    exit /b 0
)
for %%V in (3.12 3.13 3.11 3.14) do (
    if not defined PY (
        py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V"
    )
)
if defined PY exit /b 0
python -c "import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] <= (3, 14) else 1)" >nul 2>&1 && set "PY=python"
exit /b 0
