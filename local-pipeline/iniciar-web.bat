@echo off
chcp 65001 >nul
setlocal ENABLEDELAYEDEXPANSION
title AdmitER Web - Pipeline de Admissao Crosara

echo.
echo ============================================================
echo   Iniciando AdmitER Web...
echo ============================================================
echo.

REM Posiciona no diretorio do script (suporta clique duplo de qualquer lugar)
cd /d "%~dp0"

REM Descobre TODOS os IPv4 da maquina (Ethernet, Wi-Fi, VPN, etc.)
REM Guarda em IP_1, IP_2, ..., IP_N e IP_COUNT pra mostrar depois.
set IP_COUNT=0
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /R /C:"IPv4"') do (
    for /f "tokens=* delims= " %%b in ("%%a") do (
        set "ip=%%b"
        if not "!ip!"=="127.0.0.1" (
            set /a IP_COUNT+=1
            set "IP_!IP_COUNT!=!ip!"
        )
    )
)

call :mostrar_acessos

REM Tenta venv primeiro, depois cai pro Python global
if exist ".venv\Scripts\python.exe" (
    set PYEXE=.venv\Scripts\python.exe
    echo [info] Usando .venv\Scripts\python.exe
) else (
    set PYEXE=python
    echo [info] .venv nao encontrado, usando Python do sistema
)
echo.

REM Checa se Flask + waitress estao instalados; se nao, instala automaticamente
%PYEXE% -c "import flask, waitress" 2>nul
if errorlevel 1 (
    echo [info] Flask ou waitress nao instalados — rodando pip install...
    echo.
    %PYEXE% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [erro] Falha instalando dependencias. Verifique sua conexao.
        pause
        exit /b 1
    )
    echo.
    echo [info] Instalacao concluida. Repetindo enderecos antes de subir o servidor:
    echo.
    call :mostrar_acessos
)

%PYEXE% webapp.py

echo.
echo ============================================================
echo   AdmitER Web encerrou.
echo ============================================================
echo.
pause
exit /b 0

REM ===========================================================
REM Subrotina que imprime os enderecos de acesso (chamada 2x:
REM uma no boot inicial e outra apos o pip install, se rolou)
REM ===========================================================
:mostrar_acessos
echo ============================================================
echo   Acesso pela rede:
echo ============================================================
echo   Local:   http://localhost:8080
if %IP_COUNT% gtr 0 (
    for /l %%n in (1,1,%IP_COUNT%) do (
        echo   LAN:     http://!IP_%%n!:8080
    )
) else (
    echo   ^(nao foi possivel descobrir o IP da rede^)
)
echo.
echo   Para parar a web, feche esta janela ou pressione Ctrl+C.
echo ============================================================
echo.
exit /b 0
