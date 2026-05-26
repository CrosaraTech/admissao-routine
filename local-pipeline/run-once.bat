@echo off
REM Roda 1 passada e encerra. Use no Task Scheduler do Windows.
REM Comando completo pra agendar:
REM   "C:\caminho\completo\local-pipeline\run-once.bat"
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERRO] Virtualenv nao encontrado. Rode install.bat primeiro.
    exit /b 1
)

".venv\Scripts\python.exe" main.py
exit /b %errorlevel%
