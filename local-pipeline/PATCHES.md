# AdmitER v2.15.0 — Interface Web (13/06/2026)

> Interface web coexistindo com Tkinter, acessível pela LAN. Sem auth (rede
> confiável). Polling segue como hoje (Task Scheduler). Reusa toda a lógica
> do pipeline existente. **211 testes + 15 subtests passando.**

## Stack escolhida (confirmada com usuário)
- Flask 3.x + HTMX 2.0.4 (self-hosted em `web/static/htmx.min.js`)
- Jinja templates, CSS único com paleta Crosara, ZERO build step
- Bind 0.0.0.0:8080 → acessível em `http://<ip-do-pc>:8080` da LAN
- Production-grade WSGI: **waitress** (fallback automático pro dev server)

## Arquivos novos
| Arquivo | Função |
|---|---|
| `webapp.py` | Flask app, endpoints, threading, CSRF, security headers |
| `dashboard_data.py` | Leitura compartilhada Tkinter↔web (dedup por entidade, payloads, ndjson via deque) |
| `web/templates/*.html` | base + 5 páginas + pendencia_detalhe + 6 partials |
| `web/static/app.css` | CSS único, paleta Crosara, WCAG AA, print-friendly |
| `web/static/htmx.min.js` | Self-hosted (não depende de CDN externa) |
| `iniciar-web.bat` + `iniciar-web-background.vbs` | Start scripts Windows |
| `web/README-WEB.md` | Docs deploy + firewall + troubleshooting |
| `test_webapp.py`, `test_dashboard_data.py` | 35 testes Flask + dashboard |

## Endpoints
- **Páginas**: `/`, `/pendentes`, `/processadas`, `/auditoria`, `/estatisticas`, `/pendencia/<msg>/<nome>/<cnpj>`
- **Ações**: POST `/atualizar-agora`, `/pendencia/.../marcar-resolvido`, `/pendencia/.../postar`
- **HTMX**: `/htmx/contadores`, `/htmx/status-passada`, `/htmx/lista-pendentes?q=`
- **JSON API**: `/api/status`, `/api/pendentes`

## Review adversarial — 3 lentes paralelas → 22 findings → fixes aplicados

| Severidade | Achado | Status |
|---|---|---|
| **BLOCKER** | Web não passava `gmail` + labels pro wrapper → ciclo do RAIMUNDO de novo (gastaria US$0,40 por pendência resolvida) | ✅ FIX — passa gmail+label_processado+label_pendente_remover |
| **BLOCKER** | "Marcar resolvido" não fechava thread Gmail | ✅ FIX — `_fechar_thread_manual` aplica processado + remove pendente |
| **HIGH** | Sem CSRF — site externo podia forçar POST via auto-submit form | ✅ FIX — `@before_request` rejeita POSTs sem Origin/Referer matching `request.host` |
| **HIGH** | Sem allowlist em `/postar` — operador podia injetar `statusadmissao=2` | ✅ FIX — `_OVERRIDES_PERMITIDOS` (16 campos) |
| **HIGH** | Sem checagem de endereço antes do POST (Tkinter checa) | ✅ FIX — `cep/rua/bairro/cidade/estado` validados pré-POST |
| **HIGH** | Salário "R$ 1.500,00" quebrava | ✅ FIX — `_parse_salario_br()` aceita formatos brasileiros |
| **HIGH** | Sanitize em `except: pass` silenciava bug | ✅ FIX — loga + aborta com erro claro |
| **HIGH** | Flask dev server em produção | ✅ FIX — waitress primeiro, fallback dev server |
| **HIGH** | "Aplicar e POSTar" / "Marcar resolvido" sem confirmação | ✅ FIX — `onclick="return confirm(...)"` em ambos |
| **HIGH** | "Atualizar agora" sem indicador de loading | ✅ FIX — `hx-disabled-elt="this"` + spinner |
| **HIGH** | Contraste WCAG AA fail em `--text-muted #7A8896` (3.48:1) | ✅ FIX — `#5A6876` (5.1:1) |
| **HIGH** | Contraste btn-primary `#D95C32` sobre branco (3.85:1) | ✅ FIX — `--orange-dark` (4.85:1) |
| **HIGH** | Path traversal lógico via `<path:nome>` | ✅ FIX — `_validar_nome_path` regex `[A-ZÀ-ÿa-zà-ÿ0-9 .'\-]{1,120}` |
| **MEDIUM** | XSS via `{{ redirect }}` em flash_full (referrer crafted) | ✅ FIX — `_safe_redirect` valida URL relativa |
| **MEDIUM** | Cooldown ausente em `/atualizar-agora` | ✅ FIX — 30s entre passadas, mensagem clara "Aguarde Xs" |
| **MEDIUM** | `auditoria_recente` lia NDJSON inteiro na RAM | ✅ FIX — `collections.deque(maxlen=N)` |
| **MEDIUM** | APP_VERSION regex aceitava só dígitos+pontos | ✅ FIX — `[^"\']+` aceita `-rc1`, `dev`, etc. |
| **MEDIUM** | Werkzeug request log expunha PII (CPFs/nomes na janela) | ✅ FIX — `werkzeug` → WARNING |
| **MEDIUM** | Form principal recarregava página inteira | ⏭ DEFERRED — ganho marginal |
| **LOW** | Headers de segurança ausentes | ✅ FIX — X-Frame-Options, X-Content-Type-Options, Referrer-Policy |
| **LOW** | `_ip_lan` podia travar 30s em rede restritiva | ✅ FIX — `settimeout(0.5)` |
| **LOW** | `send_from_directory` importado sem uso | ✅ FIX — removido |

## Smoke test final
```
12 GET routes: 200 OK (incluindo /static/htmx.min.js servido localmente)
1 GET /pendencia/.../<nome>/<cnpj>: 200 OK
Headers: X-Frame-Options=SAMEORIGIN, X-Content-Type-Options=nosniff, Referrer-Policy=same-origin
CSRF: POST sem Origin → 403 (esperado)
```

## Como começar
```bash
# Instalação (uma vez)
pip install -r requirements.txt

# Iniciar a web (clique 2x ou:)
python webapp.py

# Acessar
http://localhost:8080         # no próprio PC
http://<ip-do-pc>:8080        # de qualquer máquina da LAN
```

Detalhes em `web/README-WEB.md` (firewall, startup do Windows, troubleshooting).

## Como rodar passos em paralelo (futuro)
A Tkinter e a web compartilham `admissoes.xlsx`, `payloads/`, `idempotencia.py`,
`post_admissao.py`. Não há corrida de POST porque o wrapper único usa
`idempotencia.consultar_duplicata` antes de qualquer escrita. Operador pode
usar Tkinter no PC e outro operador a web num celular ao mesmo tempo.

---

# AdmitER v2.14.1 — Implementação Final (13/06/2026)

> Atualização sobre o pacote v2.14.0 abaixo. Itens 1–11 do plano externo
> aplicados, **176 testes passando**, `py_compile` limpo nos 10 módulos centrais.
>
> ## Resumo das mudanças sobre v2.14.0
>
> | Item | Arquivo | O que mudou |
> |---|---|---|
> | 1 | `post_admissao.py` (NOVO) | Wrapper único `postar_candidato_registrado` — idempotência → POST → registra → grava `resultado` → label Gmail → log NDJSON. Os 3 caminhos (orquestrador / UI "Enviar mesmo assim" / UI Resolver pendência) agora chamam ele. Substitui chamadas diretas a `api.post_candidato`. |
> | 1 | `main.py` `_processar_um_bloco` | Migrado pro wrapper. Reprocesso de email multi-pessoa: hits de mesma empresa retornam `pulou=True` sem POST, com `procedencia_extra="já cadastrado — POST pulado (idempotência)"`. |
> | 1 | `interface.py` | `_enviar_mesmo_assim` e `ResolverPendenciaDialog._postar` migrados. Messagebox de duplicata continua na UI; `permitir_duplicata=True` quando usuário confirma "POSTar mesmo assim". |
> | 2 | `payload_builder.py` `finalizar_payload` | Quando `departamento_id == SEM_DEPARTAMENTO`, omite a relationship `departamento` inteira. |
> | 2 | `payload_builder.py` `sanitizar_attributes` | `_normalizar_telefone` reescrito pra produzir 12-13 **dígitos puros** com prefixo `55` (PATCHES.md §4.2 supersedeu o formato `(DD)...`). `tituloeleitor` agora vira `int` (Max long do eContador). `complemento` clamped 2× pra defesa em profundidade. |
> | 2 | `test_payload_clamps.py` (NOVO) | 15 testes, incluindo os 3 casos reais de 422 + 3 testes de `SEM_DEPARTAMENTO`. |
> | 3 | `main.py` `Config` + `resolver_departamento` | Nova flag `postar_sem_departamento_quando_vazio` (default `false`). `depto_msg` agora aceita prefixo `"ok"` (REGRA 0 retorna `"ok (sem departamento — DP atribui no Desktop)"`). |
> | 4 | `main.py` `_processar_um_bloco` | Matcher por `_motivo_codigo` (enum fechado §3.8) antes do textual. Auto-resolvíveis: `CTPS_AUSENTE`, `ESTAGIO_INDEFINIDO`, `DATA_ADMISSAO_AUSENTE` (com flag). Bloqueantes: 11 códigos. Matching textual mantido como fallback pra respostas sem código. |
> | 5 | `claude_client.py` `_parsear_json` | Log explícito de "prosa antes do JSON" + parser resiliente preservado (4 estratégias em cascata: bloco ```json``` → bloco ``` → matching balanceado → primeiro `{` até último `}`). |
> | 5 | `main.py` `rodar_uma_passada` | Nova `pausa_entre_emails_segundos` (default 20) pra espaçar chamadas Claude e não estourar 30k tokens/min do tier 1. Aplica entre emails e entre threads pendentes. |
> | 6 | `interface.py` `_refresh_tabelas` + `_coletar_msg_ids_pendentes` | Dedup por entidade `(msg_id+nome+cnpj)` pegando o último evento. Aba Pendentes mostra a fila REAL (entidades abertas), não eventos históricos. Auditoria continua mostrando tudo. |
> | 7 | `post_admissao.py` (integrado no item 1) | Após sucesso, wrapper aplica `label_processado` na msg e remove `label_pendente` quando `gmail`+`label_processado` são passados. Item 7 ficou embutido no wrapper — UI passa o `GmailClient` quando há msg_id. |
> | 8 | `main.py` `_procedencia_de` + `_finalizar_lote` + `_corpo_reply_lote` | Erro HTTP do POST → `falha_tecnica=True`, marca `"Falha técnica — HTTP <código>"` na planilha. Reply ao cliente NUNCA cita falha técnica (problema é nosso). |
> | 9 | `directdata_client.py` | (a) latência <50ms loga warning específico (auth/conexão silenciosa); (b) cache negativo persistente por 7d em `directdata_neg_cache.json`; (c) flags `pis_habilitado` e `titulo_habilitado` (default `false`) — `enrichment._get_dd_client` lê do `config.json`. |
> | 10 | `test_idempotencia.py` (NOVO) | 12 testes: backfill, duplicata mesma/outra empresa, fingerprint. |
> | 10 | `test_departamento.py` (NOVO) | 8 testes: REGRA 0 com flag on/off + regressão regras 1/2/3. |
> | 10 | `test_post_admissao_integration.py` (NOVO) | 5 testes: reprocesso multi-pessoa 2 já cadastrados sem POST novo; `permitir_duplicata`; outra empresa; falha 422; label aplicada mesmo no skip. |
> | 11 | `interface.py` `APP_VERSION` | `2.14.0` → `2.14.1`. Aba Sobre ganhou 2 bullets leigos: "Conta cada pendência uma vez — não mais o histórico inteiro" e "Distingue erro técnico nosso de informação que falta do cliente". |
> | — | `config.json` | + `pausa_entre_emails_segundos: 20`, `directdata_pis_habilitado: false`, `directdata_titulo_habilitado: false`. |
>
> ## Notas operacionais
>
> - **dry_run primeiro**: rodar 1 importação manual reaproveitando um payload de
>   sucesso recente — o wrapper deve PULAR e logar `[idempotência] PULADO`.
> - **Cobaia REGRA 0** (PATCHES.md §6.5): empresa com 0 deptos + flag ON +
>   nome TESTE/COBAIA. Confirmar que payload chega no Desktop sem `departamento`.
> - **Pausa anti-429**: se Anthropic tier subir, baixar `pausa_entre_emails_segundos`
>   pra 5 ou 0 no config.
> - **PIS/TSE OFF**: ligar **separadamente** via flags do config quando o
>   índice de sucesso voltar pra >30%.
> - **Threads pendentes do JENIFFY/YURI/EDIMAURA**: o wrapper agora as fecha
>   no reprocesso (label processado aplicada mesmo no skip). Antes ficavam
>   sendo reprocessadas a cada passada.
>
> ## Migração visível pro operador
>
> - Aba Pendentes vai cair de "68 pendentes" pra ~11 (entidades abertas reais).
> - Toast "Nova pendência" não duplica mais entre tentativas do mesmo email.
> - HTTP 4xx/5xx na planilha aparece como "Falha técnica — HTTP 422", não
>   "Pendente cliente".
> - Mensagem "Já estava cadastrado" no popup quando o reprocesso pula um POST.
>
> ---
>
# AdmitER v2.14.0 — Pacote de Patches (12/06/2026)

Base: v2.13.2 (RAR enviado em 12/06). Diagnóstico nos dados reais
(`admissao_log.ndjson`, `admissoes.xlsx`, `payloads/`, telas):

| Problema | Evidência | Patch |
|---|---|---|
| Candidatos duplicados no eContador | 31 POSTs de sucesso p/ ~17 pessoas (JENIFFY 7x, EDIMAURA 6x, YURI 3x via UI sem log) | `idempotencia.py` + guardas na UI + snippet main.py |
| Reprocesso cego (churn) | RAIMUNDO: 8 tentativas idênticas no mesmo dia (~US$ 0,40/clique) | fingerprint de tabelas + aviso no botão Reprocessar |
| Empresa com 0 departamentos vira pendência | 6 das 11 pendências reais abertas | REGRA 0 em `departamento.py` (atrás de flag) |
| Auto-resolve por substring de texto livre | matcher frágil à frase do Claude | `_motivo_codigo` (enum) no `briefing.md` + snippet matcher |
| HTTP 422 recorrentes | `complemento` >40, `tituloeleitor` >12 díg., `celular` fora de 12–13 | clamps p/ `payload_builder.py` (snippet) |
| Claude respondeu prosa antes do JSON | 1 falha real de parse | briefing endurecido + snippet parser resiliente |

---

## 1. Arquivos entregues

| Arquivo | Estado | O que mudou |
|---|---|---|
| `idempotencia.py` | **NOVO** | Registro CPF+CNPJ→candidato_id (backfill automático de `payloads/`) + fingerprint de reprocesso. Testado contra os 15 payloads reais. |
| `interface.py` | patchado (v2.13.2 → **2.14.0**) | 8 edições — detalhe na seção 2 |
| `departamento.py` | patchado | REGRA 0 + sentinela `SEM_DEPARTAMENTO` + kwarg `permitir_sem_departamento` (default `False` = comportamento atual) |
| `briefing.md` | patchado | seção 3.8 `_motivo_codigo` + item 5 endurecido (saída começa com ```json) |
| `config.json` | patchado | + `"postar_sem_departamento_quando_vazio": false` ⚠ **não sobrescreva seu config de produção** — só adicione essa linha (o do RAR tem token vazio e labels JOAOMARCOS-TESTE) |
| `PATCHES.md` | este doc | — |

`funcao.py` e `salarios_padrao.py` **não foram alterados** — a regra de só
buscar nos X-marcados é decisão documentada do escritório (28/05) e está
correta; o churn da função é atacado pelo fingerprint, não pela política.

⚠ O `interface.py` entregue parte do v2.13.2 do RAR. Se sua cópia de
trabalho divergiu depois, rode um diff antes de substituir.

## 2. O que mudou no interface.py

1. `APP_VERSION = "2.14.0"`.
2. `import idempotencia` (junto dos imports de módulos locais).
3. **Reprocessar selecionada**: se nada mudou nas tabelas locais desde a
   última tentativa daquele msg_id, o diálogo de confirmação mostra o aviso
   e o default vira NÃO. Resposta nova do cliente no Gmail não entra no
   fingerprint — por isso avisa em vez de bloquear.
4. **Worker do reprocesso**: grava o fingerprint após a passada.
5. **"Enviar mesmo assim"**: consulta `idempotencia.consultar_duplicata(cpf, cnpj)`
   ANTES do POST; se já existe candidato (mesma OU outra empresa — pega typo
   de CNPJ), mostra os candidatos anteriores e o default é NÃO. Após sucesso:
   registra no índice e grava `resultado` de volta no JSON de `payloads/`.
6. **Dialog Resolver → POSTar** (form e JSON cru — ambos passam por `_postar`):
   mesma guarda pré-POST + mesmo registro/atualização pós-sucesso.
7. Aba Sobre: bullet leigo "🛡️ Bloqueia cadastros duplicados antes de enviar".

Estado novo em disco (criados sozinhos, ao lado do .py):
`candidatos_postados.json` (índice; primeira carga faz backfill de
`payloads/`) e `reprocesso_fp.json` (fingerprints). Os dois entram no
backup junto com payloads/ se você adicionar à rotina de backup.

## 3. Snippets OBRIGATÓRIOS — main.py (não veio no RAR)

Sem isto a idempotência só protege os POSTs manuais da UI. O caminho que
mais duplicou (reprocesso de email multi-pessoa) passa pelo orquestrador.

### 3.1 Idempotência no `processar_um_bloco` (CRÍTICO)

```python
import idempotencia  # topo do main.py

# Dentro de processar_um_bloco, IMEDIATAMENTE ANTES de api.post_candidato:
cpf_bloco = (payload.get("data", {}).get("attributes", {}) or {}).get("cpf")
hits = idempotencia.consultar_duplicata(cpf_bloco, cnpj)
ja = [h for h in hits if h["mesma_empresa"]]
if ja:
    log.info(f"[idempotência] {nome}: já é candidato {ja[0]['candidato_id']} "
             f"({ja[0]['ts']}) — pulando POST")
    salvar_payload(msg_id, payload, resolucao,
                   resultado={"status": "sucesso",
                              "candidato_id": ja[0]["candidato_id"],
                              "erro": None, "origem": "idempotencia_skip"})
    return {"ok": True, "candidato_id": ja[0]["candidato_id"],
            "payload": payload, "resolucao": resolucao,
            "procedencia_extra": "já cadastrado — POST pulado (idempotência)"}

resp = api.post_candidato(payload)            # ← linha existente
# logo após confirmar sucesso do POST:
idempotencia.registrar_post(cpf_bloco, cnpj, resp.candidato_id,
                            nome=nome, origem="orquestrador")
```

Efeito: reprocessar o email da EKOPLASTIC com 3 pessoas (2 OK + 1 pendente)
re-roda os 3 blocos, mas os 2 já cadastrados curto-circuitam como sucesso
sem POST — acaba o mecanismo JENIFFY/EDIMAURA. Custo Claude do reprocesso
permanece (1 chamada por email); o que zera é o dano no eContador.

### 3.2 Passar a flag de departamento

Onde `resolver_departamento(...)` é chamado:

```python
dep_id, dep_motivo = resolver_departamento(
    empresa_id, cnpj, razao_social, deptos_api,
    departamento_sugerido, paths,
    permitir_sem_departamento=getattr(
        config, "postar_sem_departamento_quando_vazio", False),
)
```

### 3.3 Matcher de auto-resolve por código (substitui substring no motivo)

Em `motivo_auto_resolvivel` (ou equivalente), antes do matching textual:

```python
AUTO_RESOLVIVEIS = {"CTPS_AUSENTE", "DATA_ADMISSAO_AUSENTE",
                    "ESTAGIO_INDEFINIDO"}  # DATA_ admissao só se flag config
BLOQUEANTES = {"SALARIO_AUSENTE", "CPF_AUSENTE_OU_INVALIDO", "RG_AUSENTE",
               "ENDERECO_INCOMPLETO", "NOME_MAE_AUSENTE",
               "NASCIMENTO_AUSENTE", "CARGO_AUSENTE",
               "CARGOS_DIVERGENTES_SEM_ASO", "DOC_ILEGIVEL",
               "DOCS_PESSOA_AUSENTES", "CNPJ_NAO_LOCALIZADO"}

cod = str(bloco.get("_motivo_codigo") or "").upper()
cods = {cod, *map(str.upper, bloco.get("_motivos_codigos") or [])} - {""}
if cods:
    if cods & BLOQUEANTES:
        return False              # pendência genuína
    if cods <= AUTO_RESOLVIVEIS:  # todos os motivos são auto-resolvíveis
        return True
# sem código (resposta antiga/cache) → cai no matching textual atual
```

Obs.: `SALARIO_AUSENTE` continua bloqueante AQUI de propósito — quem o
resolve é o passo de salário padrão que roda antes (4.4.3); se chegou no
matcher ainda pendente, é pendência real (regra 11).

## 4. Snippets — payload_builder.py (não veio no RAR)

### 4.1 Omitir relationship quando vier `SEM_DEPARTAMENTO`

```python
from departamento import SEM_DEPARTAMENTO

# onde injeta os relationships resolvidos:
if departamento_id and departamento_id != SEM_DEPARTAMENTO:
    rels["departamento"] = {"data": {"type": "departamentos",
                                     "id": str(departamento_id)}}
# SEM_DEPARTAMENTO → simplesmente NÃO cria a chave "departamento"
```

### 4.2 Clamps dos 422 reais (em `sanitizar_attributes`)

```python
# HTTP 422 javax.validation.constraints.Size /complemento: 0–40
if attrs.get("complemento"):
    attrs["complemento"] = str(attrs["complemento"])[:40]

# HTTP 422 Max /tituloeleitor: ≤ 999999999999 (12 dígitos)
if attrs.get("tituloeleitor") is not None:
    te = re.sub(r"\D", "", str(attrs["tituloeleitor"]))
    if te and len(te) <= 12:
        attrs["tituloeleitor"] = int(te)
    else:
        attrs.pop("tituloeleitor", None)  # inválido → omite, DP completa

# HTTP 422 Size /celular: 12–13 chars. 10–11 dígitos (DDD+número) → prefixa 55
for campo in ("celular", "telefone"):
    if attrs.get(campo):
        dig = re.sub(r"\D", "", str(attrs[campo]))
        if 10 <= len(dig) <= 11:
            dig = "55" + dig
        if 12 <= len(dig) <= 13:
            attrs[campo] = dig
        else:
            attrs.pop(campo, None)  # fora do padrão → omite (é opcional)
```

(`telefone` entrou por simetria — mesma família de constraint. Se o POST de
cobaia mostrar que `telefone` aceita outro tamanho, remova-o do loop.)

## 5. Snippet — claude_client.py (parser resiliente)

Falha real: resposta abriu com "Vou analisar todos os documentos..." e o
parse quebrou. Além do briefing endurecido, fallback barato no cliente:

```python
def _extrair_json(texto: str) -> dict:
    t = texto.strip()
    if "```" in t:                       # pega o conteúdo do bloco cercado
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.S)
        if m:
            t = m.group(1)
    if not t.startswith("{"):            # prosa antes do JSON → corta
        i = t.find("{")
        if i >= 0:
            t = t[i:]
    fim = t.rfind("}")
    if fim >= 0:
        t = t[: fim + 1]
    return json.loads(t)
```

## 6. Plano de teste e rollout (na ordem)

1. `python -m pytest test_*.py` (suite existente — não veio no RAR, rodar aí)
2. `python -m py_compile interface.py main.py claude_client.py departamento.py idempotencia.py` ✓ (feito aqui nos arquivos disponíveis)
3. Subir com `dry_run=true` → "Importar arquivos" com um caso já cadastrado
   (ex.: reimportar o email da INGRIDE) → deve aparecer o aviso de duplicata
   na UI e, com o snippet 3.1 aplicado, o orquestrador deve logar
   "[idempotência] ... pulando POST".
4. **Reprocessar 1 pendência interna duas vezes seguidas** → na 2ª o diálogo
   tem que abrir com o aviso "Nada mudou nas tabelas...".
5. **Validar SEM_DEPARTAMENTO com cobaia** (nunca postamos sem depto em
   produção — não há nenhum payload de sucesso sem a relationship):
   a. ligar `postar_sem_departamento_quando_vazio: true`
   b. POSTar 1 cobaia (nome TESTE/COBAIA) numa empresa com 0 deptos
   c. conferir se desce pro Alterdata Desktop e se o DP consegue atribuir
      o departamento lá; só então deixar a flag ligada
   d. se a API recusar (422 em relationships/departamento), manter flag
      OFF e seguir só com a saída rápida: cadastrar depto + Reprocessar
6. Release: APP_VERSION já bumpado p/ 2.14.0; rebuild do .exe (PyInstaller)
   — `idempotencia.py` é import novo, entra no bundle automaticamente.

## 7. Fora do código (faz hoje, sem release)

1. **Cadastrar 1 departamento** (ex.: GERAL) nas 4 empresas travadas —
   Metalúrgica Santana (1145), Basic Store (1178), Eletrosul (59),
   Moto Brasil FL02 (982) — e clicar "Reprocessar email" em cada pendência.
   Zera 6 das 11 abertas sem nenhum patch.
2. **Excluir os duplicados no eContador**: filtrar candidatos das pessoas
   JENIFFY/JENNIFY, EDIMAURA/EDMAURA, LUANA, GABRIELY, YURI e manter só o
   mais recente de cada (regra da casa: confirmar lista antes de DELETE).
3. **Subir o tier Anthropic** (comprar créditos): os 3 erros 429 são do
   limite Tier 1 de 30k input tokens/min — briefing + anexos × 2 chamadas
   estoura com 2 emails seguidos.
4. **DirectData**: latência média de 3 ms no Cadastro Básico = falha local
   instantânea (auth/exceção engolida), não resposta da API — investigar o
   directdata_client; desligar PIS (7% sucesso, R$ 14 gastos) e Título
   Eleitor (30%, 37 s) até lá; e cachear falha por CPF por alguns dias
   (194 chamadas pagas para ~45 admissões = retentativa paga repetida).
5. Rotular falha de POST como **falha técnica**, não "Pendente cliente"
   (os 3 HTTP 422 saíram como pendência de cliente na auditoria — se o
   auto-email ligar um dia, o cliente recebe cobrança por erro nosso).
