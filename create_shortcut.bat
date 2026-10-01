@echo off
REM Creates a "Camera Viewer" icon on the Desktop that starts the app with a double-click.
set "HERE=%~dp0"
for /f "delims=" %%i in ('python -c "import sys,os; print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))"') do set "PYW=%%i"
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\Camera Viewer.lnk'); $s.TargetPath='%PYW%'; $s.Arguments='\"%HERE%start.pyw\"'; $s.WorkingDirectory='%HERE%'; $s.Save()"
echo Done - look for "Camera Viewer" on the Desktop.
pause
