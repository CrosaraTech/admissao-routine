@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERRO] Virtualenv nao encontrado. Rode install.bat primeiro.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" interface.py
if errorlevel 1 (
    echo.
    echo [Pipeline encerrou com erro — veja a mensagem acima]
    pause
)
