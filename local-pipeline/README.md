<div align="center">

<img src="web/static/admiter-logo.svg" alt="AdmitER" width="120" />

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
<img src="docs/screenshots/dashboard.png" alt="Dashboard" width="900" />
</div>

### Processadas

Histórico agrupado por dia com auditoria completa por candidato (payload enviado + resolução + resultado).

<div align="center">
<img src="docs/screenshots/processadas.png" alt="Processadas" width="900" />
</div>

## Features principais

| Área | O que faz |
|---|---|
| **Vision nativo** | Claude lê PDFs/imagens direto — sem OCR intermediário |
| **Self-consistency** | 2 chamadas ao Claude por email; divergência em CPF é resolvida por validação DV (v2.16.38) |
| **ViaCEP + BrasilAPI** | Preenche rua/bairro/cidade quando só tem CEP (v2.16.40/41) |
| **Datas normalizadas** | BR → ISO automático em `sanitizar_attributes` (v2.16.42) |
| **Perfis remetente** | Salário fixo por cargo, endereço padrão empresa, defaults quando ausente (v2.16.4/27/39) |
| **Idempotência** | Anti-duplicata por CPF+CNPJ; hit não repete POST |
| **Modo rascunho** | Auto-emails vão pra fila de revisão humana em vez de disparar direto (v2.16.32) |
| **Auditoria completa** | NDJSON append-only pra cada email enviado, cada chamada API, cada custo |
| **Excluir pendências** | Botão remove tudo (payload, planilha, rascunhos, labels Gmail) — útil pra falhas 529 (v2.16.37) |
| **Cargos frequentes** | UI edita salário manual por perfil (v2.16.39) |

## Bugs API eContador — status

Off-by-one bilateral foi **corrigido pelo suporte Alterdata em 2026-07-01** (validado via cobaia 10186). IDs semânticos agora:

- `/tipos-raca`: 1=Indígena, 2=Branca, 3=Negra, 6=Amarela, **8=Parda**, 9=Não-Informado
- `/tipos-identidade`: 1=RG, 2=RIC, 3=RNE (round-trip OK)
- `/tipos-cnh` (renomeado de `/categorias-cnh`): 1-9 A/B/AB/C/D/E/AC/AD/AE

Workarounds removidos. Ver `lookups.json:bugs_conhecidos`.

## Setup rápido

```bash
cd local-pipeline
python -m venv .venv
.venv\Scripts\activate                    # Windows
pip install -r requirements.txt
copy .env.example .env                    # preencha ECONTADOR_TOKEN, GMAIL_TOKEN, ANTHROPIC_API_KEY
```

Ver `SETUP_SERVIDOR.md` pra deploy em servidor (10 passos: venv → NSSM como serviço 24/7).

## Como rodar

### UI web (recomendado)

```bash
iniciar-web.bat                           # abre http://localhost:8080
```

Ou background sem janela:

```bash
wscript iniciar-web-background.vbs
```

### CLI (Task Scheduler)

```bash
python main.py                            # 1 passada e encerra
python main.py --loop                     # loop contínuo
```

Ver `SETUP_SERVIDOR.md` §7 pra agendar passada 2×/dia no Task Scheduler.

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

## Estrutura

| Módulo | Função |
|---|---|
| `main.py` | Orquestrador (polling, resolução, POST, finalização) |
| `webapp.py` | UI Flask (dashboard, pendentes, processadas, perfis, auditoria) |
| `claude_client.py` | API Claude Vision (self-consistency, DV CPF, briefing como system prompt) |
| `ecotador_client.py` | GET empresas/departamentos + POST candidatos com audit trail |
| `payload_builder.py` | Sanitização (datas, CPF, endereço, telefone, RG, tituloeleitor) |
| `enrichment.py` | ViaCEP + BrasilAPI + DirectData + defaults fixos |
| `endereco_utils.py` | Parse endereço string única → campos separados |
| `enderecos_padrao_empresa.py` | Endereço padrão por CNPJ (fazendas, alojamentos) |
| `perfis_remetente.py` | Perfis clientes (cargos, salários, defaults, aliases) |
| `dashboard_data.py` | Dados agregados pra UI |
| `dashboard_data.py` · `idempotencia.py` · `auditoria_emails.py` | Estado + auditoria |
| `funcao.py` | Match cargo → funcao_id via CBO + fuzzy + re-prompt |
| `departamento.py` | 3 regras de departamento (único, GERAL+empresa, ficha) |
| `gmail_client.py` | OAuth Gmail + labels + envio email DP |
| `directdata_client.py` · `directdata_mapper.py` | Consulta CPF externa (fallback) |
| `web/templates/` | Jinja2 + HTMX 2.0 |
| `web/static/app.css` | Design system (Fraunces serif + tokens claro/escuro) |
| `briefing.md` | System prompt (regras, lookups, bugs conhecidos) |
| `funcoes_cbo.xlsx` | Planilha cargos (~9k linhas, CBO + funcao_id) |
| `lookups.json` | Enums + defaults + bugs conhecidos |
| `departamentos.json` | CNPJs com múltiplos deptos |
| `config.json` | URL base, labels, intervalo polling, feature flags |

## Documentação

- `SETUP_SERVIDOR.md` — deploy do zero em servidor (checklist 10 passos)
- `RESUMO_MIGRACAO.md` — contexto pra retomar sessões Claude Code
- `PATCHES.md` — histórico fixes por versão
- `briefing.md` — system prompt completo (regras, workarounds, lookups)
- `web/README-WEB.md` — acesso LAN, firewall, tutorial
- `../CLAUDE.md` — convenções projeto (raiz do repo)

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
