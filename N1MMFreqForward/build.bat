@echo off
REM ============================================================================
REM N1MM Frequency Forwarder build script (Windows).
REM
REM Produces a single-file .exe in dist\N1MMFreqForward.exe with no console
REM window. Run from a Command Prompt in this directory.
REM
REM First-time setup:
REM   1. Install Python 3.11+ from python.org (check "Add to PATH")
REM   2. py -m venv ..\.venv
REM   3. ..\.venv\Scripts\activate
REM   4. pip install -r requirements-build.txt
REM
REM Then to build:
REM   ..\.venv\Scripts\activate
REM   build.bat
REM
REM The resulting dist\N1MMFreqForward.exe is fully standalone; copy it to
REM operators' machines (no Python required there).
REM ============================================================================

setlocal

if not exist ..\.venv\Scripts\python.exe (
    echo ERROR: no ..\.venv found. Run setup steps in the header first.
    exit /b 1
)

set PY=..\.venv\Scripts\python.exe

%PY% -m PyInstaller ^
    --noconfirm ^
    --onefile ^
    --windowed ^
    --name N1MMFreqForward ^
    --icon icon.ico ^
    relay.py

if errorlevel 1 (
    echo Build failed.
    exit /b 1
)

echo.
echo Built: %CD%\dist\N1MMFreqForward.exe
echo.

endlocal
