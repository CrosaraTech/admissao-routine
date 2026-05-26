# Deploy do Pipeline em Servidor Local

Passo-a-passo pra subir o pipeline em outra máquina. Tudo está dentro
desta pasta — basta zipar, copiar e seguir.

## 1. Pré-requisitos no servidor

| Item | Versão | Como instalar (Windows) |
|---|---|---|
| **Python** | 3.11+ | `winget install Python.Python.3.11` |
| **Internet** | — | Conectividade pra Gmail, Anthropic, eContador |
| **Disco** | ~500 MB | Pasta + venv + payloads/backups (acumulam) |
| **Permissão** | Escrita na pasta | Tudo é local — sem permissões de admin |

Em Linux/Mac: `apt install python3.11 python3.11-venv` ou equivalente.

## 2. Extrair e instalar

```powershell
# Descompactar o zip onde quiser. Ex:
cd C:\pipelines\
Expand-Archive local-pipeline-AAAAMMDD-HHMM.zip -DestinationPath .

cd local-pipeline
.\install.bat
```

O `install.bat` faz:
1. Cria virtualenv em `.venv\`
2. Atualiza pip
3. Instala todas as deps de `requirements.txt`
4. Avisa pra editar `.env`

## 3. Configurar credenciais

```powershell
copy .env.example .env
notepad .env
```

3 variáveis obrigatórias:
- `ECONTADOR_TOKEN` — JWT da API E-plugin Alterdata
- `GMAIL_TOKEN` — JSON OAuth do Gmail (1 linha)
- `ANTHROPIC_API_KEY` — chave da API Claude

> Se o zip que recebeu já vem com `.env` (ou com `.env.example` preenchido),
> pule este passo.

## 4. Validar o setup (recomendado)

```powershell
.venv\Scripts\python verificar-setup.py
```

Esse script checa:
- ✓ Python 3.11+
- ✓ Todas as deps instaladas
- ✓ Variáveis de ambiente preenchidas
- ✓ `ECONTADOR_TOKEN` válido (faz `GET /empresas` real)
- ✓ `GMAIL_TOKEN` válido (lista labels)
- ✓ `ANTHROPIC_API_KEY` válida (chamada mínima)
- ✓ Arquivos essenciais (briefing, lookups, planilha CBO)

Se algum check falhar, o erro fica claro com instruções.

## 5. Rodar

### Opção A — Interface gráfica (humano operando)
```powershell
.\run-gui.bat
```
Abre janela. Use **Iniciar polling** pra começar; **Backup agora** pra
snapshot da planilha e payloads.

### Opção B — Passada única (Task Scheduler / cron)
```powershell
.\run-once.bat
```
Faz 1 passada de processamento e encerra. Ideal pra agendar:
- **Windows Task Scheduler**: criar tarefa diária às 09:00 chamando
  `C:\caminho\completo\local-pipeline\run-once.bat`
- **Linux cron**: `0 9 * * * /caminho/local-pipeline/run-once.sh`

### Opção C — Polling contínuo (foreground)
```powershell
.\run-loop.bat
```
Roda sem parar (intervalo configurável em `config.json`). Útil pra
máquina dedicada. Ctrl+C pra parar.

## 6. Estrutura de arquivos no servidor

```
local-pipeline/
├── interface.py, main.py, *.py     # Código
├── briefing.md                     # Prompt do Claude (não editar)
├── config.json                     # Endpoint, labels, intervalo
├── departamentos.json              # CNPJs especiais multi-depto
├── lookups.json                    # Enums + workarounds bugs
├── regras.json                     # Regras customizáveis pelo DP
├── funcoes_cbo.xlsx                # Planilha CBO (9k+ cargos)
├── Logotipo Crosara - CMYK-04.jpg  # Logo da UI
├── .env                            # SECRETS — nunca commitar
├── .venv/                          # Virtualenv local
│
└── (gerados em runtime — append-only):
    ├── admissoes.xlsx              # Planilha de admissões processadas
    ├── admissao_log.ndjson         # Log técnico de cada admissão
    ├── billing.ndjson              # Custo da API Claude por passada
    ├── econtador_audit.ndjson      # Auditoria das chamadas eContador
    ├── payloads/<ts>_<msgid>.json  # Payload de cada admissão
    └── backups/<ts>/               # Backups (via botão na UI)
```

## 7. Atualizar a planilha CBO (se cargos novos)

Se o eContador ganhou cargos novos depois do zip:
```powershell
.venv\Scripts\python gerar_planilha_funcoes.py
```
Preserva os X marcados (curadoria do escritório).

## 8. Troubleshooting

### O programa não abre / erro de import
- Garantir que rodou pelos .bat (eles ativam o venv automaticamente)
- Sem .bat: `.venv\Scripts\activate` antes de `python interface.py`

### "ECONTADOR_TOKEN ausente"
- Conferir se o `.env` existe e tem a variável (sem aspas em volta do
  valor)

### Gmail "invalid_grant" / token expirado
- O `refresh_token` no `GMAIL_TOKEN` (não confundir com `token`) raramente
  expira mas pode acontecer. Regerar o `GMAIL_TOKEN` completo via OAuth do
  Google Cloud Console.

### Custo Claude está alto
- Aba **Estatísticas** mostra o gasto do mês corrente
- Aba **API eContador** mostra cada chamada e duração
- Em `config.json`: `anthropic.chamadas_verificacao=1` desliga o
  double-check (corta custo pela metade mas reduz robustez)

### Backup das admissões processadas
- UI: aba Principal → **Backup agora**. Salva em `backups/<timestamp>/`
- Manual: copiar `admissoes.xlsx` + `payloads/` periodicamente

## 9. Logs pra debug

| Arquivo | Conteúdo |
|---|---|
| `admissao_log.ndjson` | Status final de cada admissão (sucesso/pendente) |
| `payloads/*.json` | Payload exato enviado + resultado eContador |
| `econtador_audit.ndjson` | BEFORE/AFTER de cada chamada HTTP com `corr_id` |
| `billing.ndjson` | Tokens + custo Claude por passada |

Pra debugar:
```bash
# Falhas em chamadas eContador
jq 'select(.success==false)' econtador_audit.ndjson

# Admissões com problema
jq 'select(.status!="sucesso")' admissao_log.ndjson

# Custo total da semana
jq -s 'map(select(.timestamp | startswith("2026-05"))) | map(.custo_usd) | add' billing.ndjson
```
