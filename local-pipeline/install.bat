@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo Crosara - Pipeline de Admissao - Instalacao
echo ============================================================
echo.

REM Verifica Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERRO] Python nao encontrado no PATH.
    echo Instale Python 3.11+ pelo winget: winget install Python.Python.3.11
    pause
    exit /b 1
)

REM Cria venv se nao existir
if not exist ".venv\Scripts\python.exe" (
    echo Criando virtualenv em .venv\ ...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERRO] Falhou criando venv.
        pause
        exit /b 1
    )
) else (
    echo Virtualenv .venv\ ja existe — reaproveitando.
)

echo.
echo Atualizando pip...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet

echo.
echo Instalando dependencias do requirements.txt...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERRO] Falhou instalando deps.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo OK! Proximos passos:
echo.
echo   1. Copie .env.example pra .env e preencha os tokens:
echo        copy .env.example .env
echo        notepad .env
echo.
echo   2. (Opcional mas recomendado) Valide o setup:
echo        run-verificar.bat
echo.
echo   3. Rode:
echo        run-gui.bat        ^(interface grafica^)
echo        run-once.bat       ^(passada unica - Task Scheduler^)
echo        run-loop.bat       ^(polling continuo^)
echo ============================================================
pause
