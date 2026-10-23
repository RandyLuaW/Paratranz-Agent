@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONPATH=%~dp0src
rem [emudhra-fix 2026-10-21] Prepend the hardened trust-store shim dir.
rem Original PYTHONPATH (src) is preserved. Set EMUDHRA_FIX_OFF=1 to bypass.
if not exist "C:\Agent\workspace\emudhra-cert-audit\fix\sitecustomize.py" goto emudhra_fix_done
if defined EMUDHRA_FIX_OFF goto emudhra_fix_done
set "PYTHONPATH=C:\Agent\workspace\emudhra-cert-audit\fix;%~dp0src"
:emudhra_fix_done

rem WebUI console. Opens http://127.0.0.1:8787 in your browser.
rem First time: fill ParaTranz Token + project id in the Settings tab.
rem Demo without a token:  web.bat --mock
rem Keep this window open; close it (or Ctrl+C) to stop.

if not exist ".venv\Scripts\python.exe" (
    echo [error] venv missing. Run first:
    echo     uv venv .venv --python 3.13
    echo     uv pip install --python .venv\Scripts\python.exe -e ".[dev]"
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m paratranz_agent.cli serve %*
echo.
echo [webui stopped] press any key to close.
pause >nul
