# local-pipeline

Pipeline de admissão local da Crosara Contabilidade. Roda na máquina do
escritório, monitora Gmail e faz POST direto no eContador via API Claude
(Vision) — completamente independente do Routines.

## Setup

```bash
cd local-pipeline
pip install -r requirements.txt
cp .env.example .env
# preencha .env com ECONTADOR_TOKEN, GMAIL_TOKEN, ANTHROPIC_API_KEY
```

Edite `config.json` se quiser ajustar:
- `polling_intervalo_segundos` (default 300 = 5 min)
- `dp.email_notificacao` (destinatário das notificações)
- `dry_run: true` pra testar sem postar

## Como rodar

```bash
# Loop contínuo (default — polling a cada 5 min)
python main.py

# Uma única passada (debug)
python main.py --once
```

Logs append-only em `admissao_log.ndjson`.

## Estrutura

| Arquivo | Função |
|---|---|
| `main.py` | Entry point, polling loop, orquestração |
| `gmail_client.py` | Autenticação Gmail + busca emails + anexos + labels |
| `claude_client.py` | Chama API Claude com Vision (corpo + base64) |
| `ecotador_client.py` | GET /empresas, /departamentos + POST /candidatos |
| `departamento.py` | 3 regras de negócio pra resolver departamento |
| `funcao.py` | Match planilha CBO + re-prompt Claude se ambíguo |
| `payload_builder.py` | Sanitização + injeção de IDs resolvidos |
| `briefing.md` | Briefing completo (regras, lookups, bugs) — system prompt |
| `funcoes_cbo.xlsx` | Planilha de cargos (nome_cargo, cbo, funcao_id) |
| `lookups.json` | Bootstrap automático da raiz (enums + workarounds) |
| `departamentos.json` | Bootstrap automático da raiz (CNPJs especiais) |
| `config.json` | URL base, labels, intervalo polling |
| `.env.example` | Template de variáveis de ambiente |
| `admissao_log.ndjson` | Log append-only NDJSON |

## Fluxo

1. **Polling Gmail** — busca emails com label `ADMISSÃO` sem `ADMISSÃO/processado` e sem `ADMISSÃO/pendente`
2. **Para cada email:**
   1. Extrai corpo (texto) + anexos (PDF/PNG/JPG)
   2. Envia tudo pro Claude (`claude-sonnet-4-20250514`) com o briefing completo como system prompt
   3. Claude retorna `{cnpj_empresa, departamento_sugerido, data: {...payload...}}`
   4. `GET /empresas?filter[cpfcnpj]=...` → `empresa_id`
   5. `GET /departamentos?filter[empresaId]=...` + regras de negócio → `departamento_id`
   6. Match planilha CBO → `funcao_id` (re-prompt Claude se ambíguo)
   7. `POST /candidatos` no eContador
   8. Label `processado` ou `pendente` + email pro DP

## Regras de Departamento

1. **Empresa com 1 depto** → usa direto
2. **Empresa com 2 deptos (GERAL + NOME_EMPRESA)** → usa o não-GERAL
3. **3 CNPJs especiais multi-depto:**
   - `08867336000168` — SOL NASCENTE TRANSPORTADORA E LOGISTICA LTDA
   - `08881442000104` — ROSA DE OURO DISTRIBUICAO E LOGISTICA LTDA
   - `02199795000134` — EDMAR VILELA LTDA
   - Lê `departamentos.json` e faz match fuzzy entre `departamento_sugerido`
     (extraído pelo Claude) e as variantes configuradas

## Planilha CBO

`funcoes_cbo.xlsx` deve ter as colunas:
- `nome_cargo` (string) — nome cadastrado no eContador
- `cbo` (string/int) — código CBO de 6 dígitos
- `funcao_id` (string/int) — id da função no eContador

Estratégia de match:
1. Match exato por CBO (se Claude extraiu o CBO da ficha)
2. Match fuzzy por nome
3. Se múltiplos com mesmo CBO ou scores próximos → repassa lista filtrada pro
   Claude desambiguar com contexto do email

## Segurança

- `.env` está no `.gitignore` (não commitar tokens)
- `admissao_log.ndjson` pode conter CPF/nomes — não compartilhar
- Tokens lidos só de env var, nunca de arquivo commitado
