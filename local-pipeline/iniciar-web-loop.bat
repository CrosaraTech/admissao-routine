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

REM v2.16.61: git pull antes de subir webapp. Substitui deploy_watcher.ps1
REM (que morria em loop). Novo fluxo: cada vez que webapp cai/eh morta,
REM proximo start puxa versao mais recente do repo antes de rodar.
REM Redireciona output pra log dedicado — nao polui janela + serve como
REM auditoria de deploys.
echo [%date% %time%] git reset+pull... >> deploy_pull.log
REM v2.16.61 fix: reset --hard descarta working copy conflitante (ex: bat
REM editado diretamente via Y: SMB sem commit). Sem isso, pull --ff-only
REM aborta com "local changes would be overwritten by merge".
git reset --hard >> deploy_pull.log 2>&1
git pull --ff-only >> deploy_pull.log 2>&1
echo. >> deploy_pull.log

REM Path absoluto obrigatorio — Windows grava CommandLine literal no WMI.
REM Se lancar com caminho relativo (.\.venv...), Kill-WebappPython do
REM deploy_watcher.ps1 nao consegue casar contra $RepoPath e nunca mata
REM o processo -> auto-deploy silenciosamente nao funciona.
"%~dp0.venv\Scripts\python.exe" "%~dp0webapp.py"

echo.
echo [%date% %time%] Webapp encerrou (exit=%errorlevel%). Reiniciando em 3s...
ping -n 4 127.0.0.1 >nul
goto LOOP
