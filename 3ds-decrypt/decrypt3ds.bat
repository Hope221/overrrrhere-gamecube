@echo off
rem Drag .3ds / .cci / .cia files or folders onto this file. With nothing dropped, it decrypts this folder.
cd /d "%~dp0"
if "%~1"=="" (
    python "%~dp0decrypt3ds.py" "%~dp0."
) else (
    python "%~dp0decrypt3ds.py" %*
)
pause
