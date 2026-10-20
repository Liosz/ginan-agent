@echo off
chcp 65001 >nul 2>&1
setlocal

REM ============================================================================
REM  Ginan Agent launcher - double-click this file.
REM
REM  ---------------------------------------------------------------------------
REM  THIS FILE MUST STAY PURE ASCII. NO EXCEPTIONS. NOT EVEN IN COMMENTS.
REM
REM  I wrote Chinese in this file once while the comment right above said
REM  "must stay pure ASCII". The script died immediately with a wall of
REM  "command not found" errors. Do not repeat that.
REM
REM  WHY: cmd.exe reads .bat files using the system ANSI codepage.
REM  On a Chinese Windows that is GBK. This file is UTF-8. So cmd decodes the
REM  UTF-8 bytes as GBK, the result is garbage, and the garbage gets run as
REM  COMMANDS. A harmless Chinese comment becomes several failed commands and
REM  the script aborts before doing anything useful.
REM
REM  It also can NOT be fixed by saving as GBK: then it breaks on every
REM  non-Chinese Windows. There is no encoding that works on all systems.
REM
REM  So the .bat stays ASCII and does the minimum. All Chinese messages come
REM  from Python (ginan_agent/bootstrap.py), which handles UTF-8 correctly.
REM  ---------------------------------------------------------------------------

cd /d "%~dp0"

set "PY="
for %%C in ("py -3" "python" "python3") do (
    if not defined PY (
        %%~C -c "import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
        if not errorlevel 1 set "PY=%%~C"
    )
)

if not defined PY (
    echo.
    echo   ============================================================
    echo    Python not found
    echo   ============================================================
    echo.
    echo    This program needs Python 3.10 or newer.
    echo.
    echo    1. Download from https://www.python.org/downloads/
    echo    2. IMPORTANT: check "Add Python to PATH" during install
    echo    3. Then double-click this file again
    echo.
    pause
    exit /b 1
)

REM  ---------------------------------------------------------------------------
REM  Ctrl+C must NOT leave the user staring at "Terminate batch job (Y/N)?".
REM
REM  That question comes from cmd.exe, not from Python. Python already catches
REM  Ctrl+C, prints its goodbye and exits 0 -- and THEN cmd asks whether to
REM  abort the rest of this .bat. So quitting took two keystrokes instead of one.
REM
REM  The fix: put a no-op CALL on the SAME command line, joined with "&".
REM  cmd handles an interruption one COMMAND LINE at a time, so the CALL is part
REM  of the interrupted line and cmd never gets the chance to ask. The "&" is
REM  what does the work -- a CALL on its own line does not help.
REM
REM  "if errorlevel 1" rather than "%ERRORLEVEL%": the percent form is expanded
REM  when the line is PARSED (before Python even starts), so it would always see
REM  the old value. "if errorlevel" is evaluated when the line RUNS.
REM  ---------------------------------------------------------------------------

%PY% "ginan_agent\bootstrap.py" & call :after
exit /b %RC%

:after
set "RC=0"
if errorlevel 1 set "RC=1"
if not "%RC%"=="0" pause
exit /b 0
