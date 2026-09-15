@echo off
cd /d "%~dp0"
if "%~1"=="" (
  echo Enrol and manage AdmissionOS users. Examples:
  echo.
  echo   users.bat add dm --name "Digital Marketing" --tier executive --telegram 123456789
  echo   users.bat add ravi --name "Ravi K" --tier operational --whatsapp 919876543210
  echo   users.bat set dm --whatsapp 919876543210
  echo   users.bat list
  echo   users.bat disable dm
  echo   users.bat enable dm
  echo   users.bat remove dm
  echo.
  echo Tiers: executive ^| operational ^| viewer
  echo Telegram ID: the bot shows it when an un-enrolled person messages it.
  exit /b 0
)
".venv\Scripts\python.exe" scripts\manage_users.py %*
