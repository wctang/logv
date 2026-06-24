@ECHO OFF

setlocal
uv run "%~dpn0.py" %*
endlocal

pause
