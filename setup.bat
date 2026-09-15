@echo off
setlocal
cd /d "%~dp0"
echo === AdmissionOS Prime setup ===

where python >nul 2>&1
if errorlevel 1 (
  echo Python was not found. Install Python 3.12 or newer from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" in the installer, then run setup.bat again.
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtual environment...
  python -m venv .venv
  if errorlevel 1 exit /b 1
)

echo Installing packages...
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo Package installation failed - check your internet connection and run setup.bat again.
  exit /b 1
)

if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo.
  echo Created .env - Notepad will open it now. Fill in:
  echo    ANTHROPIC_API_KEY   - from https://console.anthropic.com  (API Keys)
  echo    PUAP_MCP_URL        - the admissions MCP server URL (+ PUAP_MCP_TOKEN if it has one)
  echo    TELEGRAM_BOT_TOKEN  - from @BotFather in Telegram  (/newbot)
  echo then save and close Notepad.
  start "" notepad ".env"
) else (
  echo .env already exists - leaving it alone.
)

echo.
echo Setup complete.
echo   run.bat    - start the bot
echo   users.bat  - enrol people (their Telegram ID / WhatsApp number)
endlocal
