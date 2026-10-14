@echo off
REM Builds KeithleyIV.exe from keithley2400_gui.pyw - double-click to run.
cd /d "%~dp0"

echo Installing / updating build tools...
py -m pip install --user --upgrade pyinstaller pyvisa numpy matplotlib
if errorlevel 1 goto fail

echo.
echo Building KeithleyIV.exe (takes 1-3 minutes)...
py -m PyInstaller --noconfirm --clean --onefile --windowed --name KeithleyIV ^
   --collect-all pyvisa keithley2400_gui.pyw
if errorlevel 1 goto fail

echo.
echo ============================================================
echo  Done!  Your program is:  dist\KeithleyIV.exe
echo  You can copy it anywhere (e.g. the Desktop).
echo ============================================================
explorer dist
pause
exit /b 0

:fail
echo.
echo Build failed - take a photo of this window and send it to Claude.
pause
exit /b 1
