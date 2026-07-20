@echo off
cd /d "%~dp0"
set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
  echo [!] Project environment not found: %PYTHON%
  echo [!] Run: uv sync --locked
  exit /b 1
)
echo ============================================================
echo  Provenance watcher
echo  Drop exported ZIPs into the "drop" folder and they are
echo  imported automatically (provenance__*.zip and Takeout*.zip).
echo  Keep this window open, or register it to run at logon.
echo  Press Ctrl+C to stop.
echo ============================================================
echo.
"%PYTHON%" -m provenance.ingest.watch
if errorlevel 1 (
  echo.
  echo [!] Provenance watcher failed.
)
echo.
pause
