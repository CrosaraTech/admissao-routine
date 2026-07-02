"""Cliente da API Claude com Vision — gera payload a partir de email + anexos.

O briefing completo (regras, lookups, bugs conhecidos) está em briefing.md.
Esse arquivo é montado como system prompt e enviado em toda chamada.

Fluxo:
  - Recebe corpo do email (texto) + lista de anexos (filename, mime, bytes)
  - Constrói uma mensagem multi-content com:
      • Texto: corpo + metadados + cargo/CBO sugeridos (se houver)
      • image_*/document_* blocks pra cada anexo, em base64
  - Envia pro Claude e parseia o JSON retornado dentro de bloco ```json
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import time
from pathlib import Path

import anthropic

try:
    import pymupdf  # type: ignore
    _HAS_PYMUPDF = True
except ImportError:
    _HAS_PYMUPDF = False


log = logging.getLogger("admissao.claude")


def _achar_fim_objeto(s: str, inicio: int) -> int:
    """Dado o índice de um `{` em `s`, retorna o índice do `}` correspondente
    (matching balanceado, respeitando strings e escapes). Retorna -1 se não
    encontrar. Permite extrair JSON válido mesmo com texto narrativo depois.
    """
    if inicio < 0 or inicio >= len(s) or s[inicio] != "{":
        return -1
    depth = 0
    em_string = False
    escape = False
    for i in range(inicio, len(s)):
        c = s[i]
        if escape:
            escape = False
            continue
        if em_string:
            if c == "\\":
                escape = True
            elif c == '"':
                em_string = False
            continue
        if c == '"':
            em_string = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


# Limites pra fallback 413 — Anthropic limita request body a ~32MB.
# Base64 infla 33%, entao o conteudo raw seguro fica em ~24MB.
LIMITE_RAW_MB_SEGURO = 24
DPI_COMPRESSAO_FALLBACK = 120  # PDFs originais geralmente sao 300 DPI


def _comprimir_pdf(data: bytes, dpi: int = DPI_COMPRESSAO_FALLBACK) -> bytes:
    """Re-renderiza um PDF em DPI menor pra reduzir tamanho ao mandar pro Claude.

    Cada pagina vira uma imagem JPEG (qualidade 80) embutida num novo PDF.
    Reducao tipica: 60-85%. Preserva legibilidade pra Vision do Claude.

    Se pymupdf nao estiver instalado ou houver erro de parse → devolve original.
    """
    if not _HAS_PYMUPDF:
        return data
    try:
        with pymupdf.open(stream=data, filetype="pdf") as src:
            novo = pymupdf.open()  # PDF novo, vazio
            try:
                zoom = dpi / 72.0
                mat = pymupdf.Matrix(zoom, zoom)
                for page in src:
                    pix = page.get_pixmap(matrix=mat, alpha=False)
                    img_bytes = pix.tobytes("jpeg", jpg_quality=80)
                    rect = page.rect
                    nova_page = novo.new_page(width=rect.width, height=rect.height)
                    nova_page.insert_image(rect, stream=img_bytes)
                buf = io.BytesIO()
                novo.save(buf, garbage=4, deflate=True)
                return buf.getvalue()
            finally:
                novo.close()
    except Exception as e:
        log.warning(f"   ⚠ Falha comprimindo PDF: {type(e).__name__}: {e}")
        return data


def _tamanho_total_mb(anexos: list[dict]) -> float:
    """Tamanho raw total dos anexos em MB."""
    return sum(len(a.get("data") or b"") for a in anexos) / (1024 * 1024)


# PDFs menores que isso ja vem leves — re-renderizar so piora (transforma
# texto vetorial em imagem JPEG, ficando MAIOR). So comprime acima do limiar.
LIMIAR_COMPRESSAO_BYTES = 500 * 1024  # 500 KB


def _comprimir_anexos(anexos: list[dict]) -> list[dict]:
    """Aplica _comprimir_pdf em PDFs >500KB. Mantem PDFs pequenos e imagens
    intactos. Marca cada anexo com `_comprimido=True` apos compressao bem
    sucedida pra evitar recompressao em chamadas subsequentes.

    Retorna NOVA lista (nao modifica in-place)."""
    out: list[dict] = []
    economia_total = 0
    for a in anexos:
        # Ja comprimido antes (mesma instancia do dict) ou nao-PDF? passa direto
        if a.get("_comprimido") or a.get("mime") != "application/pdf" or not a.get("data"):
            out.append(a)
            continue
        antes = len(a["data"])
        if antes < LIMIAR_COMPRESSAO_BYTES:
            # PDF pequeno — recompressao tipicamente aumenta. Pula.
            out.append(a)
            continue
        comp = _comprimir_pdf(a["data"])
        depois = len(comp)
        if depois >= antes:
            # Compressao nao ajudou (pode acontecer com PDFs ja otimizados).
            # Mantem o original.
            log.info(
                f"     '{a.get('filename')}': sem ganho ({antes/1024/1024:.1f}MB) — mantido"
            )
            out.append(a)
            continue
        economia_total += (antes - depois)
        log.info(
            f"     compressao '{a.get('filename')}': "
            f"{antes/1024/1024:.1f}MB → {depois/1024/1024:.1f}MB "
            f"({(1 - depois/antes) * 100:.0f}% menor)"
        )
        novo = dict(a)
        novo["data"] = comp
        novo["_comprimido"] = True
        out.append(novo)
    log.info(f"   Total economizado: {economia_total/1024/1024:.1f} MB")
    return out


def _agrupar_anexos(anexos: list[dict]) -> dict[str, list[dict]]:
    """Agrupa anexos pelo campo 'grupo' (vem do gmail_client).
    Grupo '' (avulsos) vai junto. Cada chave do retorno = uma chamada Claude."""
    grupos: dict[str, list[dict]] = {}
    for a in anexos:
        chave = a.get("grupo") or "_avulsos"
        grupos.setdefault(chave, []).append(a)
    return grupos


ROOT = Path(__file__).parent
BRIEFING_FILE = ROOT / "briefing.md"


# Preços oficiais em USD por 1 milhão de tokens (input, output).
# Fonte: https://www.anthropic.com/pricing — atualizar quando mudar.
PRICING_USD_POR_MTOK: dict[str, tuple[float, float]] = {
    # Opus 4.x (premium)
    "claude-opus-4-7":           (15.0, 75.0),
    "claude-opus-4-6":           (15.0, 75.0),
    "claude-opus-4-5":           (15.0, 75.0),
    # Sonnet 4.x (mainstream)
    "claude-sonnet-4-6":         (3.0, 15.0),
    "claude-sonnet-4-5":         (3.0, 15.0),
    "claude-sonnet-4-20250514":  (3.0, 15.0),  # Sonnet 4 (modelo deste pipeline)
    # Haiku 4.5 (econômico)
    "claude-haiku-4-5":          (1.0, 5.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}


def _precos_do_modelo(model: str) -> tuple[float, float]:
    """Retorna (preço_input, preço_output) em USD/MTok pro model dado.
    Fallback: Sonnet 4 ($3/$15) se modelo não conhecido."""
    if model in PRICING_USD_POR_MTOK:
        return PRICING_USD_POR_MTOK[model]
    # Tenta match por família (sonnet/opus/haiku)
    m_lower = model.lower()
    if "opus" in m_lower:
        return (15.0, 75.0)
    if "haiku" in m_lower:
        return (1.0, 5.0)
    return (3.0, 15.0)  # default Sonnet


def _esperar_retry_after(exc: "anthropic.APIStatusError", tentativa: int) -> float:
    """Calcula segundos pra esperar antes de retentar uma chamada 429.

    Prioridade:
      1. Header `retry-after` da resposta (Anthropic envia em alguns casos)
      2. Backoff exponencial: 5, 15, 45 segundos (cap 60)
    """
    try:
        resp = getattr(exc, "response", None)
        if resp is not None:
            headers = getattr(resp, "headers", {}) or {}
            ra = headers.get("retry-after") or headers.get("Retry-After")
            if ra:
                segundos = float(ra)
                if 1 <= segundos <= 300:
                    return segundos
    except (ValueError, TypeError, AttributeError):
        pass
    # Backoff: 5s, 15s, 45s — capa em 60
    return min(5.0 * (3 ** tentativa), 60.0)


def _eh_429(exc: Exception) -> bool:
    """True se é rate limit (429) ou erro 'overloaded' do Anthropic."""
    if isinstance(exc, anthropic.RateLimitError):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        if getattr(exc, "status_code", None) == 429:
            return True
    msg = str(exc).lower()
    return "rate_limit" in msg or "exceed your org" in msg or "overloaded" in msg


# Tipos MIME aceitos pelo bloco image/document do Anthropic
IMAGE_MIMES = {"image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp"}
DOC_MIMES = {"application/pdf"}

# Mapeia extensão → MIME canônico aceito pela Anthropic API. Usado como fallback
# quando o cliente de email manda MIME genérico (octet-stream, vazio) ou variantes
# obscuras (x-pdf, acrobat). NÃO inclui heic/bmp/tiff — esses NÃO são suportados
# pelo Anthropic Vision e exigem conversão (não implementado ainda).
EXT_TO_MIME_CANONICO = {
    ".pdf":  "application/pdf",
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif":  "image/gif",
    ".webp": "image/webp",
}


def _normalizar_mime_anthropic(filename: str, mime_recebido: str) -> str | None:
    """Retorna o MIME canônico que a Anthropic API aceita, ou None se não dá.

    Cobre o caso Outlook (Doc. Pedro.pdf chegou como octet-stream) e outros
    clientes que mandam MIME genérico/obscuro. Estratégia em 3 níveis:

      1. MIME já está na whitelist (IMAGE_MIMES/DOC_MIMES) — devolve direto
      2. Variante de PDF (application/x-pdf, application/acrobat, etc.) — normaliza
      3. MIME inútil (octet-stream, vazio, desconhecido) — infere pela extensão

    Não detecta por magic bytes — confia na extensão (suficiente pro contexto)."""
    if mime_recebido in IMAGE_MIMES or mime_recebido in DOC_MIMES:
        return mime_recebido
    # Variantes de PDF — todas viram application/pdf
    if mime_recebido in {"application/x-pdf", "application/acrobat",
                         "application/vnd.pdf", "text/pdf", "text/x-pdf"}:
        return "application/pdf"
    # Fallback por extensão (Outlook octet-stream, MIME vazio, sistemas internos)
    ext = os.path.splitext(filename.lower())[1]
    return EXT_TO_MIME_CANONICO.get(ext)


SYSTEM_SUFIXO = """

---

## INSTRUÇÕES DE SAÍDA (Pipeline Local)

Você está rodando dentro do pipeline local da Crosara Contabilidade.

- Receba o CORPO DO EMAIL (texto) e os ANEXOS (PDFs/imagens).
- Extraia TODOS os campos seguindo o briefing acima.
- Retorne APENAS o JSON do payload, dentro de um bloco ```json ... ```.
- NÃO inclua comentários, explicação ou texto antes/depois do JSON.

## LEITURA CUIDADOSA DE IMAGENS DE DOCUMENTOS

As fotos de RG, CPF, CTPS, título eleitoral etc. frequentemente vêm:
  - ROTACIONADAS (90/180/270 graus) ou de cabeça pra baixo
  - Tortas, com luz ruim, manchadas
  - Com texto em MAIÚSCULAS pequenas (cartão de identidade)
  - Com frente E verso na mesma imagem (cada lado em uma orientação)

ANTES de marcar campo como "não encontrado", examine CADA imagem em
TODAS as orientações possíveis. Procure ativamente por:
  - `identidade` (RG): número com 7+ dígitos, frequentemente rotulado
    "REGISTRO GERAL", "RG" ou "Nº". Ex: "9.462.146" ou "94.62146"
  - `dataidentidade`: rotulada "DATA DE EXPEDIÇÃO", formato DD/MM/AAAA
  - `orgaoemissoridentidade`: sigla do órgão, ex: "SSP/SP", "SDS/PE",
    "SSP/GO", "SECC/RJ"
  - `nascimento`: rotulada "DATA DE NASCIMENTO", formato DD/MM/AAAA
  - `nomedamae`: rotulado "FILIAÇÃO" ou "MÃE" (geralmente vem 2 nomes
    — pai primeiro, mãe segundo, ou vice-versa)
  - `nomedopai`: o OUTRO nome da FILIAÇÃO. **SEMPRE EXTRAIA SE APARECER**
    no RG ou certidão — mesmo sendo "opcional" pro fluxo, é gratuito incluir
    e poupa o DP de preencher manual depois. Só omita se não houver pai
    listado nos docs.
  - `naturalidade`: rotulada "NATURALIDADE" — cidade + UF (ex: "Itacuruba-PE")

Se um campo está VISÍVEL na imagem, EXTRAIA mesmo que a imagem esteja
inclinada/girada. Só marque como faltante se realmente não conseguir ler.

## CAMPOS GERADOS AUTOMATICAMENTE — NÃO MARQUE PENDENTE POR ELES

Os campos abaixo são DERIVADOS automaticamente pelo pipeline depois que
você responde. Se eles estiverem ausentes nos documentos, NÃO marque
_pendente=true por causa disso — o backend preenche sozinho:

  - `ctps` = `int(CPF[:7])` (derivado do CPF)
  - `seriectps` = `CPF[7:11]` (derivado do CPF)
  - `ufctps` = mesma UF da identidade (relationship)
  - `dataterminocontrato` = `admissao + diascontratoexperiencia` (calculado)
  - `statusadmissao`, `tipoadmissao`, `categoriawdp`, `tipovinculotrabalhista`,
    `tipoidentidade`, `raca`, `nacionalidade`, `paisnascimento`, `pais`,
    `formapagamento`, `estadocivil`, `escolaridade`, `tipoDeDeficiencia`,
    `statusatestadoocupacional` — todos têm DEFAULT do escritório

EXEMPLO: cliente envia documentos sem CTPS física? Não é problema — extraia
o resto normalmente. O pipeline gera CTPS do CPF.

## CAMPOS OPCIONAIS — NÃO MARQUE _pendente=true POR AUSÊNCIA DESSES

Os campos abaixo são RECOMENDADOS mas NÃO BLOQUEIAM admissão. Se faltar,
**omita do payload** (não envie null/vazio) e **NÃO marque `_pendente=true`**:

  - `dataatestadoocupacional` (ASO admissional) — DP completa manual no Desktop.
    Mesmo que o email mencione "exame feito" sem anexo do ASO, NÃO PEÇA o anexo
    via `_pendente`. Apenas omita o campo.
  - `email`, `telefone`, `celular` — opcionais; omita se não vier
  - `pis`, `datapis` — só envie se aparecer em algum doc (CTPS, Caixa Tem)
  - `cnh*` (todos os campos de CNH) — só se a CNH foi anexada
  - `tituloeleitor`, `zonatituloeleitor`, `secaotituloeleitor` — só se anexado
  - `banco`, `agencia`, `conta`, `tipoconta` — só se algum aparecer; tudo-ou-nada
  - `complemento` do endereço — só se aparecer explícito
  - `nomedopai` — extraia se em FILIAÇÃO do RG/certidão; senão omita
  - `nomesocial`, `apelido`, `requersegurodesemprego`, `observacao`, `ocorrencia`

REGRA: `_pendente=true` só pra os campos OBRIGATÓRIOS DE VERDADE (nome, CPF,
admissao, salario, nascimento, mãe, endereço básico, cargo, RG, CNPJ empresa).
Os opcionais acima são "preferíveis ter" mas não bloqueiam — omita sem drama.

## ESTAGIÁRIO É ADMISSÃO NORMAL — PROCESSA SEM MARCAR PENDENTE

⚠️ **REGRA OPERACIONAL CRÍTICA**: estagiário (Lei 11.788/2008, termo de
compromisso de estágio, TCE, bolsa-estágio, CIEE, NUBE) **É ADMISSÃO NORMAL
NO eContador**. Não importa que juridicamente NÃO seja vínculo CLT nem gere
evento eSocial S-2200 — o pipeline do escritório TRATA estagiário como
admissão regular, com função própria (ex: "ESTAGIÁRIO DE LOJA", "ESTÁGIO EM
SUPERMERCADO") que o cliente cadastrou no eContador.

✅ **QUANDO O EMAIL/ANEXOS INDICAM ESTÁGIO:**
- Extraia TODOS os campos normalmente (nome, CPF, salário/bolsa, endereço, etc.)
- Use o cargo do email/documentos (ex: "AUXILIAR DE LOJA") em `nomecargo`
- O pipeline **detecta automaticamente** que é estágio e mapeia pra função
  correta de estágio do cliente (alias separado)
- Bolsa-estágio vai em `salario` mesmo (valor numérico)
- **NÃO MARQUE `_pendente=true`** com motivos como:
  - "Contrato de estágio não é vínculo CLT"
  - "Não gera payload de admissão eSocial S-2200"
  - "Estágio não pode ser admissão"
  - "Termo de compromisso não gera S-2200"

❌ **CASO REAL (JESSYKA 11/06/2026)**: Claude marcou pendente cliente dizendo
*"Contrato de estágio (Lei 11.788/2008) NÃO é vínculo CLT — não gera payload
de admissão eSocial S-2200"*. **ISSO ESTÁ ERRADO**. Mesmo que juridicamente
você esteja certo, o pipeline da Crosara processa estagiários como admissão
no eContador. O sistema sabe disso — apenas extraia os dados e siga.

**Regra mental**: se a empresa enviou docs pra cadastrar a pessoa, ela quer
que apareça no sistema dela. Estagiário aparece via admissão também.

## SALÁRIO — REGRA ESPECIAL DE EXTRAÇÃO

⚠️ **NUNCA DEDUZA, INFIRA OU CHUTE SALÁRIO.** Esse é o campo onde inventar dado
faz dano direto ao funcionário (e ao escritório legalmente). Caso real
(GABRIELY 10/06/2026): email dizia apenas `"SALARIO BASE"` como label, sem
valor numérico — Claude não pode chutar salário mínimo, nem o piso da
categoria, nem nada. **Tem que virar pendência cliente.**

✅ **EXTRAIA SALÁRIO APENAS QUANDO:**

  1. **Valor numérico explícito**: "R$ 1.842,26", "1842.26", "salário 2000",
     "remuneração 1621,00", "salário mensal R$ 1.500", etc.
  2. **Menção explícita a "salário mínimo"** (e variações: "1 SM",
     "um salário mínimo", "piso do salário mínimo nacional", "SM").

     ⚠️ **VALOR ATUAL DO SALÁRIO MÍNIMO BRASILEIRO: R$ 1.621,00** (vigente
     em 2026). Quando o texto disser "salário mínimo", use **EXATAMENTE
     R$ 1.621,00** no campo `salario`. NÃO use valores antigos como 1518,
     1412, 1320 — esses são salários mínimos de anos anteriores e NÃO
     valem mais.

     Mas só quando o texto DIZ "salário mínimo" — não infira de "salário
     base" nem de cargo de baixa renda.
  3. **Piso de categoria/sindicato citado com valor**: "salário R$ 1.650,00
     (piso comerciário GO)" — extrai 1650.00.

❌ **NUNCA EXTRAIA SALÁRIO POR DEDUÇÃO:**

  - "SALARIO BASE" sem valor numérico → OMITE + `_pendente=true` motivo
    "Salário não informado — email/ficha menciona 'salário base' sem valor.
    Favor informar o salário contratual."
  - "Salário compatível com o cargo" / "salário a combinar" → OMITE + pendente
  - "Salário mais benefícios" sem número → OMITE + pendente
  - Cargo de baixa qualificação (ex: ajudante, auxiliar) NÃO presume mínimo
  - Ficha de admissão com campo "Salário" em branco → OMITE + pendente
  - Holerite antigo de OUTRA empresa → NÃO usar (é histórico, não atual)

**Por que essa regra é estrita**: salário errado no cadastro vira:
  - Pagamento errado (RH paga menos que o combinado)
  - Folha incorreta (impostos calculados errado)
  - Acordo de trabalho viciado (CLT exige salário acordado, não chutado)

Pendência cliente é R$ 0,15 e 1 reply. Salário errado pode virar processo
trabalhista. **Sempre prefira a pendência.**

## NÃO PEÇA "CONFIRMAÇÃO CRUZADA" — É PARANOIA E NÃO AJUDA

Caso real (v2.4.4): Claude marcou pendente dizendo *"CPF documento visível mostra
'715.770.281-98' mas precisa de confirmação cruzada pois RG traseiro está
parcialmente legível"*. **ISSO ESTÁ ERRADO**. Se você LEU o CPF em UMA fonte
clara (CPF físico, CNH, Caixa Tem, qualquer doc), USE ESSE VALOR.

⚠️ **REGRAS DE OURO**:

  1. **Se você LEU o campo em UMA fonte legível → USE.** Não exija que apareça
     em 2+ documentos. Documentos brasileiros são chatos, nem sempre repetem
     a mesma info.
  2. **NÃO marque pendente pedindo "confirmar"** algo que você já tem.
     Se está no payload, está extraído. Confiança alta.
  3. **NÃO marque pendente pedindo "validar"** ou "verificar" — você é o
     extrator, não o validador. O backend faz sanity check.
  4. **Data de admissão inferida de email** (ex: "ADMISSÃO PETROPOLIS 10/06"
     → 2026-06-10) está OK. Não peça confirmação. O pipeline tem regra que
     desloca data se hoje, então confie no que extraiu.
  5. Só marque pendente quando você LITERALMENTE NÃO TEM o valor em
     NENHUM documento ou contexto do email.

**Antes de marcar `_pendente=true`, faça este teste mental**:
> "Eu tenho UM valor pra esse campo, mesmo que de fonte única?"
> - Sim → INCLUA NO PAYLOAD, não marque pendente.
> - Não → OK, marque pendente.

Pendência cara: cada false-positive seu custa ~US$ 0.15 de reprocessamento + UX
ruim pro operador. Prefira extrair com 1 fonte do que pedir confirmação.

## GRADAÇÃO DE LEITURA — USE [ilegível] E EXPONHA INCERTEZA

NÃO escolha o palpite "mais provável" quando estiver inseguro. ALUCINAR
1 dígito errado em CPF/CNPJ/RG é PIOR que admitir incerteza — o operador
prefere corrigir manualmente do que descobrir um cadastro errado depois.

Use a seguinte gradação ao extrair campos sensíveis (CPF, CNPJ, RG,
CTPS, PIS, números de certidão, datas):

1. **CONSEGUE LER COM CERTEZA** → extrai normalmente o valor
2. **LÊ A MAIOR PARTE, 1-2 caracteres ambíguos** → use a notação
   `[ilegível]` no(s) caractere(s) duvidoso(s). Exemplos:
     - CPF parcialmente borrado: `"cpf_lido": "857887[?]4543"` em
       `_dados_parciais` (NÃO no payload final — payload exige int válido)
     - Data com mês ambíguo: `"dataidentidade_lida": "15/[?]/2010"`
   Quando houver `[ilegível]` ou `[?]`, MARQUE `_pendente=true` com
   `_motivo` descrevendo qual campo precisa confirmação.
3. **NÃO CONSEGUE LER NADA** → omita o campo do payload (NÃO invente)

### Pares de dígitos clássicos pra confusão em scan (PRESTE ATENÇÃO EXTRA)

Em scans de documentos brasileiros (CTPS, RG, conta de luz, título de eleitor,
PIS, certidão), esses pares são RESPONSÁVEIS PELA MAIORIA DOS ERROS de leitura.
Quando o dígito não está 100% nítido, NUNCA chute — marque `[ilegível]`:

  - `0` ↔ `6` ↔ `8` ↔ `9` (loops similares)
  - `1` ↔ `7` ↔ `T` (traços verticais)
  - `3` ↔ `5` ↔ `S` ↔ `8`
  - `4` ↔ `9` ↔ `A`
  - `O` (letra) ↔ `0` (zero) — sobretudo em CEPs e códigos
  - Caracteres miúdos (seção/zona de título eleitoral, nº de inscrição,
    quadra/lote em conta de luz) tendem a ser pequenos e borrados —
    redobre cuidado.

Regra prática: se você precisaria de **lupa mental** pra decidir entre 2
dígitos, EXTRAIA SÓ a parte certeza E omita ou marque o resto. O DP prefere
preencher 1 dígito no Desktop do que receber CEP errado que cria endereço
inexistente.

## VERIFICAÇÃO CRUZADA — CONFIRA ENTRE FONTES ANTES DE EXTRAIR

Vários campos críticos aparecem em MAIS DE UM documento do mesmo email:

| Campo | Fontes típicas |
|---|---|
| **CPF** | RG, CTPS, comprovante CPF, Caixa Tem, certidão |
| **Nome** | RG, CTPS, CPF, certidão, ficha de admissão |
| **Nascimento** | RG, CTPS, certidão, Caixa Tem |
| **Mãe / Pai** | RG (FILIAÇÃO), CTPS, certidão, Caixa Tem |
| **PIS** | Caixa Tem, CTPS (campo PIS/PASEP), holerite |
| **CTPS nº / série** | CTPS física, Caixa Tem (campo CTPS/Série) |
| **Endereço** | Comprovante de residência, escola, ficha — preferir o MAIS RECENTE |
| **CEP** | Comprovante de residência (deve ser o ATUAL — não o da escola se ela é antiga) |
| **RG nº** | RG (foto), CTPS (qualificação civil), título eleitor às vezes |
| **Sexo / Naturalidade** | RG, certidão, Caixa Tem, CTPS |

⚠️ **REGRA**: pra cada campo que aparece em 2+ fontes, **CONFIRA SE BATEM**
antes de escolher. Se houver divergência:

  1. **CPF, RG, nome, nascimento, mãe, pai**: precisam ser IDÊNTICOS em todas
     as fontes. Divergência = `_pendente=true` listando as variações lidas.
     Esses campos vêm direto dos cadastros oficiais, não admitem variação.
  2. **Endereço/CEP**: prefira a fonte MAIS RECENTE (conta de luz com data
     atual > escola de 2015 > ficha de 2018). Se só tem a antiga, usa essa
     mas registra `_motivo` no `observacao` que o endereço pode estar antigo.
  3. **Cargo**: regra do ASO já definida em seção própria — prioridade clara.

NÃO escolha campo arbitrariamente do "primeiro doc onde apareceu". Procure
em TODOS os documentos antes de fixar o valor.

### Coerência interna (sanity check antes de devolver)

Antes de entregar a resposta, FAÇA ESSES 4 CHECKS você mesmo:

  - **CEP × cidade**: o CEP pertence à cidade que você extraiu? Ex: CEP que
    começa com 74xxx é Goiânia/Aparecida de Goiânia. Se extraiu CEP 74xxx
    mas cidade "SÃO PAULO" → algum dos dois está errado, marque pendência.
  - **Idade × admissão**: se nascimento dá idade < 14 ou > 75 na data de
    admissão, é provável erro de leitura (data de nascimento errada).
  - **Datas dos documentos**: data de emissão do RG/CTPS/PIS deve ser
    POSTERIOR ao nascimento. Se for antes → erro de leitura.
  - **UF cruzada**: se RG tem UF=GO, naturalidade=GO, mas CEP é de SP,
    flag pra revisar (não bloqueia, mas mencione em `observacao`).

Esses checks PEGAM erros de troca de dígito típicos (0/6, 3/8). Use-os.

## CAMPO `_confianca` POR ADMISSÃO (obrigatório)

Em CADA bloco de `admissoes` (ou no payload single), adicione um campo
`_confianca` (float entre 0.0 e 1.0) representando o quão seguro você
está da extração desse funcionário específico:

  - **0.9 - 1.0**: documentos legíveis, todos os campos críticos extraídos
    com certeza, sem ambiguidade
  - **0.7 - 0.89**: maioria dos campos OK, 1-2 com pequena dúvida (ex:
    uma data parcialmente visível, mas o resto está nítido)
  - **0.5 - 0.69**: documentos com qualidade ruim, vários campos exigiram
    inferência ou estão `[ilegível]`. Recomendado revisar manualmente.
  - **< 0.5**: extração arriscada — marque `_pendente=true` em vez de
    tentar entregar

Formato no JSON (ao lado de `data`, dentro do bloco):
```json
{ "data": { ... }, "_confianca": 0.85, "_confianca_motivo": "RG com
  reflexo na foto, data de emissão ambígua (15/06/2010 ou 18/06/2010)" }
```

## ANTI-ALUCINAÇÃO DE CNPJ (REGRA CRÍTICA)

O `cnpj_empresa` é o CNPJ da EMPRESA CONTRATANTE. NUNCA INVENTE esse valor.

⚠️ FONTES VÁLIDAS pra extrair o CNPJ contratante:
  1. Corpo do email (texto) — ex: "CNPJ XX.XXX.XXX/0001-XX"
  2. Assinatura do email (rodapé do remetente)
  3. ASO (Atestado de Saúde Ocupacional): cabeçalho tem
     "EMPRESA: XX.XXX.XXX/0001-XX - NOME DA EMPRESA"
  4. Ficha de admissão / Proposta: cabeçalho ou rodapé
  5. CTPS: campo "ÚLTIMO EMPREGADOR" não conta — é histórico
  6. Holerite anterior: pode aparecer, mas confira se é o atual empregador

❌ FONTES INVÁLIDAS (NÃO usar como CNPJ do empregador):
  - Conta de luz/água/internet: o CNPJ ali é da DISTRIBUIDORA
    (Equatorial, Saneago, Vivo, etc.), NÃO do empregador
  - Carteira de habilitação (CNH): não tem CNPJ
  - Comprovante de residência em geral: CNPJ ali é do provedor do serviço

REGRA: se você NÃO ENCONTRAR um CNPJ explícito de empregador nos
documentos ou no texto do email, RETORNE _pendente=true com motivo
"CNPJ da empresa contratante não localizado nos documentos". NÃO INVENTE
um CNPJ baseado em "parece que essa empresa..." — é melhor pendente
do que CNPJ errado.

## CARGO (`nomecargo`) — QUANDO HÁ DIVERGÊNCIA NOS DOCUMENTOS

É COMUM o cliente reenviar admissão de um funcionário que JÁ TRABALHOU
ali antes (mudança de cargo na mesma empresa). Você vai receber 2+ cargos
diferentes nos anexos: um na ficha velha, outro na ficha nova, outro no
contrato anterior. **NÃO É AMBIGUIDADE — TEM HIERARQUIA CLARA:**

⚠️ **PRIORIDADE PARA EXTRAIR O CARGO (use a PRIMEIRA fonte que existir):**

  1. **ASO ADMISSIONAL** (Atestado de Saúde Ocupacional com tipo
     "ADMISSIONAL"): cabeçalho do exame tem campo "CARGO" ou "FUNÇÃO".
     **ESSE É O CARGO VERDADEIRO** — o médico examinou o funcionário
     PARA esse cargo específico, na semana do envio.
  2. Ficha de admissão **mais recente** (compare data de criação ou de
     admissão — use a com data mais próxima de hoje).
  3. Contrato de experiência atual (não o antigo).
  4. Demais documentos (CTPS antiga, RG, fichas antigas, etc.) — só
     servem como ÚLTIMO recurso se NENHUM dos itens 1-3 trouxer cargo.

❌ NÃO escolha o primeiro cargo que aparecer na ordem dos arquivos.
❌ NÃO mescle cargos ("AUXILIAR / MOTORISTA"). Escolha UM.
❌ NÃO infira cargo por outros campos (ex: "tem CNH categoria E, deve
   ser motorista" — não vale; só extraia o que está ESCRITO).

REGRA: se os documentos divergem E você NÃO ENCONTRA ASO admissional pra
desempatar, RETORNE `_pendente=true` com motivo:
"Cargos divergentes nos documentos e sem ASO admissional pra confirmar
— DP precisa escolher manualmente. Cargos encontrados: [LISTA]". É melhor
pendente do que cargo errado.

## ENDEREÇO É OBRIGATÓRIO — EXTRAIA DE QUALQUER FONTE DISPONÍVEL

Endereço completo (CEP, rua, bairro, cidade, UF) É OBRIGATÓRIO. Sem ele a
admissão não sobe automaticamente. NÃO se limite à conta de luz — procure
em TODOS os documentos.

FONTES VÁLIDAS DE ENDEREÇO (use a primeira que encontrar, em ordem de
prioridade — quanto mais recente o documento, melhor):

1. Comprovante de residência (luz, água, internet, IPTU, telefone) — em
   nome do FUNCIONÁRIO. Prioridade máxima.
2. Comprovante em nome de cônjuge, pai, mãe, filho do funcionário, OU
   qualquer parente cuja relação esteja documentada (ex: comprovante em
   nome do marido — relação visível pelo nome do funcionário aparecer em
   certidão de casamento ou de filho comum).
3. CERTIDÕES DE NASCIMENTO de filhos do funcionário — costumam ter campo
   "FILIAÇÃO" com texto tipo "residentes e domiciliados à Rua X, ...".
   Use o endereço da certidão MAIS RECENTE (datas ajudam).
4. Comprovante em nome de TERCEIRO sem relação aparente (ex: nome
   totalmente diferente, sem indicação de parentesco) — extraia mesmo
   assim, mas considere que pode ser endereço de hospedagem.
5. RG, CNH ou outro documento oficial que liste endereço.

REGRAS:
- Se encontrar endereço em QUALQUER fonte acima, EXTRAIA e popule todos
  os campos: cep, rua, numero (omitir se sem número), bairro, cidade, uf.
- Se encontrar MÚLTIPLOS endereços diferentes, escolha:
  - O mais recente (data do documento)
  - Em caso de empate, o que aparece em mais documentos
  - Em último caso, o do comprovante de residência (mesmo que de terceiro)
- Marque _pendente=true APENAS quando NÃO houver NENHUMA fonte com
  endereço identificável em TODOS os anexos. Motivo: "Não localizei
  comprovante de residência nem endereço em outros documentos. Pode
  enviar conta de luz/água em nome do funcionário ou de parente?".

- Os IDs de `empresa`, `departamento` e `funcao` serão substituídos pelo
  pipeline depois — use placeholders "1" nesses 3 relationships.
- Se faltarem dados essenciais (lista da seção 10 do briefing) PRA UM
  funcionário específico, retorne:
  {"_pendente": true, "_motivo": "<descrição curta e ESPECÍFICA>",
   "_dados_parciais": {...}}

  ⚠ MÚLTIPLAS ADMISSÕES NO MESMO EMAIL SÃO SUPORTADAS — NÃO marque
  _pendente=true só porque há mais de um funcionário. Use o formato
  `admissoes: [...]` descrito mais abaixo. Cada bloco é processado
  independente: o que estiver completo sobe, o que faltar algum vira
  pendência só daquele funcionário.

  ⚠ IMPORTANTE: SEMPRE popule `_dados_parciais` com TUDO que você conseguiu
  extrair do email/anexos, mesmo quando incompleto. Esse objeto é mostrado
  ao cliente no email de resposta — ele precisa VER o que você identificou
  pra confiar no pipeline. NUNCA retorne _dados_parciais vazio se há algum
  dado identificável nos documentos.

  Use chaves PT-BR simples no _dados_parciais (ex: nome, cpf, nascimento,
  nomedamae, cargo, salario, cnpj_empresa).

- IMPORTANTE: extraia também o campo `cnpj_empresa` (raiz) com o CNPJ
  da empresa contratante, e `departamento_sugerido` (string livre, opcional)
  pro pipeline resolver depois. Ambos vão FORA do `data` — no nível raiz
  do JSON retornado, ao lado de `data`.

## MÚLTIPLAS ADMISSÕES NO MESMO EMAIL — SEMPRE SUPORTADO

⚠ É TOTALMENTE NORMAL e ESPERADO o cliente mandar 2, 3 ou mais funcionários
no mesmo email. JAMAIS marque _pendente=true só por causa disso. JAMAIS peça
ao cliente pra "enviar separadamente" — isso é fluxo válido e suportado.

Se você identificar mais de um funcionário no mesmo email (ex: "segue
documentos de Silvani e Lourrana..." com docs de ambas), gere UM PAYLOAD
PARA CADA usando o formato `admissoes`:

```json
{
  "cnpj_empresa": "12345678000190",
  "admissoes": [
    {
      "departamento_sugerido": "COZINHA",
      "data": {"type": "candidatos", "attributes": {...}, "relationships": {...}}
    },
    {
      "departamento_sugerido": "COZINHA",
      "data": {"type": "candidatos", "attributes": {...}, "relationships": {...}}
    }
  ]
}
```

Use o formato `admissoes` SEMPRE que houver 2+ pessoas, mesmo se uma
delas tiver dados incompletos — você pode incluir o que conseguiu de cada
uma. O pipeline processa CADA admissão independentemente: a que tiver
todos os dados sobe, a que faltar algum vai pra pendência (e o cliente
recebe só pedindo o que falta daquela específica).

Pra UMA admissão só, retorne o formato simples (data no root):
```json
{
  "cnpj_empresa": "12345678000190",
  "departamento_sugerido": "ADMINISTRATIVO",
  "data": {
    "type": "candidatos",
    "attributes": {...},
    "relationships": {...}
  }
}
```

---

## QUANDO UM ÚNICO PDF CONTÉM PESSOAS DIFERENTES EM PÁGINAS DIFERENTES

⚠️ **REGRA CRÍTICA — NÃO ASSUMA "1 PDF = 1 PESSOA".**

É comum o cliente **digitalizar tudo em um único PDF** com docs de várias
pessoas misturados (ex: ASO da Alexandra na pág 1, RG+ficha+endereço da
Mariana nas págs 2-5). O briefing antes não tratava esse caso e isso causou
**falsas pendências** ("Email menciona X mas nenhum documento dela foi
localizado") quando a pessoa estava nas páginas que você não associou.

### ANTES DE EXTRAIR QUALQUER DADO, FAÇA ESTE CHECKLIST DE 4 PASSOS:

**Passo 1 — INVENTÁRIO DE NOMES.** Leia o email + corpo + assunto e liste
TODOS os nomes próprios de funcionários mencionados (ex: "Mariana Alves Costa"
e "Alexandra Goncalves Coelho Godinho"). Esse é seu **alvo**: ao final você
deve ter UM bloco em `admissoes[]` para CADA nome — mesmo que parcial.

**Passo 2 — VARRA TODAS AS PÁGINAS DE TODOS OS PDFs.** Pra cada página, anote:
- Qual NOME aparece no topo/cabeçalho/campo "Nome do funcionário"
- Qual CPF aparece
- Qual TIPO de documento é (RG, ASO, ficha, comprovante, certidão, CTPS)

Se você encontrar **2+ NOMES DIFERENTES** no mesmo PDF (em páginas distintas),
isso significa que o PDF é COMPARTILHADO — não é erro do cliente, é o jeito
dele organizar. Trate cada nome como pessoa SEPARADA.

**Passo 3 — AGRUPE PÁGINAS POR PESSOA.** Mudança de CPF entre páginas =
pessoa nova. Mudança de nome entre páginas = pessoa nova. Anote em qual
página cada pessoa aparece.

**Passo 4 — CRIE 1 BLOCO EM `admissoes[]` POR PESSOA.** Mesmo que a pessoa
tenha **só 1 documento** (ex: só o ASO):
- Inclua o que conseguiu extrair dela (nome, CPF, cargo, datas do ASO)
- Marque `_pendente: true` com `_motivo` ESPECÍFICO listando o que falta
  *para essa pessoa* (ex: "Só ASO localizado pra Alexandra — faltam RG,
  endereço, salário")
- **NUNCA descarte** uma pessoa mencionada no email porque a documentação
  dela é parcial

### EXEMPLO REAL (caso BASIC STORE 12/06/2026)

Email diz: *"Segue MARIANA ALVES COSTA e ALEXANDRA GONCALVES COELHO GODINHO"*
PDF único HEBROM.pdf:
- Página 1: ASO da Alexandra (cargo VENDEDORA, CPF 094.054.506-38)
- Páginas 2-5: ficha + RG + endereço da Mariana

❌ **ERRADO** (o que estava acontecendo antes):
```json
{
  "admissoes": [
    {"data": {"attributes": {"nome": "MARIANA ALVES COSTA", ...}}},
    // ALEXANDRA sumiu — pendência foi "nenhum doc localizado"
  ]
}
```

✅ **CERTO**:
```json
{
  "admissoes": [
    {"data": {"attributes": {"nome": "MARIANA ALVES COSTA", "cpf": ..., ...}}},
    {
      "data": {"attributes": {
        "nome": "ALEXANDRA GONCALVES COELHO GODINHO",
        "cpf": 9405450638,
        "nomecargo": "VENDEDORA",
        "dataatestadoocupacional": "2026-05-04",
        "sexo": ...,
        "nascimento": "1986-08-09"
      }},
      "_pendente": true,
      "_motivo": "Só ASO localizado pra ALEXANDRA — faltam RG/endereço/salário/nome da mãe"
    }
  ]
}
```

A diferença: pessoa **NUNCA sumiu silenciosamente**. Pipeline cria a
pendência ESPECÍFICA do que falta dela, cliente recebe pedido pontual
("manda RG e endereço da Alexandra"), não fica achando que mandou tudo certo.


Só use `_pendente: true` quando NÃO conseguiu identificar dados úteis de
NENHUMA admissão (ex: anexo só de comprovante, email sem corpo nem docs).

---

## 🔴 FORMATO DA RESPOSTA — LEIA POR ÚLTIMO

Sua resposta DEVE SER **APENAS JSON puro** dentro de um único bloco
```json ... ```. NADA mais.

❌ ERRADO (texto narrativo antes):
    Vou analisar os documentos.
    **Extração:** ...
    ```json
    {"cnpj_empresa": "..."}
    ```

❌ ERRADO (markdown explicativo entre seções):
    ```json
    {...
    ```
    Note que isso é porque...

✅ CORRETO (apenas o bloco JSON, nada mais):
    ```json
    {
      "cnpj_empresa": "09491921000179",
      "admissoes": [...]
    }
    ```

NÃO use cabeçalhos `**Extração**`. NÃO descreva seu raciocínio. NÃO comente
sobre o CNPJ pré-resolvido. NÃO confirme que vai analisar. Apenas RESPONDA
COM JSON. Texto livre quebra o parser do pipeline e gera falha técnica.
"""


def carregar_briefing() -> str:
    if not BRIEFING_FILE.exists():
        raise FileNotFoundError(
            f"briefing.md não encontrado em {BRIEFING_FILE} — "
            "copie do Obsidian (DP/Automação Admissão API/12 - Briefing...)"
        )
    return BRIEFING_FILE.read_text(encoding="utf-8") + SYSTEM_SUFIXO


class ClaudeClient:
    # Intervalo mínimo entre chamadas consecutivas ao Claude (segundos).
    # Throttle simples pra evitar rate-limit em pipelines com vários emails
    # processados em sequência (cada email pode disparar 1-2 calls).
    INTERVALO_MIN_ENTRE_CHAMADAS = 3.0

    # Campos críticos pra detectar divergência entre chamadas de verificação.
    # Se 2 chamadas extraem valores DIFERENTES nesses campos (ex: CPFs diferentes),
    # é red flag — Claude está alucinando em um dos casos.
    CAMPOS_CRITICOS_VERIFICACAO = [
        "cpf", "nome", "nascimento", "identidade",
        "admissao", "dataidentidade", "ctps",
    ]

    def __init__(
        self,
        model: str = "claude-sonnet-4-20250514",
        max_tokens: int = 8192,
        chamadas_verificacao: int = 1,
    ):
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY não encontrada no ambiente")
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.max_tokens = max_tokens
        self.system_prompt = carregar_briefing()
        self._ts_ultima_chamada: float = 0.0
        # Self-consistency: chama N vezes e funde. 1 = sem verificação,
        # 2 = double-check (recomendado), 3+ = ensemble.
        self.chamadas_verificacao = max(1, int(chamadas_verificacao))
        # Contadores cumulativos de billing (toda chamada feita pela instância)
        self.usage_total: dict[str, int] = {
            "n_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }

    def _bloco_anexo(self, anexo: dict) -> dict | None:
        """Retorna o content-block apropriado pro Claude (image_/document_).

        Normaliza MIMEs obscuros (octet-stream, vazio, x-pdf) pra o canônico
        que a Anthropic API aceita — senão a chamada falha com 400.
        Ver `_normalizar_mime_anthropic` pra detalhes."""
        raw_mime = anexo.get("mime", "")
        filename = anexo.get("filename", "")
        data = anexo.get("data")
        if not data:
            return None

        mime = _normalizar_mime_anthropic(filename, raw_mime)
        if not mime:
            log.warning(
                f"Anexo com mime não suportado ignorado: {filename} "
                f"({raw_mime or '(vazio)'}). Extensões aceitas: "
                f"{', '.join(EXT_TO_MIME_CANONICO.keys())}."
            )
            return None
        if mime != raw_mime:
            log.info(
                f"   🔧 MIME normalizado pro Claude: '{filename}' "
                f"{raw_mime or '(vazio)'} → {mime}"
            )

        b64 = base64.standard_b64encode(data).decode("ascii")
        if mime in IMAGE_MIMES:
            return {
                "type": "image",
                "source": {"type": "base64", "media_type": mime, "data": b64},
            }
        if mime in DOC_MIMES:
            return {
                "type": "document",
                "source": {"type": "base64", "media_type": mime, "data": b64},
            }
        return None

    def gerar_payload(
        self,
        corpo_email: str,
        metadados: dict,
        anexos: list[dict],
        funcoes_candidatas: list[dict] | None = None,
    ) -> dict:
        """Wrapper público: faz 1 chamada ao Claude. Se a resposta tiver
        indícios de inconsistência (campos chave faltando, _pendente=true),
        dispara chamadas extra de verificação até `chamadas_verificacao`.
        Funde respostas pegando a mais completa.

        Otimização vs. self-consistency cego (sempre 2 chamadas): quando a
        1ª resposta já tá completa (caso comum), NÃO paga a 2ª. Custo médio
        cai bastante mantendo a robustez nos casos suspeitos.

        Fallback 413: se a chamada estourar mesmo apos compressao automatica
        (feita dentro de _gerar_payload_unico), divide os anexos por grupo
        (1 grupo = 1 .rar/.zip = 1 funcionario, ou avulsos) e faz N chamadas
        separadas, mesclando os blocos `admissoes` no final.

        Pre-compressao proativa: se o tamanho raw dos anexos ja excede o
        limite seguro (~24MB), comprime ANTES da 1ª chamada — economiza ~15s
        de latencia (do 413 round-trip) e garante que chamadas de verificacao
        reusem os anexos ja comprimidos.
        """
        tam_mb = _tamanho_total_mb(anexos)
        if tam_mb > LIMITE_RAW_MB_SEGURO and _HAS_PYMUPDF:
            log.info(
                f"   📦 Anexos pesam {tam_mb:.1f}MB (limite {LIMITE_RAW_MB_SEGURO}MB) "
                "— pre-comprimindo antes da 1ª chamada"
            )
            anexos = _comprimir_anexos(anexos)
            log.info(f"   Tamanho apos pre-compressao: {_tamanho_total_mb(anexos):.1f}MB")

        try:
            primeira = self._gerar_payload_unico(
                corpo_email, metadados, anexos, funcoes_candidatas
            )
        except anthropic.APIStatusError as e:
            if getattr(e, "status_code", None) != 413:
                raise
            return self._gerar_payload_dividido(
                corpo_email, metadados, anexos, funcoes_candidatas
            )

        if self.chamadas_verificacao <= 1 or not self._precisa_verificacao(primeira):
            return primeira

        log.info(
            f"   🔁 Inconsistência na 1ª resposta — disparando até "
            f"{self.chamadas_verificacao - 1} chamada(s) de verificação"
        )
        respostas: list[dict] = [primeira]
        for i in range(1, self.chamadas_verificacao):
            try:
                r = self._gerar_payload_unico(
                    corpo_email, metadados, anexos, funcoes_candidatas
                )
                respostas.append(r)
                # Se a nova resposta JÁ parece consistente, pode parar antes
                if not self._precisa_verificacao(r):
                    log.info(f"   ✅ Verificação {i+1} retornou resposta consistente — parando")
                    break
            except Exception:
                log.exception(f"   Chamada de verificação {i+1} falhou")

        if len(respostas) == 1:
            log.warning("   Verificação falhou — usando 1ª resposta sem comparação")
            return respostas[0]

        return self._mesclar_respostas(respostas)

    def _gerar_payload_dividido(
        self,
        corpo_email: str,
        metadados: dict,
        anexos: list[dict],
        funcoes_candidatas: list[dict] | None,
    ) -> dict:
        """Fallback quando _gerar_payload_unico falhar com 413 mesmo apos
        comprimir os PDFs. Divide os anexos por grupo (1 grupo = 1 funcionario)
        e faz N chamadas Claude separadas. Mescla os blocos `admissoes` no fim.

        Custo: ~N× mais caro que uma chamada unica (cache do system prompt
        atenua bastante — chamadas 2..N reusam cache da 1ª).
        """
        grupos = _agrupar_anexos(anexos)
        log.warning(
            f"   📦 Compressao nao foi suficiente — dividindo em {len(grupos)} "
            f"chamada(s) Claude (uma por funcionario)"
        )

        respostas_por_grupo: list[dict] = []
        for nome_grupo, anexos_grupo in grupos.items():
            tam_grupo = _tamanho_total_mb(anexos_grupo)
            log.info(
                f"   ➜ Grupo '{nome_grupo}': {len(anexos_grupo)} anexos, "
                f"{tam_grupo:.1f}MB"
            )
            try:
                r = self._gerar_payload_unico(
                    corpo_email, metadados, anexos_grupo, funcoes_candidatas
                )
                respostas_por_grupo.append(r)
            except anthropic.APIStatusError as e:
                if getattr(e, "status_code", None) == 413:
                    log.error(
                        f"   ❌ Grupo '{nome_grupo}' ainda estoura 413 mesmo isolado — "
                        "anexos individuais grandes demais. Pulando."
                    )
                    continue
                log.exception(f"   Falha processando grupo '{nome_grupo}'")
                continue
            except Exception:
                log.exception(f"   Falha processando grupo '{nome_grupo}'")
                continue

        if not respostas_por_grupo:
            return {"_pendente": True, "_motivo": "Todos os grupos falharam no fallback 413"}

        # Mescla: pega o root da primeira (cnpj_empresa, departamento_sugerido)
        # e concatena os blocos `admissoes` de todas.
        merged = dict(respostas_por_grupo[0])
        admissoes_total: list[dict] = []
        for r in respostas_por_grupo:
            admissoes = r.get("admissoes")
            if isinstance(admissoes, list):
                admissoes_total.extend(admissoes)
            elif "data" in r:  # single legacy → trata como 1 admissao
                admissoes_total.append({"data": r["data"]})
        if admissoes_total:
            merged["admissoes"] = admissoes_total
            merged.pop("data", None)  # forca o caller a usar `admissoes`
        log.info(
            f"   ✅ Fallback 413 concluido: {len(admissoes_total)} admissao(oes) "
            f"de {len(respostas_por_grupo)} grupo(s)"
        )
        return merged

    @classmethod
    def _precisa_verificacao(cls, resp: dict) -> bool:
        """Heurística: a resposta parece suspeita o suficiente pra justificar
        uma 2ª chamada de verificação?

        Triggers:
          - _pendente=true (Claude desistiu — vamos tentar de novo)
          - Sem blocos de admissão (resposta vazia/malformada)
          - Algum bloco com 3+ campos chave faltando (nome/cpf/nascimento/
            identidade/admissao/salario) — Claude pode ter perdido coisas
            que estavam visíveis
        """
        if resp.get("_pendente"):
            log.info("   📍 _pendente=true → vamos verificar")
            return True

        blocos = resp.get("admissoes") or ([resp] if "data" in resp else [])
        if not blocos:
            log.info("   📍 resposta sem blocos → vamos verificar")
            return True

        CAMPOS_CHAVE = ["nome", "cpf", "nascimento", "identidade", "admissao", "salario"]
        for i, b in enumerate(blocos, 1):
            attrs = (b.get("data") or {}).get("attributes") or {}
            ausentes = [k for k in CAMPOS_CHAVE if not attrs.get(k)]
            if len(ausentes) >= 3:
                log.info(
                    f"   📍 bloco {i}/{len(blocos)} com {len(ausentes)}/6 campos chave "
                    f"faltando ({', '.join(ausentes)}) → vamos verificar"
                )
                return True
        return False

    def _gerar_payload_unico(
        self,
        corpo_email: str,
        metadados: dict,
        anexos: list[dict],
        funcoes_candidatas: list[dict] | None = None,
    ) -> dict:
        """Uma única chamada ao Claude. Retorna o dict parseado do JSON.

        funcoes_candidatas: lista de {nome, cbo, funcao_id} pra desambiguar
        cargo quando a planilha CBO tem múltiplos matches.
        """
        # v2.16.0: contexto do remetente — se já há histórico desse cliente,
        # injeta no prompt pra Claude ter base de comparação e entender padrões.
        # perfis_remetente.resumo_pra_prompt retorna "" quando é remetente novo.
        try:
            import perfis_remetente as _pr
            rem = metadados.get("remetente", "")
            contexto_rem = _pr.resumo_pra_prompt(rem) if rem else ""
        except Exception as _e:
            contexto_rem = ""
            log.warning(f"   ⚠ perfis_remetente.resumo_pra_prompt falhou: {_e}")

        # Texto inicial pro Claude
        intro_partes = [
            "# EMAIL RECEBIDO",
            f"De: {metadados.get('remetente', '?')}",
            f"Assunto: {metadados.get('assunto', '?')}",
            f"Data: {metadados.get('data', '?')}",
            "",
            "## CORPO DO EMAIL (texto)",
            corpo_email or "(vazio)",
            "",
            "## ANEXOS",
            f"Recebendo {len(anexos)} anexo(s): " + ", ".join(
                f"{a['filename']} ({a['mime']})" for a in anexos
            ) or "(nenhum)",
        ]

        if contexto_rem:
            intro_partes = [contexto_rem, "---", ""] + intro_partes

        if funcoes_candidatas:
            intro_partes += [
                "",
                "## DESAMBIGUAÇÃO DE CARGO",
                "O pipeline encontrou múltiplas funções parecidas no cadastro "
                "Crosara pro cargo que você extraiu. Sua tarefa nesse turno é "
                "ESCOLHER UMA da lista abaixo:",
                "",
                "1. Olhe a função, CBO e setor implícito de cada linha.",
                "2. Copie o `nome_cargo` EXATO (UPPERCASE) pro campo `nomecargo` "
                "do payload — o pipeline localiza a função pelo nome.",
                "3. Se houver entradas IGUAIS (mesmo nome + CBO), pode escolher "
                "qualquer uma — são duplicatas do cadastro do eContador.",
                "",
                "| funcao_id | nome_cargo | cbo |",
                "|---|---|---|",
            ]
            for f in funcoes_candidatas[:50]:
                nome = f.get("nome_cargo") or f.get("nome") or "?"
                intro_partes.append(
                    f"| {f.get('funcao_id', '?')} | {nome} | {f.get('cbo', '?')} |"
                )

        intro_partes += [
            "",
            "## INSTRUÇÃO",
            "Extraia os dados do funcionário, aplique todas as regras do briefing ",
            "(defaults, UPPERCASE, datas ISO, etc.) e retorne o JSON final no formato ",
            "especificado nas INSTRUÇÕES DE SAÍDA.",
        ]
        intro_texto = "\n".join(intro_partes)

        # Monta a content list — IMAGENS ANTES DO TEXTO.
        # Anthropic documenta que pra tarefas de extração visual, anexos antes
        # da instrução melhoram a fidelidade: o modelo "vê" todos os documentos
        # antes de decidir como aplicar as regras de extração.
        content: list[dict] = []
        for anexo in anexos:
            bloco = self._bloco_anexo(anexo)
            if bloco:
                content.append(bloco)
        content.append({"type": "text", "text": intro_texto})

        # Throttle: garante intervalo mínimo entre chamadas consecutivas
        delta = time.time() - self._ts_ultima_chamada
        if 0 < delta < self.INTERVALO_MIN_ENTRE_CHAMADAS:
            espera = self.INTERVALO_MIN_ENTRE_CHAMADAS - delta
            log.info(f"⏳ Aguardando {espera:.1f}s antes da próxima chamada ao Claude")
            time.sleep(espera)

        log.info(f"Enviando {len(content)} blocos pro Claude ({self.model})")

        # System prompt com cache_control: ~6KB de briefing estável fica
        # em cache da Anthropic por ~5min. Chamadas dentro desse intervalo
        # pagam 0.10× pelo input tokens cacheados (write é 1.25×, read 0.10×).
        # Em pipelines com várias admissões seguidas, economia de 50-70%.
        # Retry exponencial em 429 (v2.11.4): até 3 tentativas honrando
        # `retry-after`. Caso real (JESSYKA/RAYSSA 11/06/2026): rate limit
        # batia e admissão virava pendência cliente confusa em vez de
        # esperar e retentar.
        MAX_RETRIES_429 = 3

        def _create_msg(_content: list[dict]):
            """Chama messages.create com retry exponencial em 429.
            Repropaga 413 (caller trata via fallback de compressão)."""
            for tentativa in range(MAX_RETRIES_429 + 1):
                try:
                    return self.client.messages.create(
                        model=self.model,
                        max_tokens=self.max_tokens,
                        system=[{
                            "type": "text",
                            "text": self.system_prompt,
                            "cache_control": {"type": "ephemeral"},
                        }],
                        messages=[{"role": "user", "content": _content}],
                    )
                except anthropic.APIStatusError as e:
                    if _eh_429(e):
                        sc = getattr(e, "status_code", None)
                        rotulo = (
                            "Overload Anthropic (529)" if sc == 529
                            else "Rate limit Anthropic (429)" if sc == 429
                            else "Capacidade Anthropic (429/529)"
                        )
                        if tentativa < MAX_RETRIES_429:
                            wait_s = _esperar_retry_after(e, tentativa)
                            log.warning(
                                f"   ⏳ {rotulo} tentativa "
                                f"{tentativa + 1}/{MAX_RETRIES_429} — aguardando "
                                f"{wait_s:.0f}s antes de retentar..."
                            )
                            time.sleep(wait_s)
                            continue
                        if sc == 529:
                            log.error(
                                f"   ❌ Overload Anthropic persistente após "
                                f"{MAX_RETRIES_429} retentativas. API deles "
                                f"está sobrecarregada — não é seu rate limit. "
                                f"Cheque https://status.anthropic.com e tente "
                                f"de novo em alguns minutos."
                            )
                        else:
                            log.error(
                                f"   ❌ Rate limit persistente após {MAX_RETRIES_429} "
                                f"retentativas. Anthropic API quota da org esgotada — "
                                f"reduza polling, espere alguns minutos ou aumente quota."
                            )
                    raise

        try:
            msg = _create_msg(content)
        except ValueError as e:
            if "Streaming is required" in str(e):
                log.error(
                    f"   ❌ SDK bloqueou chamada — max_tokens={self.max_tokens} "
                    f"alto demais (estimou >10min). Reduza 'anthropic.max_tokens' "
                    f"em config.json (sugestão: 16384 ou menos)."
                )
            raise
        except anthropic.APIStatusError as e:
            if getattr(e, "status_code", None) != 413:
                raise
            # Fallback 413: tenta comprimir os PDFs e re-mandar 1x.
            # Split por funcionario fica a cargo do caller (gerar_payload).
            if not _HAS_PYMUPDF:
                log.error("   ❌ 413 e PyMuPDF nao instalado — sem fallback de compressao")
                raise
            tam_antes = _tamanho_total_mb(anexos)
            log.warning(
                f"   ⚠ 413 Payload Too Large ({tam_antes:.1f}MB raw). "
                f"Comprimindo PDFs em {DPI_COMPRESSAO_FALLBACK} DPI..."
            )
            anexos_comp = _comprimir_anexos(anexos)
            tam_depois = _tamanho_total_mb(anexos_comp)
            log.info(f"   Tamanho apos compressao: {tam_depois:.1f}MB")

            # Reconstroi o content com os anexos comprimidos — mesma ordem
            # (imagens antes do texto, ver comentário acima)
            content_novo: list[dict] = []
            for anexo in anexos_comp:
                bloco = self._bloco_anexo(anexo)
                if bloco:
                    content_novo.append(bloco)
            content_novo.append({"type": "text", "text": intro_texto})
            log.info(f"   Re-tentando com {len(content_novo)} blocos comprimidos...")
            msg = _create_msg(content_novo)
        self._ts_ultima_chamada = time.time()

        # Captura tokens + estima custo desta chamada
        self._registrar_uso(getattr(msg, "usage", None))

        # Detecta truncamento por MAX_TOKENS — quando isso acontece, o JSON
        # vem cortado e o parser falha. Causa custo desperdiçado + chamada
        # de verificação que também trunca. Aviso claro pro operador subir
        # `max_tokens` em config.json.
        stop_reason = getattr(msg, "stop_reason", None)
        if stop_reason == "max_tokens":
            usage = getattr(msg, "usage", None)
            out_tokens = getattr(usage, "output_tokens", 0) if usage else 0
            log.error(
                f"   ⚠ RESPOSTA TRUNCADA pelo limite de tokens "
                f"({out_tokens} output tokens — bateu max_tokens={self.max_tokens}). "
                f"JSON vai falhar parse. Aumente 'anthropic.max_tokens' em "
                f"config.json (atual: {self.max_tokens}, sugestão: dobrar)."
            )

        # Concatena texto de resposta
        resposta = "\n".join(
            b.text for b in msg.content if getattr(b, "type", None) == "text"
        )
        log.debug(f"Resposta Claude (preview): {resposta[:300]}")

        return self._parsear_json(resposta)

    def _registrar_uso(self, usage) -> None:
        """Acumula tokens da chamada atual e loga estimativa de custo."""
        if usage is None:
            return
        inp = getattr(usage, "input_tokens", 0) or 0
        out = getattr(usage, "output_tokens", 0) or 0
        cache_w = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cache_r = getattr(usage, "cache_read_input_tokens", 0) or 0

        self.usage_total["n_calls"] += 1
        self.usage_total["input_tokens"] += inp
        self.usage_total["output_tokens"] += out
        self.usage_total["cache_creation_input_tokens"] += cache_w
        self.usage_total["cache_read_input_tokens"] += cache_r

        custo = self.estimar_custo_usd(inp, out, cache_w, cache_r)
        # Log conciso, separa cache pra dar visibilidade quando estiver ativo
        extra = ""
        if cache_w or cache_r:
            extra = f" [cache: +{cache_w:,} write / +{cache_r:,} read]"
        log.info(
            f"   💰 {inp:,} input + {out:,} output tokens{extra} "
            f"≈ US$ {custo:.4f}"
        )

    def estimar_custo_usd(
        self,
        input_tokens: int,
        output_tokens: int,
        cache_creation: int = 0,
        cache_read: int = 0,
    ) -> float:
        """Estima custo em USD desta chamada (ou agregado, se chamado com totais).

        Pricing oficial Anthropic:
          - input regular: 1x base
          - cache write: 1.25x base
          - cache read: 0.1x base
          - output: out_price
        """
        in_price, out_price = _precos_do_modelo(self.model)
        in_regular = (input_tokens / 1_000_000) * in_price
        in_cache_w = (cache_creation / 1_000_000) * in_price * 1.25
        in_cache_r = (cache_read / 1_000_000) * in_price * 0.10
        out_total = (output_tokens / 1_000_000) * out_price
        return in_regular + in_cache_w + in_cache_r + out_total

    def usage_resumo(self) -> dict:
        """Retorna dict com cumulativo + estimativa de custo USD."""
        u = self.usage_total
        custo = self.estimar_custo_usd(
            u["input_tokens"], u["output_tokens"],
            u["cache_creation_input_tokens"], u["cache_read_input_tokens"],
        )
        return {
            **u,
            "model": self.model,
            "custo_usd_estimado": round(custo, 4),
        }

    # ---- Escolha auxiliar de departamento por cargo ------------------

    def escolher_departamento_por_cargo(
        self,
        cargo: str,
        deptos: list[dict],
        cbo: str | None = None,
    ) -> tuple[str | None, str]:
        """Pergunta ao Claude qual departamento melhor encaixa pra um cargo,
        dada a lista de departamentos disponíveis na empresa.

        Usado como fallback quando o cliente não mencionou o departamento
        no email e a empresa tem múltiplos cadastrados.

        Chamada FOCADA e BARATA:
          - Sem system prompt (briefing pesado não é necessário aqui)
          - max_tokens=300 (resposta curta)
          - Só texto, sem anexos

        Retorna (departamento_id ou None, motivo).
        None significa: Claude não conseguiu escolher com confiança
        (cargo não se encaixa em nenhum dos deptos disponíveis).
        """
        if not deptos:
            return None, "Lista de departamentos vazia"

        # Throttle
        delta = time.time() - self._ts_ultima_chamada
        if 0 < delta < self.INTERVALO_MIN_ENTRE_CHAMADAS:
            time.sleep(self.INTERVALO_MIN_ENTRE_CHAMADAS - delta)

        deptos_str = "\n".join(
            f"  - id={d['id']}, nome={d.get('nome', '?')}" for d in deptos
        )
        cbo_str = f" (CBO {cbo})" if cbo else ""

        prompt = (
            f"Um novo funcionário foi contratado com o cargo "
            f"'{cargo}'{cbo_str}. Em qual departamento da empresa ele/ela "
            f"vai trabalhar?\n\n"
            f"DEPARTAMENTOS DISPONÍVEIS:\n{deptos_str}\n\n"
            f"Escolha o que melhor encaixa pro cargo e retorne APENAS um "
            f"JSON dentro de bloco ```json``` com:\n"
            f"```json\n"
            f"{{\n"
            f"  \"departamento_id\": \"<id escolhido como string>\",\n"
            f"  \"motivo\": \"<frase curta justificando a escolha>\"\n"
            f"}}\n"
            f"```\n\n"
            f"Se NENHUM departamento da lista se encaixa claramente pra "
            f"esse cargo (ex: cargo administrativo numa empresa só com "
            f"departamentos operacionais), retorne `departamento_id: null` "
            f"e explique no motivo."
        )

        log.info(
            f"   🤖 Pedindo Claude pra escolher depto entre {len(deptos)} "
            f"opções (cargo: '{cargo}')"
        )
        msg = self.client.messages.create(
            model=self.model,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        self._ts_ultima_chamada = time.time()
        self._registrar_uso(getattr(msg, "usage", None))

        resposta = "\n".join(
            b.text for b in msg.content if getattr(b, "type", None) == "text"
        )

        try:
            parsed = self._parsear_json(resposta)
        except ValueError as e:
            log.warning(f"   Claude resposta não parseable: {e}")
            return None, "Resposta do Claude não parseable"

        dep_id = parsed.get("departamento_id")
        motivo = (parsed.get("motivo") or "").strip()

        if dep_id in (None, "null", "", 0):
            return None, motivo or "Claude não identificou departamento adequado"

        # Validação anti-alucinação: ID precisa estar na lista que enviamos
        ids_validos = {str(d["id"]) for d in deptos}
        if str(dep_id) not in ids_validos:
            log.warning(
                f"   Claude retornou departamento_id={dep_id!r} fora da lista "
                f"— descartando (anti-alucinação)"
            )
            return None, f"Claude inventou um ID ({dep_id}) que não está na lista"

        return str(dep_id), motivo

    # ---- Self-consistency (multi-chamada) ----------------------------

    @staticmethod
    def _cpf_dv_valido(cpf: str) -> bool:
        """v2.16.38: valida CPF pelos 2 dígitos verificadores. Usado como
        tiebreaker quando 2 chamadas do Claude divergem no CPF (caso JOSÉ
        DIVINO 24/06/2026 — Claude leu '015.273.671.93' como tanto
        '01527367193' [válido] quanto '15273671193' [inválido])."""
        import re as _re
        d = _re.sub(r"\D", "", str(cpf or ""))
        if len(d) != 11 or d == d[0] * 11:
            return False
        # DV1
        soma = sum(int(d[i]) * (10 - i) for i in range(9))
        dv1 = (soma * 10) % 11
        if dv1 == 10:
            dv1 = 0
        if dv1 != int(d[9]):
            return False
        # DV2
        soma = sum(int(d[i]) * (11 - i) for i in range(10))
        dv2 = (soma * 10) % 11
        if dv2 == 10:
            dv2 = 0
        return dv2 == int(d[10])

    @staticmethod
    def _cpf_da_resposta(resp: dict) -> str:
        """Extrai o primeiro CPF não-vazio de uma resposta (single ou multi)."""
        if not isinstance(resp, dict):
            return ""
        if resp.get("_pendente"):
            return str((resp.get("_dados_parciais") or {}).get("cpf") or "")
        blocos = resp.get("admissoes") or []
        if blocos:
            for b in blocos:
                if isinstance(b, dict):
                    v = ((b.get("data") or {}).get("attributes") or {}).get("cpf")
                    if v:
                        return str(v)
        elif "data" in resp:
            v = ((resp.get("data") or {}).get("attributes") or {}).get("cpf")
            if v:
                return str(v)
        return ""

    @staticmethod
    def _contar_preenchidos(resp: dict) -> int:
        """Conta valores não-vazios em campos relevantes da resposta.
        Usado pra ranquear respostas: mais campos preenchidos → mais completa.
        """
        if not isinstance(resp, dict):
            return 0
        n = 0
        # Caminho _pendente: conta o que foi pra _dados_parciais
        if resp.get("_pendente"):
            for v in (resp.get("_dados_parciais") or {}).values():
                if v not in (None, "", 0, [], {}):
                    n += 1
            return n
        # cnpj_empresa raiz vale 1
        if resp.get("cnpj_empresa"):
            n += 1
        # Multi-admissão
        blocos = resp.get("admissoes")
        if isinstance(blocos, list) and blocos:
            for b in blocos:
                if isinstance(b, dict):
                    n += ClaudeClient._contar_campos_bloco(b)
            return n
        # Single legacy
        if "data" in resp:
            n += ClaudeClient._contar_campos_bloco(resp)
        return n

    @staticmethod
    def _contar_campos_bloco(bloco: dict) -> int:
        """Conta attributes não-vazios + relationships com .data.id."""
        n = 0
        data = bloco.get("data") or {}
        attrs = data.get("attributes") or {}
        for v in attrs.values():
            if v not in (None, "", 0, [], {}):
                n += 1
        rels = data.get("relationships") or {}
        for v in rels.values():
            if isinstance(v, dict) and (v.get("data") or {}).get("id"):
                n += 1
        return n

    @classmethod
    def _mesclar_respostas(cls, respostas: list[dict]) -> dict:
        """Funde N respostas em uma. Estratégia:

        1. Se há respostas NÃO-pendentes, descarta as `_pendente: true`
           (Claude que conseguiu processar é melhor que o que desistiu).
        2. Entre as candidatas, pega a com MAIS campos preenchidos.
        3. Loga divergências em campos críticos pra auditoria/debug.

        Não tenta fundir per-field — risco alto de misturar CPF de uma
        com nome de outra, criando dados inconsistentes. Melhor confiar
        em uma resposta inteira que é internamente coerente.
        """
        if not respostas:
            return {}
        if len(respostas) == 1:
            return respostas[0]

        nao_pendentes = [r for r in respostas if not r.get("_pendente")]
        candidatas = nao_pendentes or respostas

        candidatas_ord = sorted(candidatas, key=cls._contar_preenchidos, reverse=True)
        base = candidatas_ord[0]
        contagens = [cls._contar_preenchidos(r) for r in respostas]
        log.info(
            f"   Mesclando {len(respostas)} respostas — campos preenchidos: "
            f"{contagens} → escolhida a com {max(contagens)} campos"
        )

        # v2.16.38: tiebreak por DV de CPF quando há divergência. Antes,
        # a escolhida era só "a com mais campos" — risco de aceitar resposta
        # com CPF alucinado. Agora: se base tem CPF inválido E existe outra
        # candidata com CPF válido, troca a base.
        cpf_base = cls._cpf_da_resposta(base)
        if cpf_base and not cls._cpf_dv_valido(cpf_base):
            for alt in candidatas_ord[1:]:
                cpf_alt = cls._cpf_da_resposta(alt)
                if cpf_alt and cls._cpf_dv_valido(cpf_alt):
                    log.warning(
                        f"   ⚠ Tiebreak CPF: base tinha {cpf_base!r} "
                        f"(DV inválido), trocando por candidata com "
                        f"{cpf_alt!r} (DV válido). Claude alucinou na "
                        f"resposta com mais campos."
                    )
                    base = alt
                    break

        # Comparação em campos críticos pra auditoria
        cls._log_divergencias(respostas)
        return base

    @classmethod
    def _log_divergencias(cls, respostas: list[dict]) -> None:
        """Compara campos críticos entre as respostas. Loga warning quando
        ≥2 respostas têm valores DIFERENTES (não vazios) no mesmo campo —
        indica que Claude alucinou em pelo menos uma das chamadas.
        """
        valores_por_campo: dict[str, set] = {k: set() for k in cls.CAMPOS_CRITICOS_VERIFICACAO}

        # v2.16.17: normalização por tipo de campo antes de comparar.
        # Antes: 'cpf' = '037.811.331-33' vs '037811331-33' aparecia como
        # divergência (mesmo CPF, formatos diferentes). Idem datas BR vs ISO.
        # Agora: tira pontuação de docs, converte data pra ISO, etc.
        import re as _re
        import unicodedata as _ud
        CAMPOS_DOC = {"cpf", "identidade", "ctps", "pis"}  # comparar só dígitos
        CAMPOS_DATA = {"nascimento", "admissao", "dataidentidade",
                       "dataatestadoocupacional"}

        def _normalizar(campo: str, valor) -> str:
            s = str(valor).strip()
            if not s:
                return ""
            if campo in CAMPOS_DOC:
                return _re.sub(r"\D", "", s)
            if campo in CAMPOS_DATA:
                # ISO já?
                if len(s) >= 10 and s[4:5] == "-" and s[7:8] == "-":
                    return s[:10]
                # BR DD/MM/YYYY → ISO
                if len(s) == 10 and s[2:3] == "/" and s[5:6] == "/":
                    return f"{s[6:10]}-{s[3:5]}-{s[0:2]}"
                # BR DD-MM-YYYY → ISO
                if len(s) == 10 and s[2:3] == "-" and s[5:6] == "-":
                    return f"{s[6:10]}-{s[3:5]}-{s[0:2]}"
                return s.upper()
            # Nome ou genérico: uppercase + sem acentos + colapsa espaços
            s = _ud.normalize("NFD", s).encode("ASCII", "ignore").decode("ASCII")
            return " ".join(s.upper().split())

        def coletar_de_bloco(bloco: dict) -> None:
            attrs = (bloco.get("data") or {}).get("attributes") or {}
            for k in cls.CAMPOS_CRITICOS_VERIFICACAO:
                v = attrs.get(k)
                if v not in (None, "", 0):
                    valores_por_campo[k].add(_normalizar(k, v))

        for r in respostas:
            if r.get("_pendente"):
                dp = r.get("_dados_parciais") or {}
                for k in cls.CAMPOS_CRITICOS_VERIFICACAO:
                    v = dp.get(k)
                    if v not in (None, "", 0):
                        valores_por_campo[k].add(_normalizar(k, v))
                continue
            blocos = r.get("admissoes") or []
            if blocos:
                for b in blocos:
                    if isinstance(b, dict):
                        coletar_de_bloco(b)
            elif "data" in r:
                coletar_de_bloco(r)

        # Remove "" que pode aparecer de strings vazias normalizadas
        for k in list(valores_por_campo.keys()):
            valores_por_campo[k].discard("")
        divergentes = {k: v for k, v in valores_por_campo.items() if len(v) > 1}
        if divergentes:
            for campo, valores in divergentes.items():
                log.warning(
                    f"   ⚠ DIVERGÊNCIA em '{campo}' entre as chamadas: "
                    f"{sorted(valores)} — Claude pode ter alucinado em uma. "
                    f"Verifique o payload final."
                )

    @staticmethod
    def _parsear_json(resposta: str) -> dict:
        """Extrai JSON do primeiro objeto válido na resposta, tolerante a
        texto narrativo antes/depois.

        Estratégia em cascata (v2.14.1 — PATCHES.md §5):
          1. Bloco ```json {...} ``` (formato preferido — briefing exige)
          2. Bloco ``` {...} ``` (sem hint de linguagem)
          3. Matching balanceado: procura `{` e itera até `}` correspondente,
             respeitando aspas e escapes
          4. Fallback: primeiro `{` até último `}` (pode falhar se houver
             texto livre depois do JSON)

        Caso real (12/06): resposta começou com "Vou analisar todos os
        documentos..." e o parser quebrou. Briefing endurecido + estratégias
        abaixo coexistem como dupla defesa.
        """
        # Defesa preliminar: log warning se a resposta tem prosa antes do JSON
        # (sinal de briefing-drift — o modelo ignorou a regra de saída).
        texto = resposta.strip()
        if texto and not texto.startswith(("{", "```")):
            primeira_linha = texto.split("\n", 1)[0][:80]
            log.warning(
                f"   ⚠ Resposta Claude começa com prosa ('{primeira_linha}...') "
                f"em vez de bloco JSON — parser vai tentar recuperar"
            )

        # Estratégia 1+2: blocos de código markdown
        for pat in (
            r"```json\s*(\{[\s\S]*?\})\s*```",
            r"```\s*(\{[\s\S]*?\})\s*```",
        ):
            m = re.search(pat, resposta)
            if m:
                try:
                    return json.loads(m.group(1))
                except json.JSONDecodeError:
                    pass  # tenta próxima estratégia

        # Estratégia 3: matching balanceado a partir do primeiro `{`
        inicio = resposta.find("{")
        while inicio != -1:
            fim = _achar_fim_objeto(resposta, inicio)
            if fim > inicio:
                cru = resposta[inicio:fim + 1]
                try:
                    return json.loads(cru)
                except json.JSONDecodeError:
                    pass
            # tenta próximo `{` na string
            inicio = resposta.find("{", inicio + 1)

        # Estratégia 4 (fallback): primeiro { até último }
        inicio = resposta.find("{")
        fim = resposta.rfind("}")
        if inicio != -1 and fim > inicio:
            cru = resposta[inicio:fim + 1]
            try:
                return json.loads(cru)
            except json.JSONDecodeError as e:
                raise ValueError(f"JSON inválido do Claude: {e}\nConteúdo:\n{cru[:1000]}")

        raise ValueError(f"Claude não retornou JSON parseable:\n{resposta[:500]}")
