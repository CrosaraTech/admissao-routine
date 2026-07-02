# AdmitER — deploy watcher
#
# Roda em loop no servidor. A cada INTERVALO_SEG:
#   1. git fetch --quiet origin main
#   2. Compara HEAD local vs origin/main
#   3. Se ha commit novo -> git pull + kill python webapp.py
#   4. iniciar-web-loop.bat reinicia python automatico (auto-restart wrapper)
#
# Uso no servidor:
#   powershell -ExecutionPolicy Bypass -File deploy_watcher.ps1
#
# Ou via Task Scheduler:
#   - Trigger: at startup
#   - Action: powershell.exe -ExecutionPolicy Bypass -WindowStyle Hidden -File "C:\AdmitER\deploy_watcher.ps1"
#
# Log em deploy_watcher.log (mesmo diretorio).

$ErrorActionPreference = "Continue"
$RepoPath = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogFile = Join-Path $RepoPath "deploy_watcher.log"
$IntervalSec = 120  # 2 min

function Write-Log {
    param([string]$Msg)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$ts] $Msg"
    Write-Host $line
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

function Kill-WebappPython {
    # Mata processos python.exe rodando webapp.py neste repo
    $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue
    $killed = 0
    foreach ($p in $procs) {
        $cmd = $p.CommandLine
        if ($cmd -and $cmd -match "webapp\.py" -and $cmd -match [regex]::Escape($RepoPath)) {
            try {
                Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
                Write-Log "  Killed PID $($p.ProcessId): $cmd"
                $killed++
            } catch {
                Write-Log "  ERRO kill PID $($p.ProcessId): $_"
            }
        }
    }
    return $killed
}

Set-Location $RepoPath
Write-Log "Deploy watcher iniciado em $RepoPath (intervalo=${IntervalSec}s)"

while ($true) {
    try {
        # Pega HEAD atual
        $local = (git rev-parse HEAD 2>&1).Trim()

        # Busca remoto sem merge
        git fetch --quiet origin main 2>&1 | Out-Null
        $remote = (git rev-parse origin/main 2>&1).Trim()

        if ($local -ne $remote) {
            Write-Log "NOVO COMMIT detectado. Local=$($local.Substring(0,7)) Remote=$($remote.Substring(0,7))"

            # Pull
            $pullOut = git pull --ff-only origin main 2>&1
            Write-Log "  git pull: $pullOut"

            # Kill webapp -> iniciar-web-loop.bat reinicia sozinho
            $n = Kill-WebappPython
            Write-Log "  Killed $n webapp process(es). Wrapper vai reiniciar."
        }
    } catch {
        Write-Log "ERRO no loop: $_"
    }

    Start-Sleep -Seconds $IntervalSec
}
