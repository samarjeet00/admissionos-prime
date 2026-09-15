@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  exit /b 1
)
echo Packing .env, users and the Google key into secrets.zip - you will be asked for the passphrase (twice).
".venv\Scripts\python.exe" scripts\secrets_tool.py pack
if errorlevel 1 exit /b 1
git add secrets.zip
git commit -m "Update encrypted secrets" >nul 2>&1
git push origin main
echo Done - secrets.zip is on GitHub (encrypted).
