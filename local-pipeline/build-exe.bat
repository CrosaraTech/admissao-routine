@echo off
REM Empacota o pipeline num AdmitER.exe standalone via PyInstaller.
REM Inclui icone admitir-logo.ico + todos os arquivos auxiliares.
REM
REM Saida: dist\AdmitER.exe (executavel) + dist\AdmitER\ (libs)
REM
REM Pre-requisitos:
REM   - .venv/ configurado (rodar install.bat antes)
REM   - admitir-logo.png na pasta (gera .ico automaticamente)
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERRO] Virtualenv nao encontrado. Rode install.bat primeiro.
    pause
    exit /b 1
)

if not exist "admitir-logo.png" (
    echo [ERRO] admitir-logo.png nao existe na pasta.
    echo Salve a imagem do logo como admitir-logo.png e rode de novo.
    pause
    exit /b 1
)

REM Gera .ico se nao existir (interface.py tambem faz isso, mas garantimos
REM que PyInstaller tenha o .ico antes de buildar)
".venv\Scripts\python.exe" -c "from PIL import Image; img = Image.open('admitir-logo.png'); img.convert('RGBA').save('admitir-logo.ico', format='ICO', sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])"
if errorlevel 1 (
    echo [ERRO] Falha gerando admitir-logo.ico
    pause
    exit /b 1
)

echo Instalando PyInstaller no venv...
".venv\Scripts\python.exe" -m pip install pyinstaller --quiet
if errorlevel 1 (
    echo [ERRO] Falha instalando pyinstaller
    pause
    exit /b 1
)

REM Inclui unrar.exe se existir (opcional — pra descompactar .rar)
set UNRAR_OPT=
if exist "unrar.exe" (
    echo Incluindo unrar.exe no build (suporte a .rar)
    set UNRAR_OPT=--add-data "unrar.exe;."
) else (
    echo [AVISO] unrar.exe nao encontrado — .rar nao sera suportado
    echo         Baixe em https://www.rarlab.com/rar_add.htm e coloque na pasta
)

echo.
echo Buildando AdmitER.exe (pode levar 1-2 min)...
".venv\Scripts\python.exe" -m PyInstaller ^
    --name AdmitER ^
    --icon admitir-logo.ico ^
    --windowed ^
    --noconfirm ^
    --add-data "briefing.md;." ^
    --add-data "config.json;." ^
    --add-data "lookups.json;." ^
    --add-data "departamentos.json;." ^
    --add-data "regras.json;." ^
    --add-data "funcoes_cbo.xlsx;." ^
    --add-data "admitir-logo.png;." ^
    --add-data "admitir-logo.ico;." ^
    --add-data "Logotipo Crosara - CMYK-04.jpg;." ^
    %UNRAR_OPT% ^
    interface.py

if errorlevel 1 (
    echo.
    echo [ERRO] Build falhou. Veja o output do PyInstaller acima.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo OK! AdmitER.exe gerado em:
echo   dist\AdmitER\AdmitER.exe
echo.
echo Pra rodar: clique 2x no AdmitER.exe (precisa do .env na mesma pasta
echo do .exe ou da raiz do projeto).
echo ============================================================
pause
