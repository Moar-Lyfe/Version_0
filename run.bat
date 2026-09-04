@echo off
REM Start the Executive Dashboard on Windows.
REM
REM   run.bat                  start on http://localhost:8501
REM   run.bat --server.port 9000
REM
REM Creates .venv and installs dependencies on first run; afterwards it just
REM launches. Nothing is installed system-wide.
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.10+ is required but "python" was not found on PATH.
  echo Install it from https://www.python.org/downloads/ and tick "Add to PATH".
  exit /b 1
)

if not exist ".venv" (
  echo First run: creating virtual environment in .venv ...
  python -m venv .venv
  .venv\Scripts\python.exe -m pip install --upgrade pip >nul
  .venv\Scripts\python.exe -m pip install -r requirements.txt
  if errorlevel 1 exit /b 1
)

if not exist "config\config.yaml" (
  echo No config\config.yaml yet -- copying the example.
  copy /y config\config.example.yaml config\config.yaml >nul
  echo Edit config\config.yaml to point 'data.sources' at your workbooks.
)

if not exist "data\sample\*.xlsx" (
  echo Generating sample workbooks so the dashboard has something to show ...
  .venv\Scripts\python.exe tools\generate_sample_data.py
)

echo Starting the dashboard. Press Ctrl+C to stop.
.venv\Scripts\python.exe -m streamlit run app\main.py %*
endlocal
