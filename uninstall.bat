@echo off
setlocal

echo Universal Cam Viewer - Uninstaller
echo ------------------------------------
set "APPDATA_DIR=%APPDATA%\UniversalCamViewer"

if exist "%APPDATA_DIR%" (
    echo Removing saved device list at %APPDATA_DIR% ...
    rmdir /s /q "%APPDATA_DIR%"
) else (
    echo No saved data found.
)

echo.
set /p CONFIRM="Delete UniversalCamViewer.exe from this folder too? (Y/N): "
if /i "%CONFIRM%"=="Y" (
    del /f /q "%~dp0UniversalCamViewer.exe" 2>nul
    echo Removed.
)

echo Uninstall complete.
pause
