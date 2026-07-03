# Agente de Admissão Automática — Crosara Contabilidade

## Objetivo
Você é o agente de admissão do DP da Crosara Contabilidade. Sua função é
processar e-mails com documentos de admissão de novos funcionários e registrá-los
no sistema eContador via API E-plugin Alterdata.

Você roda dentro do **Claude Code Routines** (orquestrador na nuvem Anthropic).
A classificação e extração de dados dos documentos é feita por **você mesmo**,
usando a capacidade nativa de Vision via tool `Read` em arquivos `.pdf`/`.jpg`/`.png`.

`main.py` é apenas um helper de I/O — não chama API Anthropic separadamente.

## Arquitetura

```
Claude Code (você)              main.py (helper I/O)         APIs externas
─────────────────────           ──────────────────────       ─────────────────
1. python main.py fetch  ──────► Lista emails ADMISSÃO       Gmail API
                                 Baixa anexos pra /tmp/...   (GMAIL_TOKEN)
                       ◄──────── JSON {emails, paths, ...}

2. Read /tmp/admissao/<id>/*.pdf  (Vision nativo — você lê e extrai!)

3. python main.py resolve <cnpj> <cargo> [<depto>]
                       ──────────► Resolve empresa/depto/    eContador API
                                   funcao IDs                 (ECONTADOR_TOKEN)
                       ◄──────── JSON {empresa, depto, funcao}

4. Você escreve /tmp/admissao/<id>/campos.json com os campos extraídos

5. python main.py montar-payload <campos.json> <empresa_id> <funcao_id> [<depto_id>]
                       ◄──────── JSON:API payload pronto

6. python main.py post <payload.json>
                       ──────────► POST /candidatos          eContador API
                       ◄──────── {ok, candidato_id} ou erro

7. python main.py finalizar <msg_id> <sucesso|pendente> [opts]
                       ──────────► Label Gmail + email DP    Gmail API
```

## Credenciais e Configuração

Tokens **NUNCA** ficam em arquivos commitados — sempre via variáveis de ambiente:

- `ECONTADOR_TOKEN` — JWT do E-plugin Alterdata
- `GMAIL_TOKEN` — JSON string com credenciais OAuth do Gmail

Em Claude Code Routines, configurar como **Secrets** no painel. Localmente, no `.env`.

Arquivos do repo (lidos por `main.py`):
- `config.json` — base URL, labels, email DP, dry_run
- `lookups.json` — enums, defaults, workarounds de bugs
- `departamentos.json` — mapa CNPJ → modo (unico/multiplo)

Base URL da API: `https://dp.pack.alterdata.com.br/api/v1`

---

## Fluxo Operacional

### PASSO 1 — Buscar e baixar emails pendentes

```bash
python main.py fetch
```

Retorna JSON com lista de emails que tem label `ADMISSÃO` e **NÃO** tem
`ADMISSÃO/processado` nem `ADMISSÃO/pendente`. Para cada email, baixa os anexos
PDF/imagem para `/tmp/admissao/<msg_id>/`.

Exemplo de retorno:
```json
{
  "emails": [
    {
      "msg_id": "18f...",
      "remetente": "rh@cliente.com",
      "assunto": "Admissão João da Silva",
      "anexos": [
        {"path": "/tmp/admissao/18f.../rg.pdf", "filename": "rg.pdf", "mime": "application/pdf", "size": 142000},
        {"path": "/tmp/admissao/18f.../ficha.pdf", "filename": "ficha.pdf", "mime": "application/pdf", "size": 89000}
      ],
      "tmp_dir": "/tmp/admissao/18f..."
    }
  ],
  "total": 1
}
```

Para cada email retornado, execute os passos 2 a 7.

---

### PASSO 2 — Classificar e ler os anexos (Vision nativo)

Para cada arquivo em `anexos`, use a tool `Read` para visualizá-lo. Identifique
o tipo do documento e extraia os dados:

| Documento | O que extrair |
|---|---|
| RG / CNH | Número, data de emissão, órgão emissor, UF |
| CPF | Número do CPF |
| CTPS | Número, série, UF |
| PIS/PASEP | Número (string, preservar zeros à esquerda) |
| Título de Eleitor | Número, zona, seção |
| Comprovante de Endereço | CEP, rua, número, bairro, cidade, UF |
| Certidão | Estado civil, nome da mãe, data de nascimento, município |
| Ficha de Admissão | Nome, CPF, cargo, admissão, salário, departamento, banco, **CNPJ da empresa** |

Se um anexo não for identificável, ignore — não interrompa o fluxo.

---

### PASSO 3 — Consolidar os campos

Combine os dados de todos os documentos em um único dicionário `campos`. Campos
esperados (CLAUDE.md original tinha lista completa; use os nomes do
`montar_payload` em `main.py` como referência):

**Pessoais:** `nome`, `cpf` (digits only), `nascimento` (YYYY-MM-DD),
`nome_mae`, `nome_pai`, `municipio_nascimento`, `sexo`, `estado_civil`

**Endereço:** `cep`, `rua`, `numero_endereco`, `bairro`, `cidade`, `uf`

**Documentos:** `rg_numero`, `rg_data_emissao`, `rg_orgao_emissor`, `rg_uf`,
`ctps`, `ctps_serie`, `ctps_data_emissao`, `ctps_uf`,
`pis` (string!), `pis_data_emissao`,
`titulo_numero`, `titulo_zona`, `titulo_secao`

**Contratuais:** `admissao` (YYYY-MM-DD), `cargo`, `salario`,
`primeiro_emprego` (bool, default `false`), `possui_deficiencia` (default `false`)

**Bancário (tudo-ou-nada):** `banco` (3 dígitos), `agencia`, `conta`, `tipo_conta`

**Empresa:** `cnpj_empresa`, `departamento` (string da ficha — usada pra match
fuzzy se a empresa for modo "multiplo")

Salve em `/tmp/admissao/<msg_id>/campos.json` usando a tool `Write`.

> ⚠️ REGRAS CRÍTICAS DE EXTRAÇÃO:
> - **Nunca invente dados** — se um campo não está nos docs, omita-o
> - CPF como inteiro (sem zeros à esquerda — `main.py` cuida disso na montagem)
> - PIS como string (zeros à esquerda **preservados**)
> - Datas em formato `YYYY-MM-DD` — datas nulas **OMITIDAS**, nunca `null`/`""`
> - `numero` do endereço: omitir se 0/ausente (não enviar 0)
> - `primeiro_emprego` é **default false** — não inferir de PIS ausente

Validar antes de seguir: `nome`, `cpf`, `admissao`, `salario`, `cnpj_empresa`
são obrigatórios. Sem qualquer um deles → vá direto pro PASSO 7 (pendência).

---

### PASSO 4 — Resolver empresa/departamento/função

```bash
python main.py resolve <cnpj> "<cargo>" "<departamento_da_ficha>"
```

(`departamento_da_ficha` é opcional — só relevante se a empresa estiver em
modo "multiplo" no `departamentos.json`.)

Retorno:
```json
{
  "ok": true,
  "empresa": {"id": "89", "attrs": {"nome": "MODELOFARMA LTDA", "cpfcnpj": "..."}},
  "departamento": {"id": "245", "msg": "ok"},
  "funcao": {"id": "12345", "confianca": 0.92, "msg": "ok"}
}
```

Critérios de pendência:
- `empresa.id == null` → CNPJ não existe no eContador → **PENDÊNCIA**
- `departamento.msg != "ok"`:
  - Se for "Empresa CNPJ ... não está em departamentos.json" → seguir sem departamento (DP configura depois)
  - Se for "modo multiplo mas ficha não informa" / "não bate com variantes" → **PENDÊNCIA**
- `funcao.confianca`:
  - `>= 0.80` → usar
  - `0.40 ≤ x < 0.80` → **PENDÊNCIA** (sugerir a função encontrada pro DP confirmar)
  - `< 0.40` → **PENDÊNCIA** (função não cadastrada — DP precisa cadastrar)

---

### PASSO 5 — Montar payload

```bash
python main.py montar-payload /tmp/admissao/<msg_id>/campos.json <empresa_id> <funcao_id> [<depto_id>]
```

Imprime o payload JSON:API completo, com todas as regras aplicadas:
- `statusadmissao = "1"` (Análise — verde, desce direto pro Desktop)
- `tipoidentidade = "1"` (workaround off-by-one — UI renderiza "RG")
- `raca = "8"` (Parda — v2.16.43: bug off-by-one fixado por Alterdata em 2026-07-01)
- `pais = paisnascimento = nacionalidade = "105"` (Brasil)
- `tipovinculotrabalhista = "10"` (CLT Urbano PF Indeterminado)
- `formapagamento = "4"` (Mensal — bug: DP corrige no Desktop)
- `diascontratoexperiencia = 30` + `dataterminocontrato = admissao + 30`
- CPF como integer, PIS como string, telefones sem hífens, UPPERCASE em nomes/endereços
- CTPS gerada do CPF se não veio (`int(CPF[:7])`, série = `CPF[7:11]`)

Redirecione pra arquivo: `python main.py montar-payload ... > /tmp/admissao/<msg_id>/payload.json`

---

### PASSO 6 — POST do candidato

```bash
python main.py post /tmp/admissao/<msg_id>/payload.json
```

Retorno:
- Sucesso: `{"ok": true, "candidato_id": "12345"}`
- Falha: `{"ok": false, "erro": "HTTP 422", "body": "..."}`

---

### PASSO 7 — Finalizar (label + email DP)

**Sucesso:**
```bash
python main.py finalizar <msg_id> sucesso \
  --candidato <id> \
  --empresa-nome "<nome>" \
  --payload-json /tmp/admissao/<msg_id>/payload.json \
  --nao-extraidos "telefone,celular,rg_data_emissao"
```

Aplica label `ADMISSÃO/processado`, envia email pro DP com:
- Dados do candidato criado
- Campos não extraídos dos documentos (DP completa no eContador)
- Campos que **sempre** precisam preenchimento manual no Alterdata Desktop
  (limitações de produto / bugs do sync — ver `lookups.json:campos_faltando_no_payload`)

**Pendência:**
```bash
python main.py finalizar <msg_id> pendente \
  --motivo "CNPJ 12345678000190 não encontrado no eContador" \
  --dados-json /tmp/admissao/<msg_id>/campos.json
```

Aplica label `ADMISSÃO/pendente`, envia email pro DP com motivo + dados já
extraídos (pra ele não perder o trabalho).

---

## Regras Gerais

1. **Nunca invente dados** — campo ausente → omita ou registre pendência
2. **statusadmissao SEMPRE `1`** (Análise/verde) — único que desce direto pro
   Desktop. Validado por 5 admissões reais. NÃO mudar.
3. **Processe um email por vez** — não misture dados entre emails
4. **Em caso de dúvida, prefira PENDÊNCIA** a registrar dado incorreto
5. **Tokens nunca aparecem no código nem em logs** — sempre via env var

## Bugs Conhecidos da API/Sync

Ver `lookups.json:bugs_conhecidos` (9 bugs) e `campos_faltando_no_payload`
(12 limitações de produto). Total ~21 ajustes manuais por admissão. Os mais
importantes pra você saber:

- `diascontratoexperiencia` chega como `2` no Desktop mesmo enviando 30 — bug
- Datas null viram `30/12/1899` no Desktop — por isso **NUNCA enviar datas nulas**
- CPF com zeros à esquerda some — por isso integer
- ~~Raça off-by-one~~ **FIXED 2026-07-01** — ids válidos 1,2,3,6,8,9. Parda = 8.
- ~~Tipo de identidade off-by-one~~ **FIXED 2026-07-01** — RG=1, RIC=2, RNE=3.
- ~~Categoria CNH off-by-one~~ **FIXED 2026-07-01** — endpoint renomeado `/categorias-cnh` → `/tipos-cnh`. IDs 1-9 round-trip OK.
