@echo off
title Clipify — YouTube to Vertical Clips
color 0A

echo.
echo  ==========================================
echo   Clipify - YouTube to Vertical Clips
echo  ==========================================
echo.

:: Check if Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python not found on PATH.
    echo  Please install Python 3.10+ from https://python.org
    pause
    exit /b 1
)

:: Activate virtual environment if it exists
if exist ".venv\Scripts\activate.bat" (
    echo  [INFO] Activating virtual environment...
    call .venv\Scripts\activate.bat
) else if exist "venv\Scripts\activate.bat" (
    echo  [INFO] Activating virtual environment...
    call venv\Scripts\activate.bat
)

:: Check for required packages
python -c "import fastapi, uvicorn, pydantic" >nul 2>&1
if errorlevel 1 (
    echo  [INFO] Installing dependencies...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo  [ERROR] Failed to install dependencies.
        pause
        exit /b 1
    )
)

echo  [INFO] Starting Clipify on http://127.0.0.1:8000
echo  [INFO] Press Ctrl+C to stop.
echo.

:: Open browser after a short delay (in background)
start "" cmd /c "timeout /t 2 >nul && start http://127.0.0.1:8000"

:: Start the server
python app.py

pause
