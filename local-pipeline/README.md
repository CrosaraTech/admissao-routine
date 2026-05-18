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
# Padrão: uma única passada e encerra (ideal pro Windows Task Scheduler)
python main.py

# Modo loop contínuo (debug / execução manual em terminal)
python main.py --loop
```

A passada faz tudo de uma vez: busca emails novos, processa cada um,
busca threads aguardando resposta do cliente, reprocessa-os, e encerra.

Logs append-only em `admissao_log.ndjson`.

### Agendar no Windows Task Scheduler (2× por dia)

1. Abra **Agendador de Tarefas** → **Criar Tarefa**
2. **Geral:** nome `Crosara Admissão Pipeline`, marque *Executar com privilégios mais altos*
3. **Disparadores:** adicione 2 disparadores diários (ex: 09:00 e 14:00)
4. **Ações** → *Iniciar um programa*:
   - **Programa/script:** `C:\Users\Havai\AppData\Local\Programs\Python\Python311\python.exe`
   - **Argumentos:** `main.py`
   - **Iniciar em:** `C:\Users\Havai\Desktop\teste eContador\admissao-routine\local-pipeline`
5. **Condições:** desmarque *Iniciar somente se conectado à rede CA* se quiser rodar sempre

O programa lê `.env` da pasta `Iniciar em`, então tokens são pegos automaticamente.

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
| `payloads/` | Um JSON por admissão (timestamp + msg_id) — auditoria/re-envio |

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

## Payloads salvos (auditoria)

Cada admissão processada gera um arquivo em `payloads/<timestamp>_<msgid>.json`
com o payload completo + contexto. Estrutura:

```json
{
  "timestamp": "2026-05-15T10:30:00.123",
  "msg_id": "18f3ab...",
  "remetente": "rh@cliente.com",
  "assunto": "Admissão João",
  "data_email": "Thu, 15 May 2026 09:15:00 -0300",
  "resolucao": {
    "cnpj_empresa": "10560396000185",
    "empresa_id": "89",
    "razao_social": "MODELOFARMA LTDA",
    "departamento_id": "1139",
    "departamento_motivo": "ok",
    "funcao_id": "1259",
    "funcao_confianca": 0.92,
    "cargo_extraido": "Operador de Caixa",
    "cbo_extraido": ""
  },
  "resultado": {
    "status": "sucesso",
    "candidato_id": "20191",
    "erro": null
  },
  "payload": { "data": { "type": "candidatos", "attributes": {...}, "relationships": {...} } }
}
```

Útil pra:
- Auditar o que foi enviado em cada admissão
- Re-postar manualmente (`python -c "import json,httpx; ..."`) se POST falhou
- Comparar resoluções entre admissões parecidas
- Investigar bugs do sync (payload enviado vs. o que chegou no Desktop)

Os arquivos ficam no `.gitignore` (têm CPF/nomes).

## Segurança

- `.env` está no `.gitignore` (não commitar tokens)
- `admissao_log.ndjson` e `payloads/` podem conter CPF/nomes — não compartilhar
- Tokens lidos só de env var, nunca de arquivo commitado
