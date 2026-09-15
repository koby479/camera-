@echo off
REM Run this on Windows, inside a venv where requirements.txt is installed.
pip install -r requirements.txt

for /f "delims=" %%i in ('python -c "import onvif, os; print(os.path.join(os.path.dirname(onvif.__file__), 'wsdl'))"') do set ONVIF_WSDL=%%i
echo Using ONVIF wsdl dir: %ONVIF_WSDL%

pyinstaller --noconfirm --onefile --windowed ^
    --name "UniversalCamViewer" ^
    --collect-all numpy ^
    --collect-all cv2 ^
    --collect-all onvif ^
    --add-data "%ONVIF_WSDL%;onvif\wsdl" ^
    --add-data "app;app" ^
    app\main.py
echo.
echo Done. EXE is in dist\UniversalCamViewer.exe
pause
