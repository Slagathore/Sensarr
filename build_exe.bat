@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "PYINSTALLER_CHECK_LOG=%SCRIPT_DIR%\pyinstaller_check.log"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "BUILD_STAMP=%%i"
set "DIST_ROOT=%SCRIPT_DIR%\dist\%BUILD_STAMP%"
set "WORK_ROOT=%SCRIPT_DIR%\build\%BUILD_STAMP%"

echo ============================================
echo  PlexResetButton - Windows EXE Build
echo ============================================
echo.

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo ERROR: Could not find python on PATH.
    pause
    exit /b 1
)

python -m PyInstaller --version >"%PYINSTALLER_CHECK_LOG%" 2>&1
if %errorlevel% neq 0 (
    echo ERROR: PyInstaller preflight check failed.
    echo.
    type "%PYINSTALLER_CHECK_LOG%"
    echo.
    echo If the message says PyInstaller is not installed, run:
    echo   python -m pip install pyinstaller
    pause
    exit /b 1
)

del "%PYINSTALLER_CHECK_LOG%" >nul 2>nul

python -m PyInstaller --noconfirm --clean --distpath "%DIST_ROOT%" --workpath "%WORK_ROOT%" "%SCRIPT_DIR%\PlexResetButton.spec"
if %errorlevel% neq 0 (
    echo.
    echo ERROR: Build failed.
    pause
    exit /b 1
)

echo.
echo Build complete.
echo Executable bundle: %DIST_ROOT%\PlexResetButton
pause
endlocal
