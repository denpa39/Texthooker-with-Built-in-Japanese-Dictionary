@echo off
REM Starts Down the Rabbit Hole with no console window - just the app.
REM (pythonw = console-less Python; startup errors appear as a message box.)
REM First run: downloads the dictionary and tools via setup.py automatically.
if not exist "%~dp0dict.sqlite" (
  echo First run - setting up ^(downloads the dictionary and tools, ~250 MB^)...
  python "%~dp0setup.py" || (pause & exit /b 1)
)
start "" pythonw "%~dp0server.py" %*
