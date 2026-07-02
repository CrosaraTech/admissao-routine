@echo off
REM AdmitER — wrapper com auto-restart
REM Roda webapp.py num loop infinito. Quando processo morre (crash OU kill
REM do deploy_watcher.ps1), reinicia automatico apos 3s. Permite deploy
REM sem intervencao humana: watcher mata python -> este loop reinicia com
REM codigo novo.

REM Path do repo — ajuste se instalou em outro lugar
cd /d "%~dp0"

:LOOP
echo.
echo ============================================================
echo [%date% %time%] Iniciando AdmitER webapp...
echo ============================================================
REM Path absoluto obrigatorio — Windows grava CommandLine literal no WMI.
REM Se lancar com caminho relativo (.\.venv...), Kill-WebappPython do
REM deploy_watcher.ps1 nao consegue casar contra $RepoPath e nunca mata
REM o processo -> auto-deploy silenciosamente nao funciona.
"%~dp0.venv\Scripts\python.exe" "%~dp0webapp.py"

echo.
echo [%date% %time%] Webapp encerrou (exit=%errorlevel%). Reiniciando em 3s...
timeout /t 3 /nobreak >nul
goto LOOP
