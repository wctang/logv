@ECHO OFF

setlocal
"%~dp0\runtime\python.exe" "%~dpn0.py" %*
endlocal

pause
