@echo off
setlocal EnableExtensions

rem Keep this batch ASCII-only to avoid mojibake/parsing issues on Chinese Windows cmd.

set "BASE_DIR=%~dp0"
cd /d "%BASE_DIR%"

call :make_ts
set "LOG_DIR=%BASE_DIR%_env_setup_logs"
if not exist "%LOG_DIR%" md "%LOG_DIR%" >nul 2>nul
set "LOG_FILE=%LOG_DIR%\setup_%TS%.log"

set "PY_MAJOR_MINOR=3.14"
set "PY_FULL_VER=3.14.4"
set "PY_URL=https://www.python.org/ftp/python/3.14.4/python-3.14.4-amd64.exe"
set "PY_INSTALLER=%TEMP%\python-3.14.4-amd64.exe"
set "CHROME_MSI_URL=https://dl.google.com/edgedl/chrome/install/GoogleChromeStandaloneEnterprise64.msi"
set "CHROME_MSI=%TEMP%\GoogleChromeStandaloneEnterprise64.msi"
set "VENV_DIR=%BASE_DIR%.venv"
set "REQS=playwright psutil termcolor numpy pandas matplotlib python-docx"
set "PYTHON_EXE="
set "VENV_PY="
set "CHROME_EXE="
set "GUIDE_TXT=%BASE_DIR%manual_setup_guide.txt"
set "AUTO_TXT=%BASE_DIR%auto_install_list.txt"
set "LAUNCHER_BAT=%BASE_DIR%start_bot_chrome.bat"
set "VAR_URL=https://omni.variational.io/perpetual/ZEC"
set "EXT_URL=https://app.extended.exchange/join/NORTHAND"
set "CDP_OK="

call :log ==============================================
call :log BOTV5 env setup started (Python %PY_FULL_VER%)
call :log BASE_DIR=%BASE_DIR%
call :log LOG_FILE=%LOG_FILE%
call :log ==============================================

echo.
echo [1/9] Check Python...
call :find_python
if not defined PYTHON_EXE (
    call :log Python %PY_FULL_VER% not found, installing Python %PY_FULL_VER%
    call :install_python
    call :find_python
)
if not defined PYTHON_EXE goto :python_fail
call :log PYTHON_EXE=%PYTHON_EXE%
"%PYTHON_EXE%" -V >> "%LOG_FILE%" 2>&1

echo [2/9] Create venv...
if not exist "%VENV_DIR%\Scripts\python.exe" (
    "%PYTHON_EXE%" -m venv "%VENV_DIR%" >> "%LOG_FILE%" 2>&1
)
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
if not exist "%VENV_PY%" goto :venv_fail
call :log VENV_PY=%VENV_PY%

echo [3/9] Upgrade pip/setuptools/wheel...
"%VENV_PY%" -m pip install --upgrade pip setuptools wheel >> "%LOG_FILE%" 2>&1
if errorlevel 1 goto :pip_fail

echo [4/9] Install Python packages...
"%VENV_PY%" -m pip install --upgrade %REQS% >> "%LOG_FILE%" 2>&1
if errorlevel 1 goto :deps_fail

echo [5/9] Install Playwright Chromium...
"%VENV_PY%" -m playwright install chromium >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    call :log Playwright Chromium install failed. Manual retry: .venv\Scripts\python.exe -m playwright install chromium
) else (
    call :log Playwright Chromium install ok
)

echo [6/9] Check Google Chrome...
call :find_chrome
if not defined CHROME_EXE (
    call :log Chrome not found, try install
    call :install_chrome
    call :find_chrome
)
if defined CHROME_EXE (
    call :log CHROME_EXE=%CHROME_EXE%
) else (
    call :log Chrome install not completed automatically
)

echo [7/9] Write helper files...
call :write_launcher
call :write_text_files
> "%GUIDE_TXT%" echo BOTV5 manual guide
>> "%GUIDE_TXT%" echo ==============================
>> "%GUIDE_TXT%" echo Auto-installed:
>> "%GUIDE_TXT%" echo 1. Python 3.14.x
>> "%GUIDE_TXT%" echo 2. .venv
>> "%GUIDE_TXT%" echo 3. pip / setuptools / wheel
>> "%GUIDE_TXT%" echo 4. playwright
>> "%GUIDE_TXT%" echo 5. psutil
>> "%GUIDE_TXT%" echo 6. termcolor
>> "%GUIDE_TXT%" echo 7. numpy
>> "%GUIDE_TXT%" echo 8. pandas
>> "%GUIDE_TXT%" echo 9. matplotlib
>> "%GUIDE_TXT%" echo 10. python-docx
>> "%GUIDE_TXT%" echo 11. Playwright Chromium
>> "%GUIDE_TXT%" echo 12. Google Chrome
>> "%GUIDE_TXT%" echo.
>> "%GUIDE_TXT%" echo Manual steps:
>> "%GUIDE_TXT%" echo 1. EXT / VAR login is manual.
>> "%GUIDE_TXT%" echo 2. Captcha / MFA / risk checks are manual.
>> "%GUIDE_TXT%" echo 3. If download is blocked by policy, run this bat as administrator.
>> "%GUIDE_TXT%" echo 4. After setup, this script auto-runs start_bot_chrome.bat.
>> "%GUIDE_TXT%" echo 5. It auto-opens VAR and EXT target pages.
>> "%GUIDE_TXT%" echo 6. It auto-checks localhost:9222.
>> "%GUIDE_TXT%" echo 7. Then login and complete captcha / MFA / risk checks before running the bot.
>> "%GUIDE_TXT%" echo 8. If Playwright browser install failed, run: .venv\Scripts\python.exe -m playwright install chromium
>> "%GUIDE_TXT%" echo.
>> "%GUIDE_TXT%" echo This one-click setup solves:
>> "%GUIDE_TXT%" echo 1. Missing or wrong Python version.
>> "%GUIDE_TXT%" echo 2. Missing virtual environment.
>> "%GUIDE_TXT%" echo 3. Missing pip / setuptools / wheel.
>> "%GUIDE_TXT%" echo 4. Missing Python packages required by bot.
>> "%GUIDE_TXT%" echo 5. Missing Playwright Chromium.
>> "%GUIDE_TXT%" echo 6. Missing Google Chrome for remote debugging.
>> "%GUIDE_TXT%" echo 7. Repeated manual environment setup on a new Windows machine.
>> "%GUIDE_TXT%" echo 8. Forgot to launch Chrome debug mode or forgot to open the two target pages.
> "%AUTO_TXT%" echo BOTV5 auto install list
>> "%AUTO_TXT%" echo ==============================
>> "%AUTO_TXT%" echo Python 3.14.x
>> "%AUTO_TXT%" echo .venv
>> "%AUTO_TXT%" echo pip / setuptools / wheel
>> "%AUTO_TXT%" echo playwright
>> "%AUTO_TXT%" echo psutil
>> "%AUTO_TXT%" echo termcolor
>> "%AUTO_TXT%" echo numpy
>> "%AUTO_TXT%" echo pandas
>> "%AUTO_TXT%" echo matplotlib
>> "%AUTO_TXT%" echo python-docx
>> "%AUTO_TXT%" echo Playwright Chromium
>> "%AUTO_TXT%" echo Google Chrome
call :log Wrote guide files
goto :eof
:make_ts
set "TS="
where powershell >nul 2>nul
if not errorlevel 1 (
    for /f "usebackq delims=" %%I in (`powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"`) do set "TS=%%I"
)
if defined TS goto :eof
for /f "skip=1 delims=" %%I in ('wmic os get localdatetime 2^>nul') do if not defined TS set "TS=%%I"
if defined TS (
    set "TS=%TS:~0,8%_%TS:~8,6%"
    goto :eof
)
set "TS=%RANDOM%%RANDOM%"
goto :eof

:find_python
set "PYTHON_EXE="
for %%P in (
    "%LocalAppData%\Programs\Python\Python314\python.exe"
    "%ProgramFiles%\Python314\python.exe"
    "%ProgramFiles(x86)%\Python314\python.exe"
) do (
    if exist %%~P (
        set "PYTHON_EXE=%%~P"
        goto :eof
    )
)
where py >nul 2>nul
if not errorlevel 1 (
    py -3.14 -V >nul 2>nul && (
        for /f "delims=" %%I in ('py -3.14 -c "import sys;print(sys.executable)" 2^>nul') do set "PYTHON_EXE=%%I"
        if defined PYTHON_EXE goto :eof
    )
)
where python >nul 2>nul
if not errorlevel 1 (
    set "PYTHON_CANDIDATE="
    set "PYTHON_MM="
    for /f "delims=" %%I in ('python -c "import sys;print(sys.executable)" 2^>nul') do set "PYTHON_CANDIDATE=%%I"
    for /f "delims=" %%V in ('python -c "import sys;print(str(sys.version_info[0])+'.'+str(sys.version_info[1]))" 2^>nul') do set "PYTHON_MM=%%V"
)
if /I "%PYTHON_MM%"=="%PY_MAJOR_MINOR%" set "PYTHON_EXE=%PYTHON_CANDIDATE%"
goto :eof

:install_python
where winget >nul 2>nul
if not errorlevel 1 (
    call :log Try winget install Python %PY_MAJOR_MINOR%
    winget install --id Python.Python.3.14 -e --accept-package-agreements --accept-source-agreements --silent >> "%LOG_FILE%" 2>&1
    timeout /t 3 >nul
    call :find_python
    if defined PYTHON_EXE goto :eof
)
call :log Winget Python failed, use python.org installer %PY_FULL_VER%
call :download_file "%PY_URL%" "%PY_INSTALLER%"
if not exist "%PY_INSTALLER%" goto :eof
call :unblock_file "%PY_INSTALLER%"
call :run_exe_installer "%PY_INSTALLER%" "/quiet InstallAllUsers=0 PrependPath=1 Include_test=0 Include_pip=1 Include_launcher=1 SimpleInstall=1"
call :find_python
goto :eof

:find_chrome
set "CHROME_EXE="
for %%C in (
    "%ProgramFiles%\Google\Chrome\Application\chrome.exe"
    "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
    "%LocalAppData%\Google\Chrome\Application\chrome.exe"
) do (
    if exist %%~C (
        set "CHROME_EXE=%%~C"
        goto :eof
    )
)
goto :eof

:install_chrome
where winget >nul 2>nul
if not errorlevel 1 (
    call :log Try winget install Chrome
    winget install --id Google.Chrome -e --accept-package-agreements --accept-source-agreements --silent >> "%LOG_FILE%" 2>&1
    timeout /t 3 >nul
    call :find_chrome
    if defined CHROME_EXE goto :eof
)
call :log Winget Chrome failed, use MSI installer
call :download_file "%CHROME_MSI_URL%" "%CHROME_MSI%"
if not exist "%CHROME_MSI%" goto :eof
call :unblock_file "%CHROME_MSI%"
call :run_msi "%CHROME_MSI%"
timeout /t 3 >nul
call :find_chrome
goto :eof

:download_file
set "DL_URL=%~1"
set "DL_OUT=%~2"
call :log Download %DL_URL% -> %DL_OUT%
if exist "%DL_OUT%" del /f /q "%DL_OUT%" >nul 2>nul
where curl >nul 2>nul
if not errorlevel 1 (
    curl.exe -L --fail --output "%DL_OUT%" "%DL_URL%" >> "%LOG_FILE%" 2>&1
    if exist "%DL_OUT%" goto :eof
)
where powershell >nul 2>nul
if errorlevel 1 goto :eof
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%DL_URL%' -OutFile '%DL_OUT%'" >> "%LOG_FILE%" 2>&1
goto :eof

:unblock_file
set "UB_FILE=%~1"
if not exist "%UB_FILE%" goto :eof
where powershell >nul 2>nul
if errorlevel 1 goto :eof
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Unblock-File -Path '%UB_FILE%' -ErrorAction SilentlyContinue } catch {}" >> "%LOG_FILE%" 2>&1
goto :eof

:run_exe_installer
set "RUN_FILE=%~1"
set "RUN_ARGS=%~2"
if not exist "%RUN_FILE%" goto :eof
call :log Run EXE installer: %RUN_FILE%
where powershell >nul 2>nul
if errorlevel 1 (
    "%RUN_FILE%" %RUN_ARGS% >> "%LOG_FILE%" 2>&1
    goto :eof
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p=Start-Process -FilePath '%RUN_FILE%' -ArgumentList '%RUN_ARGS%' -Wait -PassThru; exit $p.ExitCode" >> "%LOG_FILE%" 2>&1
goto :eof

:run_msi
set "MSI_FILE=%~1"
if not exist "%MSI_FILE%" goto :eof
call :log Run MSI installer: %MSI_FILE%
msiexec.exe /i "%MSI_FILE%" /qn /norestart >> "%LOG_FILE%" 2>&1
goto :eof

:write_launcher
> "%LAUNCHER_BAT%" echo @echo off
>> "%LAUNCHER_BAT%" echo setlocal EnableExtensions
>> "%LAUNCHER_BAT%" echo set "CHROME_EXE=C:\Program Files\Google\Chrome\Application\chrome.exe"
>> "%LAUNCHER_BAT%" echo if not exist "%%CHROME_EXE%%" set "CHROME_EXE=%%ProgramFiles(x86)%%\Google\Chrome\Application\chrome.exe"
>> "%LAUNCHER_BAT%" echo if not exist "%%CHROME_EXE%%" set "CHROME_EXE=%%LocalAppData%%\Google\Chrome\Application\chrome.exe"
>> "%LAUNCHER_BAT%" echo if not exist "%%CHROME_EXE%%" ^(
>> "%LAUNCHER_BAT%" echo   echo Chrome not found. Run env setup first.
>> "%LAUNCHER_BAT%" echo   pause
>> "%LAUNCHER_BAT%" echo   exit /b 1
>> "%LAUNCHER_BAT%" echo ^)
>> "%LAUNCHER_BAT%" echo set "USER_DATA_DIR=%%~dp0chrome_profile"
>> "%LAUNCHER_BAT%" echo if not exist "%%USER_DATA_DIR%%" md "%%USER_DATA_DIR%%" ^>nul 2^>nul
>> "%LAUNCHER_BAT%" echo set "VAR_URL=https://omni.variational.io/perpetual/ZEC"
>> "%LAUNCHER_BAT%" echo set "EXT_URL=https://app.extended.exchange/join/NORTHAND"
>> "%LAUNCHER_BAT%" echo start "BOT_CHROME" "%%CHROME_EXE%%" --remote-debugging-port=9222 --user-data-dir="%%USER_DATA_DIR%%" --no-first-run --no-default-browser-check "%%VAR_URL%%" "%%EXT_URL%%"
call :log Wrote launcher: %LAUNCHER_BAT%
goto :eof

:launch_browser_and_verify
set "CDP_OK="
if not exist "%LAUNCHER_BAT%" (
    call :log Launcher missing, skip auto launch
    goto :eof
)
call :log Auto launch browser via %LAUNCHER_BAT%
call "%LAUNCHER_BAT%" >> "%LOG_FILE%" 2>&1
call :verify_cdp_9222
if /I "%CDP_OK%"=="1" (
    call :log CDP 9222 verified ok
    echo [OK] Chrome launched and 9222 verified.
) else (
    call :log CDP 9222 not confirmed automatically
    echo [WARN] Chrome launched, but 9222 was not confirmed automatically.
)
goto :eof

:verify_cdp_9222
set "CDP_OK="
where powershell >nul 2>nul
if not errorlevel 1 (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$ok=$false; $deadline=(Get-Date).AddSeconds(30); while((Get-Date) -lt $deadline){ try{ $r=Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:9222/json/version' -TimeoutSec 3; if($r.StatusCode -eq 200 -and $r.Content -match 'webSocketDebuggerUrl'){ $ok=$true; break } } catch {}; Start-Sleep -Seconds 1 }; if($ok){exit 0}else{exit 1}" >> "%LOG_FILE%" 2>&1
    if not errorlevel 1 (
        set "CDP_OK=1"
        goto :eof
    )
)
for /l %%N in (1,1,10) do (
    netstat -ano | findstr /R /C:":9222 .*LISTENING" >nul 2>nul && set "CDP_OK=1"
    if defined CDP_OK goto :eof
    timeout /t 1 >nul
)
goto :eof

:write_text_files
> "%GUIDE_TXT%" echo BOTV5 manual guide
>> "%GUIDE_TXT%" echo ==============================
>> "%GUIDE_TXT%" echo Auto-installed:
>> "%GUIDE_TXT%" echo 1. Python 3.14.x
>> "%GUIDE_TXT%" echo 2. .venv
>> "%GUIDE_TXT%" echo 3. pip / setuptools / wheel
>> "%GUIDE_TXT%" echo 4. playwright
>> "%GUIDE_TXT%" echo 5. psutil
>> "%GUIDE_TXT%" echo 6. termcolor
>> "%GUIDE_TXT%" echo 7. numpy
>> "%GUIDE_TXT%" echo 8. pandas
>> "%GUIDE_TXT%" echo 9. matplotlib
>> "%GUIDE_TXT%" echo 10. python-docx
>> "%GUIDE_TXT%" echo 11. Playwright Chromium
>> "%GUIDE_TXT%" echo 12. Google Chrome
>> "%GUIDE_TXT%" echo.
>> "%GUIDE_TXT%" echo Manual steps:
>> "%GUIDE_TXT%" echo 1. EXT / VAR login is manual.
>> "%GUIDE_TXT%" echo 2. Captcha / MFA / risk checks are manual.
>> "%GUIDE_TXT%" echo 3. If download is blocked by policy, run this bat as administrator.
>> "%GUIDE_TXT%" echo 4. After setup, this script auto-runs start_bot_chrome.bat.
>> "%GUIDE_TXT%" echo 5. It auto-opens VAR and EXT target pages.
>> "%GUIDE_TXT%" echo 6. It auto-checks localhost:9222.
>> "%GUIDE_TXT%" echo 7. Then login and complete captcha / MFA / risk checks before running the bot.
>> "%GUIDE_TXT%" echo 8. If Playwright browser install failed, run: .venv\Scripts\python.exe -m playwright install chromium
>> "%GUIDE_TXT%" echo.
>> "%GUIDE_TXT%" echo This one-click setup solves:
>> "%GUIDE_TXT%" echo 1. Missing or wrong Python version.
>> "%GUIDE_TXT%" echo 2. Missing virtual environment.
>> "%GUIDE_TXT%" echo 3. Missing pip / setuptools / wheel.
>> "%GUIDE_TXT%" echo 4. Missing Python packages required by bot.
>> "%GUIDE_TXT%" echo 5. Missing Playwright Chromium.
>> "%GUIDE_TXT%" echo 6. Missing Google Chrome for remote debugging.
>> "%GUIDE_TXT%" echo 7. Repeated manual environment setup on a new Windows machine.
>> "%GUIDE_TXT%" echo 8. Forgot to launch Chrome debug mode or forgot to open the two target pages.
> "%AUTO_TXT%" echo BOTV5 auto install list
>> "%AUTO_TXT%" echo ==============================
>> "%AUTO_TXT%" echo Python 3.14.x
>> "%AUTO_TXT%" echo .venv
>> "%AUTO_TXT%" echo pip / setuptools / wheel
>> "%AUTO_TXT%" echo playwright
>> "%AUTO_TXT%" echo psutil
>> "%AUTO_TXT%" echo termcolor
>> "%AUTO_TXT%" echo numpy
>> "%AUTO_TXT%" echo pandas
>> "%AUTO_TXT%" echo matplotlib
>> "%AUTO_TXT%" echo python-docx
>> "%AUTO_TXT%" echo Playwright Chromium
>> "%AUTO_TXT%" echo Google Chrome
call :log Wrote guide files
goto :eof

:log
set "MSG=%~1"
echo [%date% %time%] %MSG%
>> "%LOG_FILE%" echo [%date% %time%] %MSG%
goto :eof

:python_fail
echo.
echo [FAIL] Python install not completed.
echo See log: %LOG_FILE%
pause
exit /b 1

:venv_fail
echo.
echo [FAIL] venv creation failed.
echo See log: %LOG_FILE%
pause
exit /b 1

:pip_fail
echo.
echo [FAIL] pip/setuptools/wheel install failed.
echo See log: %LOG_FILE%
pause
exit /b 1

:deps_fail
echo.
echo [FAIL] Python package install failed.
echo See log: %LOG_FILE%
pause
exit /b 1
