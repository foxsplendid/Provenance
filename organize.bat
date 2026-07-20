@echo off
cd /d "%~dp0"
set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
  echo [!] Project environment not found: %PYTHON%
  echo [!] Run: uv sync --locked
  exit /b 1
)
echo ============================================================
echo  Provenance organize
echo  Read inbox/, classify by keyword rules, build
echo  output/00_index.md + output/00_catalog.csv
echo ============================================================
echo.
"%PYTHON%" -m provenance.organize.core %*
if errorlevel 1 (
  echo.
  echo [!] Provenance organize failed.
)
echo.
pause
