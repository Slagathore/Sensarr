@echo off
setlocal

echo ============================================
echo  Sensarr - Autostart Setup
echo ============================================
echo.

:: Get the directory this script lives in (the project root)
set "SCRIPT_DIR=%~dp0"
:: Remove trailing backslash
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "LAUNCHER=%SCRIPT_DIR%\start_sensarr.bat"

echo Project directory: %SCRIPT_DIR%
echo.

:: --------------------------------------------------------------------------
:: The task runs start_sensarr.bat, which picks the NEWEST staged build under
:: dist\ at the moment of logon (falling back to the source tree). The task
:: itself never stores an EXE path, so a rebuild needs no re-run of this
:: script and a stale build can never be started by mistake.
:: --------------------------------------------------------------------------
if not exist "%LAUNCHER%" (
    echo ERROR: %LAUNCHER% is missing.
    pause
    exit /b 1
)

echo Right now the launcher would start:
call "%LAUNCHER%" --print
echo.

:: Clear autostart entries left by the app's old names so only one survives.
schtasks /delete /tn "Plexxarr" /f >nul 2>nul
schtasks /delete /tn "PlexResetButton" /f >nul 2>nul

:: Recreate the task (/f overwrites any existing one).
schtasks /create ^
  /tn "Sensarr" ^
  /tr "\"%LAUNCHER%\"" ^
  /sc onlogon ^
  /rl highest ^
  /f

if %errorlevel% neq 0 (
    echo.
    echo ERROR: Failed to create scheduled task. Try running this script as Administrator.
    pause
    exit /b 1
)

echo.
echo ============================================
echo  Done! Sensarr will start at logon from
echo  whichever staged build is newest at that
echo  moment. Rebuild + stage (update_sensarr.bat)
echo  and it is picked up automatically.
echo.
echo  To remove: run remove_autostart.bat
echo ============================================
pause
endlocal
