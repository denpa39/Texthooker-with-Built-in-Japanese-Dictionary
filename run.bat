@echo off
REM Starts Down the Rabbit Hole and opens it in your browser.
python "%~dp0server.py" %*
REM Keep the window open only if the server failed (e.g. missing dict.sqlite),
REM so the error stays readable; a clean quit closes the terminal too.
if errorlevel 1 pause
