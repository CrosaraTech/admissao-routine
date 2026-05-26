@echo off
REM Polling continuo (foreground). Ctrl+C pra parar.
REM Intervalo configuravel em config.json (polling_intervalo_segundos).
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERRO] Virtualenv nao encontrado. Rode install.bat primeiro.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" main.py --loop
echo.
echo [Loop encerrado]
pause
