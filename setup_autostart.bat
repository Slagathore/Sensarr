@echo off
setlocal EnableDelayedExpansion

echo ============================================
echo  Plexxarr - Autostart Setup
echo ============================================
echo.

:: Get the directory this script lives in (the project root)
set "SCRIPT_DIR=%~dp0"
:: Remove trailing backslash
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "MAIN_PY=%SCRIPT_DIR%\main.py"

echo Project directory: %SCRIPT_DIR%
echo.

:: --------------------------------------------------------------------------
:: Find the NEWEST built EXE. Builds land in dist\<timestamp>\PlexResetButton\
:: (timestamp names sort chronologically), so the last match is the newest.
:: A legacy build at dist\PlexResetButton\ is used only if no timestamped
:: build exists. This means every rebuild is picked up automatically and you
:: never accidentally auto-start a stale binary.
:: --------------------------------------------------------------------------
set "EXE_PATH="
for /f "delims=" %%D in ('dir /b /ad /on "%SCRIPT_DIR%\dist" 2^>nul') do (
    if exist "%SCRIPT_DIR%\dist\%%D\PlexResetButton\PlexResetButton.exe" (
        set "EXE_PATH=%SCRIPT_DIR%\dist\%%D\PlexResetButton\PlexResetButton.exe"
    )
    if exist "%SCRIPT_DIR%\dist\%%D\Plexxarr\Plexxarr.exe" (
        set "EXE_PATH=%SCRIPT_DIR%\dist\%%D\Plexxarr\Plexxarr.exe"
    )
)
if not defined EXE_PATH (
    if exist "%SCRIPT_DIR%\dist\PlexResetButton\PlexResetButton.exe" (
        set "EXE_PATH=%SCRIPT_DIR%\dist\PlexResetButton\PlexResetButton.exe"
    )
)

if defined EXE_PATH (
    set "TASK_TARGET=\"!EXE_PATH!\""
    echo Found packaged executable at: !EXE_PATH!
) else (
    :: No build found — fall back to running the source with pythonw (no console).
    set "PYTHONW="
    for /f "tokens=*" %%i in ('where pythonw.exe 2^>nul') do (
        if not defined PYTHONW set "PYTHONW=%%i"
    )
    if not defined PYTHONW (
        echo ERROR: No built EXE under dist\ and pythonw.exe not on PATH.
        echo Build first with build_exe.bat, or install Python and add it to PATH.
        pause
        exit /b 1
    )
    echo No built EXE found; using source via: !PYTHONW!
    set "TASK_TARGET=\"!PYTHONW!\" \"%MAIN_PY%\""
)

:: Recreate the task (/f overwrites any existing one so re-running this after
:: a new build repoints autostart at the newest binary).
schtasks /create ^
  /tn "Plexxarr" ^
  /tr "!TASK_TARGET!" ^
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
echo  Done! Plexxarr will start at logon
echo  from the newest build. Re-run this script
echo  after each rebuild to repoint autostart.
echo.
echo  To remove: run remove_autostart.bat
echo ============================================
pause
endlocal
