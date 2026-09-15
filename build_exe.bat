@echo off
REM Run this on Windows, inside a venv where requirements.txt is installed.
pip install -r requirements.txt
pyinstaller --noconfirm --onefile --windowed ^
    --name "UniversalCamViewer" ^
    --add-data "app;app" ^
    app\main.py
echo.
echo Done. EXE is in dist\UniversalCamViewer.exe
pause
