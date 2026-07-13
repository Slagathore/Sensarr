@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "PYINSTALLER_CHECK_LOG=%SCRIPT_DIR%\pyinstaller_check.log"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "BUILD_STAMP=%%i"
set "DIST_ROOT=%SCRIPT_DIR%\dist\%BUILD_STAMP%"
set "WORK_ROOT=%SCRIPT_DIR%\build\%BUILD_STAMP%"

echo ============================================
echo  Plexxarr - Windows EXE Build
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

python -m PyInstaller --noconfirm --clean --distpath "%DIST_ROOT%" --workpath "%WORK_ROOT%" "%SCRIPT_DIR%\Plexxarr.spec"
if %errorlevel% neq 0 (
    echo.
    echo ERROR: Build failed.
    pause
    exit /b 1
)

:: --------------------------------------------------------------------------
:: Seed the Node torrent runner NEXT TO the freshly built EXE and install its
:: dependencies. node_modules must sit beside download.mjs (Node resolves it
:: from the script's folder), and PyInstaller only bundles the scripts under
:: _internal, so we copy them out and npm install here. This makes every build
:: self-sufficient for the Downloads pipeline (Node.js must be on PATH).
:: --------------------------------------------------------------------------
set "EXE_RUNNER=%DIST_ROOT%\Plexxarr\torrent_runner"
where node >nul 2>nul
if %errorlevel% neq 0 (
    echo.
    echo NOTE: Node.js not found on PATH — skipping torrent runner setup.
    echo       The app runs fine, but the Downloads pipeline needs Node.js.
    echo       Install Node 20+, then run: cd "%EXE_RUNNER%" ^&^& npm install
) else (
    echo.
    echo Setting up the torrent runner next to the EXE...
    if not exist "%EXE_RUNNER%" mkdir "%EXE_RUNNER%"
    copy /y "%SCRIPT_DIR%\torrent_runner\download.mjs"       "%EXE_RUNNER%\" >nul
    copy /y "%SCRIPT_DIR%\torrent_runner\diag.mjs"           "%EXE_RUNNER%\" >nul
    copy /y "%SCRIPT_DIR%\torrent_runner\package.json"       "%EXE_RUNNER%\" >nul
    copy /y "%SCRIPT_DIR%\torrent_runner\package-lock.json"  "%EXE_RUNNER%\" >nul
    pushd "%EXE_RUNNER%"
    call npm install --no-audit --no-fund
    popd
)

:: Delete PyInstaller's scratch dir — it contains a half-built Plexxarr.exe
:: stub that fails with "Failed to load Python DLL" if launched by mistake,
:: and it wastes a few hundred MB per build. Only dist\ matters.
rmdir /s /q "%WORK_ROOT%" >nul 2>nul

:: --------------------------------------------------------------------------
:: Wrap the fresh onedir bundle in a Windows installer (packaging\installer.iss)
:: if Inno Setup is on this machine. Skipped silently otherwise -- the folder
:: bundle and portable exe still work without it.
:: --------------------------------------------------------------------------
set "ISCC=C:\Users\Cole\AppData\Local\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
    echo.
    echo NOTE: Inno Setup ^(ISCC.exe^) not found -- skipping installer build.
    echo       Folder bundle and portable exe are unaffected.
) else (
    setlocal EnableDelayedExpansion
    set "APP_VERSION="
    for /f "usebackq delims=" %%v in (`powershell -NoProfile -Command "(Select-String -Path '%SCRIPT_DIR%\config.py' -Pattern '^APP_VERSION').Line -replace '.*=\s*\u0022([^\u0022]+)\u0022.*', '$1'"`) do set "APP_VERSION=%%v"
    if "!APP_VERSION!"=="" (
        echo.
        echo NOTE: Could not read APP_VERSION from config.py -- skipping installer build.
    ) else (
        echo.
        echo Building installer for version !APP_VERSION!...
        "%ISCC%" "/DAppVersion=!APP_VERSION!" "/DSourceDir=%DIST_ROOT%\Plexxarr" "%SCRIPT_DIR%\packaging\installer.iss"
        if errorlevel 1 (
            echo.
            echo WARNING: Installer build failed. Folder bundle and portable exe are unaffected.
        ) else (
            echo.
            echo Installer: %SCRIPT_DIR%\packaging\Output\Plexxarr-!APP_VERSION!-Setup.exe
            echo Sign it per SIGNING.md before shipping.
        )
    )
    endlocal
)

echo.
echo Build complete.
echo Executable bundle: %DIST_ROOT%\Plexxarr
echo Run setup_autostart.bat to point logon-autostart at this new build.
pause
endlocal
