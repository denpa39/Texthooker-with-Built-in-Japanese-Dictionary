@echo off
REM One-time setup: downloads the tokenizer + JMdict and builds the dictionary DB.
python "%~dp0setup.py" %*
echo.
pause
