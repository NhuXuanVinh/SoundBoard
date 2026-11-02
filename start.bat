@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Creating the local Python environment...
    for %%V in (3.12 3.11 3.10 3.9) do (
        py -%%V -c "import sys" >nul 2>&1
        if not errorlevel 1 (
            py -%%V -m venv .venv
            if errorlevel 1 goto failed
            goto environment_ready
        )
    )
    echo Install 64-bit Python 3.9 through 3.12, including the Python launcher.
    goto failed
)
:environment_ready
".venv\Scripts\python.exe" -c "import PySide6, numpy, scipy, sounddevice, soundfile" >nul 2>&1
if errorlevel 1 (
    echo Installing soundboard dependencies...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" main.py
if errorlevel 1 goto failed
exit /b 0
:failed
echo.
echo Soundboard could not start. Check the error above.
pause
exit /b 1
