@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERRO] Virtualenv nao encontrado. Rode install.bat primeiro.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" verificar-setup.py
pause
