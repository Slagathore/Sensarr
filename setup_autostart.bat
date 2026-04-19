@echo off
setlocal

echo ============================================
echo  PlexResetButton - Autostart Setup
echo ============================================
echo.

:: Get the directory this script lives in (the project root)
set "SCRIPT_DIR=%~dp0"
:: Remove trailing backslash
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

set "EXE_PATH=%SCRIPT_DIR%\dist\PlexResetButton\PlexResetButton.exe"
set "MAIN_PY=%SCRIPT_DIR%\main.py"

echo Project directory: %SCRIPT_DIR%
echo.

if exist "%EXE_PATH%" (
    set "TASK_TARGET=\"%EXE_PATH%\""
    echo Found packaged executable at: %EXE_PATH%
) else (
    :: Find pythonw.exe (runs Python without a console window)
    for /f "tokens=*" %%i in ('where pythonw.exe 2^>nul') do (
        set "PYTHONW=%%i"
        goto :found_python
    )

    echo ERROR: Could not find PlexResetButton.exe in dist\ or pythonw.exe on PATH.
    echo Build the executable first, or install Python and add it to PATH.
    pause
    exit /b 1
)

goto :create_task

:found_python
echo Found Python at: %PYTHONW%
set "TASK_TARGET=\"%PYTHONW%\" \"%MAIN_PY%\""

:create_task
:: Create the scheduled task
schtasks /create ^
  /tn "PlexResetButton" ^
  /tr %TASK_TARGET% ^
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
echo  Done! PlexResetButton will now start
echo  automatically each time you log in.
echo.
echo  To remove: run remove_autostart.bat
echo  or open Task Scheduler and delete
echo  the "PlexResetButton" task.
echo ============================================
pause
endlocal
