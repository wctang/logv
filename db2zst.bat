@echo off
setlocal disableDelayedExpansion

:loop_args
if "%~1"=="" (
  goto end
) else if /I "%~x1"==".db-shm" (
  goto pass
) else if /I "%~x1"==".db-wal" (
  goto pass
) else if /I "%~x1"==".db" (
  call :rundb "%~1"
) else (
  call :other "%~1"
)
shift
goto loop_args

:pass
echo Pass "%~1"
shift
goto loop_args


:rundb
setlocal
set "DB_OK="
for /f "usebackq delims=" %%A in (`^""%~dp0sqlite3.exe" "%~1" "PRAGMA quick_check;"^" 2^>nul`) do (
  if "%%A"=="ok" set "DB_OK=1"
)

if "%DB_OK%"=="1" (
  "%~dp0sqlite3.exe" "%~1" "SELECT * FROM log_raw" | "%~dp0zstd.exe" -T0 -10 -f -o "%~dpn1.log.zst"
  "%~dp0zstd.exe" "%~1" -T0 -10 --rm -o "%~dpn1.db.zst"
) else (
  echo [WARN] "%~nx1" is corrupted. Recovering with .recover ...
  if exist "%~dpn1-fix.db" del /f /q "%~dpn1-fix.db"
  "%~dp0sqlite3.exe" "%~1" ".recover" | "%~dp0sqlite3.exe" "%~dpn1-fix.db"
  if exist "%~dpn1-fix.db" (
    "%~dp0sqlite3.exe" "%~dpn1-fix.db" "SELECT * FROM log_raw" | "%~dp0zstd.exe" -T0 -10 -f -o "%~dpn1.log.zst"
    del /f /q "%~dpn1-fix.db"
  ) else (
    echo [ERROR] Failed to recover "%~nx1".
  )
  "%~dp0zstd.exe" "%~1" -T0 -10 --rm -o "%~dpn1.db.zst"
)
exit /b

:other
setlocal
"%~dp0zstd.exe" "%~1" -T0 -10 --rm -o "%~1.zst"
exit /b

:end
pause
