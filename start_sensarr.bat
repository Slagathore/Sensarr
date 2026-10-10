@echo off
setlocal EnableDelayedExpansion

:: =========================================================================
::  start_sensarr.bat - launch the NEWEST staged build, resolved right now.
::
::  The logon task points here instead of at a fixed EXE path, so a rebuild
::  is picked up at the next logon without re-running setup_autostart.bat.
::  A fixed path is how an old build came back after a reboot on 2026-09-10.
::
::  Newest = the last timestamp-named folder under dist\ (YYYYMMDD-HHMMSS
::  sorts chronologically) that holds BOTH Sensarr\Sensarr.exe and a staged
::  .env next to it. A bundle without .env was never run through
::  stage_build.py, would open its own empty database, and is skipped.
::  With no staged bundle at all, the source tree runs via pythonw.
::
::  start_sensarr.bat --print   only prints what would launch (dry run).
:: =========================================================================

set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

set "EXE_PATH="
for /f "delims=" %%D in ('dir /b /ad /on "%SCRIPT_DIR%\dist" 2^>nul') do (
    set "STAMP_FIRST=%%D"
    set "STAMP_FIRST=!STAMP_FIRST:~0,1!"
    if "!STAMP_FIRST!" geq "0" if "!STAMP_FIRST!" leq "9" (
        if exist "%SCRIPT_DIR%\dist\%%D\Sensarr\Sensarr.exe" (
            if exist "%SCRIPT_DIR%\dist\%%D\Sensarr\.env" (
                set "EXE_PATH=%SCRIPT_DIR%\dist\%%D\Sensarr\Sensarr.exe"
            )
        )
    )
)

:: Shipped zip layout: the bundle folder sits right next to this script.
if not defined EXE_PATH (
    if exist "%SCRIPT_DIR%\Sensarr\Sensarr.exe" (
        set "EXE_PATH=%SCRIPT_DIR%\Sensarr\Sensarr.exe"
    )
)

if defined EXE_PATH (
    for %%F in ("!EXE_PATH!") do set "EXE_DIR=%%~dpF"
    if "%~1"=="--print" (
        echo !EXE_PATH!
        exit /b 0
    )
    start "" /d "!EXE_DIR!" "!EXE_PATH!"
    exit /b 0
)

:: No staged bundle: run from source.
set "PYTHONW="
for /f "tokens=*" %%i in ('where pythonw.exe 2^>nul') do (
    if not defined PYTHONW set "PYTHONW=%%i"
)
if not defined PYTHONW (
    echo ERROR: no staged build under dist\ and pythonw.exe is not on PATH.
    exit /b 1
)
if "%~1"=="--print" (
    echo "!PYTHONW!" "%SCRIPT_DIR%\main.py"
    exit /b 0
)
start "" /d "%SCRIPT_DIR%" "!PYTHONW!" "%SCRIPT_DIR%\main.py"
exit /b 0
