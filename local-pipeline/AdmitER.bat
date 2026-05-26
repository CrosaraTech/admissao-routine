@echo off
REM AdmitER — Pipeline de Admissao da Crosara (by CrosaraTech)
REM Launcher principal — abre a interface grafica.
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
    echo [AdmitER encerrou com erro — veja a mensagem acima]
    pause
)
