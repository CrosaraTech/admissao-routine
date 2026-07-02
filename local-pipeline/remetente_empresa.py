"""remetente_empresa.py — inferir CNPJ da empresa quando não foi extraído dos docs.

Quando o Claude não consegue achar CNPJ explícito nos documentos (caso real:
ficha sem CNPJ no rodapé), em vez de virar pendência interna direto, tentamos
inferir a empresa a partir de pistas no email:

  Estratégia 1 — ALIAS EXATO (fonte mais confiável):
      Cache persistente `remetente_aliases.json` mapeia
      `rh@modelofarma.com.br → cnpj`. Operador cadastra manualmente OU
      sistema aprende automaticamente em admissões bem-sucedidas.

  Estratégia 2 — DOMÍNIO:
      Email `qualquer@modelofarma.com.br` → procurar empresa cujo nome
      contenha "MODELOFARMA". Cobre cliente novo (sem alias ainda) com
      domínio próprio.

  Estratégia 3 — RAZÃO SOCIAL FUZZY (assunto + corpo do email):
      Busca substrings que parecem nome de empresa em `assunto` / `corpo`
      e faz fuzzy match contra razões sociais do cache. Threshold 90%+.

Cada estratégia retorna `(cnpj, razao_social, estrategia)` ou `None`.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import datetime
from pathlib import Path

log = logging.getLogger("admissao.remetente_empresa")

REMETENTE_ALIASES_FILE = Path(__file__).parent / "remetente_aliases.json"

# Domínios genéricos — pulam estratégia "por domínio" (não dá pra inferir
# nada útil de @gmail.com). Lista expansível.
DOMINIOS_GENERICOS = frozenset({
    "gmail.com", "googlemail.com",
    "hotmail.com", "outlook.com", "live.com", "msn.com",
    "yahoo.com", "yahoo.com.br", "ymail.com",
    "icloud.com", "me.com",
    "uol.com.br", "bol.com.br", "terra.com.br", "ig.com.br",
    "protonmail.com", "proton.me",
    "aol.com", "zoho.com",
})

# Palavras-chave societárias que ajudam a identificar nome de empresa
# em corpo de email (ex: "MODELOFARMA LTDA", "Padaria do Zé ME").
SUFIXOS_SOCIETARIOS = (
    "ltda", "me", "mei", "epp", "eireli", "s/a", "sa", "s.a", "ss",
    "comercial", "comercio", "industria", "industrial",
    "transportes", "transporte", "logistica", "engenharia",
    "construtora", "consultoria", "servicos", "serviços",
    "distribuidora", "farmacia", "farmácia", "padaria",
    "ltda.", "ltda-me",
)


# ============================================================
# Persistência de aliases
# ============================================================

def carregar_aliases() -> dict:
    """Lê `{remetente: {cnpj, razao_social, criado_em, fonte}}`.
    Retorna `{}` se arquivo não existir ou estiver corrompido."""
    if not REMETENTE_ALIASES_FILE.exists():
        return {}
    try:
        with REMETENTE_ALIASES_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"Falha lendo {REMETENTE_ALIASES_FILE}: {e}")
        return {}


def salvar_alias(
    remetente: str,
    cnpj: str,
    razao_social: str,
    fonte: str = "manual",
) -> None:
    """Salva (ou atualiza) `remetente → cnpj`. Idempotente.

    Args:
        remetente: email completo (`rh@modelofarma.com.br`) — case-insensitive
        cnpj: 14 dígitos
        razao_social: pra log/auditoria
        fonte: 'manual' (operador via UI) ou 'auto' (aprendizado em sucesso)
    """
    chave = _norm_email(remetente)
    if not chave or not cnpj:
        return

    aliases = carregar_aliases()
    aliases[chave] = {
        "cnpj": str(cnpj),
        "razao_social": razao_social.strip(),
        "criado_em": datetime.now().isoformat(timespec="seconds"),
        "fonte": fonte,
    }
    try:
        with REMETENTE_ALIASES_FILE.open("w", encoding="utf-8") as fh:
            json.dump(aliases, fh, ensure_ascii=False, indent=2)
        log.info(
            f"[alias remetente {fonte}] '{chave}' → {razao_social} "
            f"(cnpj={cnpj}) em {REMETENTE_ALIASES_FILE.name}"
        )
    except OSError as e:
        log.warning(f"Falha salvando alias de remetente: {e}")


def consultar_alias_exato(remetente: str) -> dict | None:
    """Procura alias exato. Retorna entry ou None."""
    chave = _norm_email(remetente)
    if not chave:
        return None
    aliases = carregar_aliases()
    return aliases.get(chave)


# ============================================================
# Estratégias de inferência
# ============================================================

def inferir_por_dominio(remetente: str, cache) -> dict | None:
    """Extrai domínio do remetente e procura empresas cujo NOME contenha
    a parte principal do domínio.

    Exemplos:
      rh@modelofarma.com.br → procura "MODELOFARMA"
      contato@padariadoze.com.br → procura "PADARIA DO ZE"

    Domínios genéricos (gmail, hotmail, etc.) retornam None — não há sinal.
    """
    dominio = _extrair_dominio(remetente)
    if not dominio or dominio.lower() in DOMINIOS_GENERICOS:
        return None

    # Pega a parte principal do domínio (ex: "modelofarma" de "modelofarma.com.br")
    raiz = dominio.split(".")[0].lower()
    if len(raiz) < 4:
        # Raízes curtas (3 chars) batem com qualquer coisa — perigoso
        return None

    # Procura no cache empresas cujo nome contenha a raiz (case-insensitive)
    matches = []
    for cnpj, info in cache.detalhes.items():
        razao = _norm_texto(info.get("razao_social", ""))
        if raiz in razao:
            matches.append((cnpj, info.get("razao_social", ""), info.get("empresa_id", "")))

    if len(matches) == 1:
        cnpj, razao, empresa_id = matches[0]
        log.info(
            f"   🔍 Inferido por DOMÍNIO: '{dominio}' → {razao} "
            f"(cnpj={cnpj}, empresa_id={empresa_id})"
        )
        return {
            "cnpj": cnpj,
            "razao_social": razao,
            "empresa_id": empresa_id,
            "estrategia": "dominio",
        }
    if len(matches) > 1:
        nomes = ", ".join(m[1] for m in matches[:5])
        log.warning(
            f"   ⚠ Domínio '{dominio}' ambíguo — {len(matches)} candidatos: {nomes}"
        )
    return None


def inferir_por_razao_fuzzy(
    texto_busca: str,
    cache,
    threshold: int = 90,
) -> dict | None:
    """Procura nomes de empresa no texto e faz fuzzy match contra cache.

    Args:
        texto_busca: corpo + assunto do email concatenados
        cache: EmpresasCache
        threshold: similaridade mínima (0-100) pra aceitar match

    Returns:
        Match único acima do threshold ou None. Múltiplos matches → None
        (operador resolve manualmente).
    """
    if not texto_busca or not cache.detalhes:
        return None

    from difflib import SequenceMatcher

    # Extrai candidatos a "nome de empresa" do texto: palavras em UPPERCASE
    # OU sequências terminadas por sufixo societário ("LTDA", "S/A", etc).
    candidatos_texto = _extrair_candidatos_empresa(texto_busca)
    if not candidatos_texto:
        return None

    melhor_score = 0
    melhor_match = None
    for candidato in candidatos_texto:
        cand_norm = _norm_texto(candidato)
        if len(cand_norm) < 5:
            continue
        cand_tokens = {t for t in cand_norm.split() if len(t) >= 5}
        for cnpj, info in cache.detalhes.items():
            razao = info.get("razao_social", "")
            razao_norm = _norm_texto(razao)
            if not razao_norm:
                continue
            # Base score: similaridade da string completa
            score = int(SequenceMatcher(None, cand_norm, razao_norm).ratio() * 100)
            # Boost por substring bilateral
            if cand_norm in razao_norm or razao_norm in cand_norm:
                score = max(score, 95)
            # Boost por token exato — pega "ADMISSAO MODELOFARMA" vs
            # "MODELOFARMA LTDA": ambos têm o token "modelofarma" intacto.
            razao_tokens = {t for t in razao_norm.split() if len(t) >= 5}
            tokens_comuns = cand_tokens & razao_tokens
            if tokens_comuns:
                # Boost proporcional à fração de tokens significativos compartilhados
                frac = len(tokens_comuns) / max(len(cand_tokens), len(razao_tokens), 1)
                if frac >= 0.5:
                    score = max(score, 95)
                else:
                    score = max(score, 85)
            if score > melhor_score:
                melhor_score = score
                melhor_match = (cnpj, razao, info.get("empresa_id", ""), candidato)

    if melhor_match and melhor_score >= threshold:
        cnpj, razao, empresa_id, candidato = melhor_match
        log.info(
            f"   🔍 Inferido por RAZÃO FUZZY ({melhor_score}%): "
            f"'{candidato}' → {razao} (cnpj={cnpj})"
        )
        return {
            "cnpj": cnpj,
            "razao_social": razao,
            "empresa_id": empresa_id,
            "estrategia": f"razao_fuzzy_{melhor_score}",
        }
    if melhor_match:
        log.debug(
            f"   Melhor fuzzy match foi {melhor_score}% (abaixo de {threshold}%): "
            f"{melhor_match[3]} → {melhor_match[1]}"
        )
    return None


def resolver(
    remetente: str,
    texto_email: str,
    cache,
) -> dict | None:
    """Orquestra as 3 estratégias em ordem de confiabilidade.

    Args:
        remetente: email do remetente (`rh@x.com.br`)
        texto_email: assunto + corpo concatenados (pra fuzzy match)
        cache: EmpresasCache populado

    Returns:
        dict {cnpj, razao_social, empresa_id, estrategia} ou None.
        Cabe ao caller validar com `cache.contem(cnpj)` antes de usar.
    """
    if not cache or not cache.carregado:
        return None

    # 1. Alias exato
    alias = consultar_alias_exato(remetente)
    if alias:
        cnpj = alias["cnpj"]
        # Confirma que ainda existe no cache (empresa pode ter sido removida)
        if cache.contem(cnpj):
            info = cache.info(cnpj) or {}
            log.info(
                f"   🔍 ALIAS de remetente: '{_norm_email(remetente)}' → "
                f"{alias.get('razao_social', '?')} (cnpj={cnpj}, fonte={alias.get('fonte')})"
            )
            return {
                "cnpj": cnpj,
                "razao_social": alias.get("razao_social") or info.get("razao_social", ""),
                "empresa_id": info.get("empresa_id", ""),
                "estrategia": f"alias_{alias.get('fonte', 'manual')}",
            }
        else:
            log.warning(
                f"   ⚠ Alias salvo aponta pra CNPJ {cnpj} que NÃO está mais "
                f"no cache de empresas. Tentando outras estratégias."
            )

    # 2. Domínio do email
    por_dominio = inferir_por_dominio(remetente, cache)
    if por_dominio:
        return por_dominio

    # 3. Razão social fuzzy
    por_razao = inferir_por_razao_fuzzy(texto_email, cache)
    if por_razao:
        return por_razao

    return None


# ============================================================
# Helpers internos
# ============================================================

_RE_EMAIL_ENTRE_BRACKETS = re.compile(r"<([^>]+@[^>]+)>")
_RE_DOMINIO = re.compile(r"@([^\s>]+)")


def _norm_email(email: str | None) -> str:
    """Normaliza email pra chave de alias: extrai o endereço puro (se vier
    em formato 'Nome <a@b.c>'), lowercase, sem espaços."""
    if not email:
        return ""
    s = str(email).strip()
    m = _RE_EMAIL_ENTRE_BRACKETS.search(s)
    if m:
        s = m.group(1)
    return s.strip().lower()


def _extrair_dominio(email: str | None) -> str:
    """Extrai 'modelofarma.com.br' de 'rh@modelofarma.com.br'."""
    e = _norm_email(email)
    if not e or "@" not in e:
        return ""
    return e.split("@", 1)[1]


def _norm_texto(s: str) -> str:
    """Remove acentos, lowercase, colapsa espaços."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s.lower()).strip()


def _extrair_candidatos_empresa(texto: str) -> list[str]:
    """Procura em `texto` strings que parecem nome de empresa.

    Heurísticas:
      a) Sequências em CAIXA ALTA com 2+ palavras (>= 4 chars cada)
         Ex: "MODELOFARMA LTDA", "JOSE DA SILVA ME"
      b) Sequências (qualquer case) seguidas de sufixo societário
         Ex: "Padaria do Zé Ltda", "Transportes Silva ME"
    """
    if not texto:
        return []

    candidatos: list[str] = []

    # (a) UPPERCASE 2+ palavras
    for m in re.finditer(r"\b[A-ZÁÉÍÓÚÂÊÔÃÕÇ]{4,}(?:\s+[A-ZÁÉÍÓÚÂÊÔÃÕÇ&/.\-]{2,}){1,6}\b", texto):
        s = m.group(0).strip()
        if 8 <= len(s) <= 80:
            candidatos.append(s)

    # (b) Qualquer case + sufixo societário
    sufixos_pattern = r"\b(?:" + "|".join(re.escape(s) for s in SUFIXOS_SOCIETARIOS) + r")\b"
    for m in re.finditer(
        r"([A-ZÁÉÍÓÚÂÊÔÃÕÇa-záéíóúâêôãõç&/.\- ]{4,80}?)\s+" + sufixos_pattern,
        texto,
        re.IGNORECASE,
    ):
        s = m.group(0).strip()
        if 8 <= len(s) <= 80:
            candidatos.append(s)

    # Dedup preservando ordem
    visto: set[str] = set()
    unicos: list[str] = []
    for c in candidatos:
        k = c.lower()
        if k not in visto:
            visto.add(k)
            unicos.append(c)
    return unicos
