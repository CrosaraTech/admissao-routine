# Resumo da Migração — AdmitER

> Cole esse arquivo no primeiro turno do Claude Code no servidor. Ele dá
> contexto suficiente pra retomar de onde paramos sem reler todo o histórico.

## Versão atual

**v2.16.42** (interface.py:68)

## Mudanças recentes (chat anterior, 2026-06-23/24)

| Ver | O que entrou | Onde |
|---|---|---|
| 2.16.37 | Botão **🗑 Excluir** por linha em `/pendentes` — remove tudo (payloads, planilha, rascunhos, fingerprint, labels Gmail). Útil pra falhas técnicas | `webapp.py` rota `/pendencia/<msg_id>/excluir` · `_partials/lista_pendentes.html` · `app.css` classe `.btn-danger-text` |
| 2.16.37 | Log de retry agora distingue **"Overload Anthropic (529)"** de **"Rate limit (429)"** | `claude_client.py:1231-1276` |
| 2.16.38 | **Validação DV de CPF** como tiebreak quando 2 chamadas do Claude divergem (caso JOSÉ DIVINO: `01527367193` válido vs `15273671193` alucinado) | `claude_client.py:1485-1530` (`_cpf_dv_valido`, `_cpf_da_resposta`) + `_mesclar_respostas` |
| 2.16.39 | **Salário fixo por cargo** no perfil do remetente — sobrescreve média histórica. UI com input editável + Salvar por linha | `perfis_remetente.py` (`salarios_manuais_por_cargo` + `atualizar_salario_manual_cargo`) · `webapp.py` rota `/perfis/<r>/salario-cargo` · `perfil_detalhe.html` |
| 2.16.40 | **ViaCEP automático** no postar — quando CEP veio mas falta rua/bairro/cidade, consulta e preenche | `webapp.py:1643-1678` |
| 2.16.41 | **Fallback BrasilAPI** quando ViaCEP falha. Cobre CEPs genéricos (ex: 76300-000 Ceres dá pelo menos cidade+UF) | `enrichment.py` (`_enrich_from_cep_brasilapi`) |
| 2.16.42 | **Normalização universal de datas BR→ISO** em `sanitizar_attributes`. Fixa HTTP 500 do tipo "Cannot deserialize LocalDate from '08/09/1988'" (caso WILDA ROSA) | `payload_builder.py:517-557` |

## Pendências reais em aberto (próxima sessão)

- **WILDA ROSA DA SILVA** (CPF 88172279191) — pendência técnica de 24/06 15:25:26 por HTTP 500 do `dataidentidade='08/09/1988'`. **Pode ser reprocessada agora** pelo botão "🔄 Reprocessar email" — fix da 2.16.42 cobre.
- **JOSÉ DIVINO ANTONIO MARQUES** (CPF 01527367193, Mercafrutas CNPJ 26742569000116) — pendência cliente por salário. Operador subiu manual em 24/06 mas perfil da Mercafrutas ainda **não tem salário manual cadastrado**. Próxima admissão sem salário cai pendência de novo. Sugestão: cadastrar `MOTORISTA/ENTREGADOR` no perfil com valor real (UI implementada na 2.16.39).
- **CEP 76300-000** (Ceres-GO) — endereço da Mercafrutas. ViaCEP+BrasilAPI dão só cidade+UF, sem rua. Soluções: cadastrar **endereço padrão por CNPJ** (já existe na 2.16.27, botão na pendência), ou pedir comprovante real ao cliente.

## Padrões aprendidos importantes (do CLAUDE.md auto-memory)

- `statusadmissao = 1` **SEMPRE** — único valor que faz candidato descer pro Desktop. NÃO mudar.
- `numero` do endereço é Integer obrigatório — `0` quando sem número (não "SN" string).
- `diascontratoexperiencia = 30` default; UI eContador calcula prorrogação = 90-30.
- `tipoidentidade = 1` (off-by-one, faz UI mostrar "RG").
- `raca = 4` (Parda, default escritório).
- Bug do sync: cor/raça, depto fl 1139, tipo identidade vazio etc — DP preenche manual no Desktop.
- API `/funcionarios` é **read-only via API** — só Desktop preenche.
- `filter[empresa.id]=X` funciona; `filter[nome]=Y` NÃO funciona (filtrar client-side).
- API eContador valida CPF como cadastro pessoa-física também (produtor rural, etc).

## Caminho que o sistema toma quando recebe email

```
PASSO 1 main.py fetch  → baixa anexos
PASSO 2 Claude lê PDFs/IMGs com Vision nativo
PASSO 3 escreve campos.json
PASSO 4 main.py resolve <cnpj> <cargo> <depto>
PASSO 5 main.py montar-payload (aplica sanitizar_attributes)
PASSO 6 main.py post
PASSO 7 main.py finalizar <sucesso|pendente>
```

## Arquivos sensíveis (NÃO commitar)

- `.env` (tokens ECONTADOR, GMAIL, ANTHROPIC, DIRECTDATA)
- `admissoes.xlsx` (PII de funcionários)
- `payloads/` (PII)
- `rascunhos/` (PII)
- `perfis_remetente.json` (emails de clientes)
- `econtador_audit.ndjson` · `billing.ndjson` · `directdata_audit.ndjson`
