@echo off
echo Removing Plexxarr from scheduled tasks...
schtasks /delete /tn "Plexxarr" /f
schtasks /delete /tn "PlexResetButton" /f 2>nul
if %errorlevel% neq 0 (
    echo Task not found or could not be removed.
) else (
    echo Done. Plexxarr will no longer start on login.
)
pause
