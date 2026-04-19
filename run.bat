@echo off
REM Second Spring Control launcher (Windows)
REM Creates venv on first run, installs deps, boots the app, opens browser.

setlocal
cd /d "%~dp0"

if not exist .venv (
    echo Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: python not found. Install Python 3.11+ from python.org first.
        pause
        exit /b 1
    )
)

call .venv\Scripts\activate.bat

REM Install/upgrade deps quietly. Skip if .deps_installed marker exists.
if not exist .deps_installed (
    echo Installing dependencies...
    pip install -q --upgrade pip
    pip install -q -r requirements.txt
    if errorlevel 1 (
        echo ERROR: pip install failed.
        pause
        exit /b 1
    )
    type nul > .deps_installed
)

if not exist .env (
    echo No .env file found. Copying .env.example to .env.
    echo You MUST edit .env to add OPENAI_API_KEY and the path variables.
    copy .env.example .env
    notepad .env
)

REM Kill anything already listening on port 8080 so restart is clean.
echo Checking port 8080...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8080 " ^| findstr "LISTENING"') do (
    echo   Killing PID %%P on port 8080
    taskkill /F /PID %%P >nul 2>&1
)

REM Open browser shortly after server starts.
start "" /b cmd /c "timeout /t 3 /nobreak > nul && start http://localhost:8080"

echo.
echo ====================================================
echo  Second Spring Control: http://localhost:8080
echo  Press Ctrl+C to stop.
echo ====================================================
echo.

REM --reload: uvicorn restarts the server whenever any .py file changes.
REM This means code edits take effect immediately, no manual restart.
python -m uvicorn control.main:app --host 127.0.0.1 --port 8080 --reload
endlocal
