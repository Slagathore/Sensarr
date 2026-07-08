@echo off
:: =========================================================================
::  update_plexxarr.bat — the one file to run after code changes.
::  Builds a fresh EXE bundle, then stages it (copies .env, pins the shared
::  database + staging folder, seeds offline caches). When it finishes, just
::  launch the printed EXE. Run setup_autostart.bat (elevated) afterwards if
::  you want logon autostart repointed at the new build.
:: =========================================================================
setlocal
set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

echo ============================================
echo  Plexxarr - Build ^& Stage
echo ============================================

:: Feed a keypress to build_exe.bat's trailing pause so this runs unattended.
echo. | cmd /c "%SCRIPT_DIR%\build_exe.bat"
if errorlevel 1 (
    echo Build failed - see output above.
    pause
    exit /b 1
)

python "%SCRIPT_DIR%\stage_build.py"
if errorlevel 1 (
    echo Staging failed - see output above.
    pause
    exit /b 1
)

echo.
echo All done. Close the running app, launch the EXE printed above.
pause
endlocal
