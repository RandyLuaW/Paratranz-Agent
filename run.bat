@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONPATH=%~dp0src

if not exist ".venv\Scripts\python.exe" (
    echo [error] venv missing. Run first:
    echo     uv venv .venv --python 3.13
    echo     uv pip install --python .venv\Scripts\python.exe -e ".[dev]"
    exit /b 1
)

".venv\Scripts\python.exe" -m paratranz_agent.cli %*
exit /b %errorlevel%
