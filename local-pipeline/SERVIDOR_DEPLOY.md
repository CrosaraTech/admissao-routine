# Setup Servidor com Auto-Deploy — AdmitER

Guia específico pra máquina servidor Win10 que roda o AdmitER 24/7.
Suplementa `SETUP_SERVIDOR.md` com automação de deploy.

## Como funciona

```
Desktop (dev)         GitHub privado         Servidor (Win10)
    edit  ────push───► main branch ◄──fetch── deploy_watcher.ps1
                                                  │
                                                  │ novo commit
                                                  ▼
                                              git pull
                                                  │
                                                  ▼
                                              kill python webapp
                                                  │
                                                  ▼
                                          iniciar-web-loop.bat
                                          reinicia sozinho
```

Sem webhook. Sem porta aberta. Polling git a cada 2min.

## Setup inicial (1x, ~15min)

### 1. Instalar Python 3.11 + Git

- Python 3.11: https://www.python.org/downloads/release/python-3119/ — marca "Add to PATH"
- Git: https://git-scm.com/download/win

### 2. Clonar repo privado

Abre PowerShell como Administrador:

```powershell
# Configura credencial (só 1x — abre browser pra login GitHub)
gh auth login
# OU: git config --global credential.helper wincred
#     (na primeira operação de push/pull, digita usuário+PAT do GitHub)

# Clone em C:\AdmitER
cd C:\
git clone https://github.com/JoaoMarcos347/admissao-routine-privado.git AdmitER
cd C:\AdmitER
```

### 3. Criar .env

```powershell
copy .env.example .env
notepad .env
```

Preenche os 4 tokens (Anthropic, Gmail OAuth, eContador JWT, DirectData).
`.env` fica no `.gitignore` — nunca vai pro git.

### 4. Virtualenv + deps

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 5. Autoriza execução de PowerShell scripts

Uma vez, como Admin:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

### 6. Cria atalho de inicialização automática

Cria dois atalhos na pasta **Startup do Windows** pra rodar após login:

Abre: `Win+R` → `shell:startup` → cola dois arquivos:

**Atalho 1 — AdmitER webapp** (arquivo `admiter-webapp.bat`):
```bat
@echo off
start "AdmitER Web" cmd /k "cd /d C:\AdmitER && iniciar-web-loop.bat"
```

**Atalho 2 — Deploy watcher** (arquivo `admiter-watcher.bat`):
```bat
@echo off
start "AdmitER Watcher" powershell -ExecutionPolicy Bypass -File "C:\AdmitER\deploy_watcher.ps1"
```

Reinicia PC → ambos abrem sozinhos. Duas janelas terminal ficam abertas.

## Fluxo do dia a dia

### Do lado dev (aqui, desktop):

```bash
# Faz alterações no código
git add local-pipeline/algum_arquivo.py
git commit -m "fix: bug X"
git push origin main
```

### Do lado servidor:

- Em ≤2 min, `deploy_watcher.ps1` detecta commit novo
- Faz `git pull`
- Mata processo `python webapp.py`
- `iniciar-web-loop.bat` detecta que processo morreu e reinicia com código novo
- Log em `deploy_watcher.log`

Tempo total dev → produção: ~2min de espera + segundos de restart.

## Verificar se está funcionando

### No servidor:

```powershell
# Log do watcher
Get-Content C:\AdmitER\deploy_watcher.log -Tail 20 -Wait

# Ver processos python rodando
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Select ProcessId, CommandLine

# Testar deploy manual
cd C:\AdmitER
git fetch origin main
git log HEAD..origin/main --oneline  # commits pendentes
```

### Testar auto-deploy end-to-end:

1. Do desktop: faz um commit pequeno (ex: edit README) + push
2. No servidor, abra `deploy_watcher.log` — em ≤2min aparece `NOVO COMMIT detectado`
3. Terminal do webapp mostra restart
4. Confere http://localhost:8080 — versão nova rodando

## Troubleshooting

| Problema | Solução |
|---|---|
| Watcher não puxa commit | Verifica `git remote -v` no servidor + credenciais salvas |
| Webapp não reinicia após kill | Confere `iniciar-web-loop.bat` está rodando (janela aberta) |
| Erro auth git | `gh auth status` OU `git config --global credential.helper wincred` |
| PS script bloqueado | Executa `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` como Admin |
| Duas instâncias webapp | Watcher mata só quem bate path do repo; se rodou webapp de outro lugar, mata manual |

## Limitações

- Polling a cada 2min → não é instantâneo. Se precisa faster, muda `$IntervalSec` em `deploy_watcher.ps1`
- Se conexão internet cair no servidor, `git fetch` falha mas watcher continua tentando
- **NÃO faz rollback automático**: se commit quebra webapp, precisa `git revert` + push do desktop pra corrigir
- Sem healthcheck: watcher não sabe se webapp está saudável, só se o processo existe
