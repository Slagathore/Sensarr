@echo off
echo Removing PlexResetButton from scheduled tasks...
schtasks /delete /tn "PlexResetButton" /f
if %errorlevel% neq 0 (
    echo Task not found or could not be removed.
) else (
    echo Done. PlexResetButton will no longer start on login.
)
pause
