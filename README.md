<div align="center">

<img src="local-pipeline/web/static/admiter-logo.svg" alt="AdmitER" width="120" />

# AdmitER

**Pipeline de Admissão Automatizada** — Crosara Contabilidade

`v2.16.43` · Python 3.11 · Flask + HTMX + Waitress · Anthropic Claude Vision · Alterdata eContador API

</div>

---

## O que faz

Lê emails de admissão do Gmail, extrai dados dos documentos anexados (RG, CPF, CTPS, PIS, ficha, comprovante endereço, ASO) usando **Claude Vision**, resolve empresa/departamento/função no eContador, monta payload JSON:API e faz `POST /candidatos` no Alterdata — tudo sem intervenção manual.

Quando algo falha (CNPJ sem cadastro, salário ausente, cargo ambíguo, documento ilegível), levanta pendência com contexto suficiente pra DP resolver em 30 segundos via UI web.

## Interface

### Dashboard

Contadores em tempo real, controle de polling, custo APIs do mês, últimas pendências.

<div align="center">
<img src="local-pipeline/docs/screenshots/Screenshot_6.png" alt="Dashboard" width="900" />
</div>

### Processadas

Histórico agrupado por dia com auditoria completa por candidato (payload enviado + resolução + resultado).

<div align="center">
<img src="local-pipeline/docs/screenshots/Screenshot_7.png" alt="Processadas" width="900" />
</div>

## Features principais

| Área | O que faz |
|---|---|
| **Vision nativo** | Claude lê PDFs/imagens direto — sem OCR intermediário |
| **Self-consistency** | 2 chamadas por email; divergência em CPF é resolvida por validação DV (v2.16.38) |
| **ViaCEP + BrasilAPI** | Preenche rua/bairro/cidade quando só tem CEP (v2.16.40/41) |
| **Datas normalizadas** | BR → ISO automático em `sanitizar_attributes` (v2.16.42) |
| **Perfis remetente** | Salário fixo por cargo, endereço padrão empresa, defaults quando ausente (v2.16.4/27/39) |
| **Idempotência** | Anti-duplicata por CPF+CNPJ; hit não repete POST |
| **Modo rascunho** | Auto-emails vão pra fila de revisão humana em vez de disparar direto (v2.16.32) |
| **Auditoria completa** | NDJSON append-only pra cada email enviado, cada chamada API, cada custo |
| **Excluir pendências** | Botão remove tudo (payload, planilha, rascunhos, labels Gmail) — útil pra falhas 529 (v2.16.37) |
| **Cargos frequentes** | UI edita salário manual por perfil (v2.16.39) |
| **Auto-deploy** | Polling git a cada 2min no servidor → pull + restart automático |

## Bugs API eContador — status

Off-by-one bilateral **corrigido pelo suporte Alterdata em 2026-07-01** (validado via cobaia 10186). IDs semânticos agora:

- `/tipos-raca`: 1=Indígena, 2=Branca, 3=Negra, 6=Amarela, **8=Parda**, 9=Não-Informado
- `/tipos-identidade`: 1=RG, 2=RIC, 3=RNE (round-trip OK)
- `/tipos-cnh` (renomeado de `/categorias-cnh`): 1-9 A/B/AB/C/D/E/AC/AD/AE

Workarounds removidos. Ver `local-pipeline/lookups.json:bugs_conhecidos`.

## Estrutura do repo

| Path | O que é |
|---|---|
| `local-pipeline/` | Código principal (Python + Flask + templates + config + tests) |
| `CLAUDE.md` | Convenções projeto (regras críticas, bugs conhecidos, fluxo passo-a-passo) |
| `local-pipeline/README.md` | README técnico detalhado da subpasta |
| `local-pipeline/SETUP_SERVIDOR.md` | Guia deploy servidor (10 passos: venv → tokens → serviço 24/7) |
| `local-pipeline/SERVIDOR_DEPLOY.md` | Setup auto-deploy Windows (polling git + restart automático) |
| `local-pipeline/RESUMO_MIGRACAO.md` | Contexto pra retomar sessões Claude Code no servidor |
| `local-pipeline/PATCHES.md` | Histórico fixes por versão |
| `local-pipeline/briefing.md` | System prompt Claude (regras, lookups, bugs) |

## Setup rápido

```bash
cd local-pipeline
python -m venv .venv
.venv\Scripts\activate                    # Windows
pip install -r requirements.txt
copy .env.example .env                    # preencha ECONTADOR_TOKEN, GMAIL_TOKEN, ANTHROPIC_API_KEY, DIRECTDATA_TOKEN
```

Deploy servidor: veja `local-pipeline/SETUP_SERVIDOR.md` + `local-pipeline/SERVIDOR_DEPLOY.md`.

## Como rodar

### UI web (recomendado)

```bash
cd local-pipeline
iniciar-web.bat                           # http://localhost:8080
```

Background sem janela:
```bash
wscript iniciar-web-background.vbs
```

Servidor 24/7 com auto-restart + auto-deploy:
```bash
iniciar-web-loop.bat                      # wrapper reinicia quando cai
# + deploy_watcher.ps1 (janela separada — polling git 2min)
```

### CLI (Task Scheduler)

```bash
python main.py                            # 1 passada e encerra
python main.py --loop                     # loop contínuo
```

## Arquitetura

```
┌─────────────┐         ┌──────────────┐         ┌─────────────┐
│  Gmail API  │────────▶│   AdmitER    │────────▶│  eContador  │
│ (labels     │  emails │              │  POST   │ Alterdata   │
│  ADMISSÃO)  │◀────────│  Claude AI   │────────▶│  API v1     │
└─────────────┘  status │  (Vision)    │  IDs    └─────────────┘
                        │              │
                        │  ViaCEP +    │
                        │  BrasilAPI   │
                        │              │
                        │  DirectData  │
                        │  (fallback)  │
                        └──────┬───────┘
                               │
                        ┌──────▼───────┐
                        │  UI Web      │
                        │  Flask+HTMX  │
                        │  localhost   │
                        │    :8080     │
                        └──────────────┘
```

## Fluxo por email

1. Polling Gmail busca label `ADMISSÃO` sem `processado` / `pendente`
2. Baixa anexos → `/tmp/admissao/<msg_id>/`
3. Claude Vision lê PDFs/imagens, retorna `{cnpj, cargo, dados}`
4. Resolve `empresa_id` via `GET /empresas?filter[cpfcnpj]`
5. Resolve `departamento_id` via 3 regras (único, GERAL+empresa, ficha)
6. Resolve `funcao_id` via match CBO + re-prompt se ambíguo
7. Monta payload com `sanitizar_attributes` (datas ISO, CPF int, endereço sem pontuação)
8. `POST /candidatos` — sucesso → label `processado` + email DP
9. Falha → label `pendente` + email DP com contexto

## Segurança

- `.env` **nunca** commitado (`.gitignore` bloqueia)
- Tokens só via variável de ambiente (`ECONTADOR_TOKEN`, `GMAIL_TOKEN`, `ANTHROPIC_API_KEY`, `DIRECTDATA_TOKEN`)
- PII (perfis, rascunhos, planilhas, payloads) fica em `.gitignore` no repo público — fork privado separado guarda estado operacional
- CSRF via `@before_request` (Origin/Referer match `request.host`)
- Allowlist explícita de campos que operador pode sobrescrever via web
- Hard freio no envio automático de email (só via sentinela arquivo)

## Regras críticas (não mudar sem consultar)

- `statusadmissao = 1` **SEMPRE** — único valor que faz candidato descer pro Alterdata Desktop
- `raca = 8` (Parda) default escritório — após fix Alterdata 2026-07-01
- `tipoidentidade = 1` (RG) default escritório
- `diascontratoexperiencia = 30` — UI eContador calcula prorrogação 60 (=90-30)
- `numero = 0` quando ausente (Integer obrigatório — API rejeita "SN" string com 500)
- Datas em ISO 8601 (`YYYY-MM-DD`) — nunca `null`/`""` (viram 30/12/1899 no Desktop)
- CPF como Integer, PIS como String (preserva zeros)
- Strings UPPERCASE (regra escritório), exceto email
- Banco/agência/conta = attributes string, tipoconta = relationship — tudo-ou-nada
