@echo off
rem Build single-file exes for non-Python users (PyInstaller).
rem Data stays OUTSIDE the exe: static\, dict.sqlite and textractor\ must sit
rem next to DownTheRabbitHole.exe (the server finds them via sys.frozen).
rem Ship: DownTheRabbitHole.exe + RabbitHoleSetup.exe in one folder; the user
rem runs RabbitHoleSetup.exe once (downloads everything), then the app exe.
cd /d "%~dp0"

python -m PyInstaller --version >nul 2>nul || python -m pip install pyinstaller
if errorlevel 1 exit /b 1

python -m PyInstaller --noconfirm --onefile --noconsole --name DownTheRabbitHole server.py
if errorlevel 1 exit /b 1
python -m PyInstaller --noconfirm --onefile --name RabbitHoleSetup setup.py
if errorlevel 1 exit /b 1

copy /y dist\DownTheRabbitHole.exe . >nul
copy /y dist\RabbitHoleSetup.exe . >nul
echo.
echo Built DownTheRabbitHole.exe and RabbitHoleSetup.exe.
echo Distribute both together; run RabbitHoleSetup.exe once, then the app.
