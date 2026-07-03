@echo off
REM Starts Down the Rabbit Hole with no console window — just the app.
REM (pythonw = console-less Python; startup errors appear as a message box.)
start "" pythonw "%~dp0server.py" %*
