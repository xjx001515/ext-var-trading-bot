@echo off
setlocal EnableExtensions
set "EDGE_EXE=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
if not exist "%EDGE_EXE%" set "EDGE_EXE=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
if not exist "%EDGE_EXE%" (
  echo Edge not found.
  pause
  exit /b 1
)
set "USER_DATA_DIR=%~dp0edge_profile"
if not exist "%USER_DATA_DIR%" md "%USER_DATA_DIR%" >nul 2>nul
set "VAR_URL=https://omni.variational.io/perpetual/ZEC"
set "EXT_URL=https://app.extended.exchange/perp"
start "BOT_EDGE" "%EDGE_EXE%" --remote-debugging-port=9222 --user-data-dir="%USER_DATA_DIR%" --lang=en-US --accept-lang=en-US --no-first-run --no-default-browser-check "%VAR_URL%" "%EXT_URL%"