#Requires -Version 5.1
<#
.SYNOPSIS
    Clipify launcher script for PowerShell.
.DESCRIPTION
    Checks for Python, activates a virtual environment if present,
    installs Python dependencies if needed, then starts the Clipify server
    and opens the browser automatically.
#>

$Host.UI.RawUI.WindowTitle = "Clipify — YouTube to Vertical Clips"

Write-Host ""
Write-Host "  ==========================================" -ForegroundColor Green
Write-Host "   Clipify - YouTube to Vertical Clips" -ForegroundColor Green
Write-Host "  ==========================================" -ForegroundColor Green
Write-Host ""

# Check Python
try {
    $pyVersion = python --version 2>&1
    Write-Host "  [INFO] Using $pyVersion" -ForegroundColor Cyan
} catch {
    Write-Host "  [ERROR] Python not found on PATH." -ForegroundColor Red
    Write-Host "  Please install Python 3.10+ from https://python.org" -ForegroundColor Yellow
    Read-Host "  Press Enter to exit"
    exit 1
}

# Activate virtual environment if present
if (Test-Path ".venv\Scripts\Activate.ps1") {
    Write-Host "  [INFO] Activating .venv virtual environment..." -ForegroundColor Cyan
    & ".venv\Scripts\Activate.ps1"
} elseif (Test-Path "venv\Scripts\Activate.ps1") {
    Write-Host "  [INFO] Activating venv virtual environment..." -ForegroundColor Cyan
    & "venv\Scripts\Activate.ps1"
}

# Check / install dependencies
python -c "import fastapi, uvicorn, pydantic" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  [INFO] Installing Python dependencies..." -ForegroundColor Cyan
    pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  [ERROR] Failed to install dependencies." -ForegroundColor Red
        Read-Host "  Press Enter to exit"
        exit 1
    }
}

Write-Host "  [INFO] Starting Clipify on http://127.0.0.1:8000" -ForegroundColor Green
Write-Host "  [INFO] Press Ctrl+C to stop the server." -ForegroundColor Yellow
Write-Host ""

# Open browser after short delay (background job)
Start-Job -ScriptBlock {
    Start-Sleep -Seconds 2
    Start-Process "http://127.0.0.1:8000"
} | Out-Null

# Start the server
python app.py
