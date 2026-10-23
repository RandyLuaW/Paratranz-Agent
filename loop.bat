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

rem NOTE: keep every line ASCII-only. chcp 65001 + CJK comments get truncated
rem by cmd and the tail is executed as a command.

if not exist ".venv\Scripts\python.exe" (
    echo [error] venv missing. Run first:
    echo     uv venv .venv --python 3.13
    echo     uv pip install --python .venv\Scripts\python.exe -e ".[dev]"
    pause
    exit /b 1
)

if not exist "config\agent.yaml" (
    echo [error] config\agent.yaml not found.
    echo     copy config\agent.example.yaml config\agent.yaml
    echo     then fill PTZ_API_TOKEN in .env
    echo.
    echo To see a full offline demo instead:
    echo     run.bat run --mock --yes
    pause
    exit /b 1
)

rem Poll every 10 minutes (600s). For 3 minutes use --interval 180.
rem Close this window or press Ctrl+C to stop.

".venv\Scripts\python.exe" -m paratranz_agent.cli watch --yes --interval 600
echo.
echo [agent stopped] press any key to close.
pause >nul
