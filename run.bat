@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Virtual environment missing - run setup.bat first.
  exit /b 1
)
if not exist ".env" (
  echo No .env file - run setup.bat first and fill in the keys.
  exit /b 1
)
echo Starting AdmissionOS Prime... (Ctrl+C to stop)
".venv\Scripts\python.exe" -m app.main
