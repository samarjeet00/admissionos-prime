@echo off
cd /d "%~dp0"
set "CF=cloudflared"
where cloudflared >nul 2>&1
if errorlevel 1 (
  if exist "%ProgramFiles(x86)%\cloudflared\cloudflared.exe" set "CF=%ProgramFiles(x86)%\cloudflared\cloudflared.exe"
  if exist "%ProgramFiles%\cloudflared\cloudflared.exe" set "CF=%ProgramFiles%\cloudflared\cloudflared.exe"
)
"%CF%" --version >nul 2>&1
if errorlevel 1 (
  echo cloudflared not found - install it with:  winget install --id Cloudflare.cloudflared -e
  exit /b 1
)
echo Starting a public HTTPS tunnel to the WhatsApp webhook on port 8765...
echo Look for the line "https://xxxx.trycloudflare.com" below - that + /webhook is the Callback URL for Meta.
echo (Quick tunnels get a new URL every start. For a permanent URL use a named tunnel or host on a server.)
"%CF%" tunnel --url http://localhost:8765
