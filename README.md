# admissao-routine

Pipeline de admissão automática da **Crosara Contabilidade**, projetado pra rodar em **Claude Code Routines** (nuvem Anthropic).

Processa e-mails com label `ADMISSÃO` no Gmail. O agente Claude Code lê os
anexos com **Vision nativo** (tool `Read`), extrai os campos, monta payload
JSON:API e cria candidatos via API E-plugin Alterdata.

## Arquitetura

`main.py` é um **helper de I/O** — não chama API Anthropic separadamente.
A inteligência (classificar docs, extrair campos) é feita pelo Claude Code
diretamente via Vision nativo. `main.py` só expõe subcomandos CLI:

```
python main.py fetch                              # baixa anexos do Gmail
python main.py resolve <cnpj> <cargo> [<depto>]   # resolve IDs
python main.py montar-payload <campos.json> ...   # monta payload JSON:API
python main.py post <payload.json>                # POSTa /candidatos
python main.py finalizar <msg_id> ...             # label Gmail + email DP
```

Veja `CLAUDE.md` pro fluxo completo passo-a-passo.

## Estrutura

| Arquivo | Função |
|---|---|
| `main.py` | Helper de I/O (CLI com subcomandos) |
| `CLAUDE.md` | Instruções operacionais do agente (fluxo, regras, bugs) |
| `lookups.json` | Enums + defaults + workarounds dos bugs do produto |
| `departamentos.json` | Mapa CNPJ → modo (único / múltiplo) |
| `config.json` | Configuração não-secreta (URLs, labels, email DP) |

## Pré-requisitos

```bash
pip install -r requirements.txt
```

E **antes da primeira execução**:

1. Configurar variáveis de ambiente (Claude Code Routines: como Secrets; local: no `.env`):
   - `ECONTADOR_TOKEN` — token JWT da API E-plugin Alterdata
   - `GMAIL_TOKEN` — JSON string com credenciais OAuth do Gmail. Formato:
     ```json
     {
       "token": "ya29....",
       "refresh_token": "1//0...",
       "token_uri": "https://oauth2.googleapis.com/token",
       "client_id": "....apps.googleusercontent.com",
       "client_secret": "GOCSPX-...",
       "scopes": [
         "https://www.googleapis.com/auth/gmail.readonly",
         "https://www.googleapis.com/auth/gmail.modify",
         "https://www.googleapis.com/auth/gmail.send"
       ]
     }
     ```
     Gere via fluxo OAuth do Google Cloud Console uma vez e cole o JSON serializado.
     O token é auto-refreshed em runtime via `refresh_token`.
2. Editar `config.json`: ajustar `dp.email_notificacao`
3. Popular `departamentos.json` com os CNPJs reais
4. (Opcional) Setar `"dry_run": true` em `config.json` pra testar sem postar

## Como rodar

Em ambiente Claude Code Routines: o agente Claude Code orquestra `main.py`
seguindo as instruções de `CLAUDE.md`.

Para teste local manual:
```bash
python main.py fetch
# inspecione o JSON e cada PDF baixado
python main.py resolve 12345678000190 "Auxiliar Administrativo"
# monte campos.json manualmente
python main.py montar-payload campos.json 89 12345 > payload.json
python main.py post payload.json
```

Logs append-only em `admissao_log.json` (NDJSON).

## Regras críticas implementadas

Todas as 21+ correções/limitações documentadas em `CLAUDE.md` e `lookups.json:bugs_conhecidos`:

- `statusadmissao = "1"` (Análise — desce direto pro Alterdata, validado por 5 admissões reais)
- `tipoidentidade = "1"` (workaround off-by-one — Desktop renderiza "RG")
- `raca = "4"` (default Parda — API armazena correto; DP corrige no Desktop se UI exibir vazio)
- CPF como integer (Java rejeita string)
- `numero` zero ou ausente → omitir o campo
- Datas nulas → omitir o campo (Desktop transforma `null` em `30/12/1899`)
- PIS como string (preserva zeros à esquerda)
- Telefone/celular: sem hífens, 12–13 chars
- Bancário: tudo-ou-nada
- CTPS gerada do CPF se não vier (`int(CPF[:7])`, série = `CPF[7:11]`)
- Match fuzzy de função: ≥80% usa, 40–80% pendência com sugestão, <40% pendência sem cadastro

## Bugs conhecidos do produto E-plugin (manuais pro DP)

Ver `lookups.json:bugs_conhecidos`. 9 bugs do sync + 12 campos sem atributo no payload = ~21 ajustes manuais por admissão no Alterdata Desktop.

## Segurança

- `.env`, `gmail_token.json`, `credentials.json`, `*_log.json` estão no `.gitignore`
- Tokens (`ECONTADOR_TOKEN`, `GMAIL_TOKEN`) nunca em arquivos commitados — sempre via env var/secrets
- Logs podem conter CPF/nomes — não compartilhar fora do escritório
