"""Resolução de departamento — 3 regras de negócio.

REGRA 1: empresa com 1 depto → usa direto.
REGRA 2: empresa com 2 deptos (padrão GERAL + NOME_EMPRESA) → usa o não-GERAL.
REGRA 3: 3 empresas especiais com múltiplos deptos reais → ler departamentos.json
         e fazer match fuzzy entre o departamento_sugerido (extraído pelo Claude)
         e as variantes configuradas.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path


log = logging.getLogger("admissao.depto")

# v2.14.0 — sentinela "resolvido como SEM departamento".
# Quando resolver_departamento devolve este valor, o payload_builder deve
# OMITIR a relationship `departamento` inteira (DP atribui no Desktop).
# Comportamento atrás de flag (config.json: postar_sem_departamento_quando_vazio)
# porque NUNCA postamos sem depto em produção — validar com 1 cobaia antes.
SEM_DEPARTAMENTO = "__SEM_DEPARTAMENTO__"


CNPJS_ESPECIAIS = {
    "08867336000168",  # SOL NASCENTE TRANSPORTADORA E LOGISTICA LTDA
    "08881442000104",  # ROSA DE OURO DISTRIBUICAO E LOGISTICA LTDA
    "02199795000134",  # EDMAR VILELA LTDA
}


def _norm(s: str) -> str:
    """Lowercase, sem acento, sem pontuação extra — pra comparação fuzzy."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^\w\s/-]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _cnpj_digitos(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")


def _carregar_departamentos_json(cnpj_digits: str, fallback_paths: list[Path]) -> dict | None:
    """Procura o config da empresa no departamentos.json (raiz ou local)."""
    for path in fallback_paths:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning(f"Falha lendo {path}: {e}")
            continue
        empresas = data.get("empresas", {})
        if cnpj_digits in empresas:
            return empresas[cnpj_digits]
    return None


def resolver_departamento(
    empresa_id: str,
    cnpj_empresa: str,
    razao_social: str,
    deptos_api: list[dict],
    departamento_sugerido: str | None,
    departamentos_json_paths: list[Path],
    permitir_sem_departamento: bool = False,
) -> tuple[str | None, str]:
    """Retorna (departamento_id, motivo_ou_'ok').

    Args:
      empresa_id: id da empresa no eContador (já resolvido)
      cnpj_empresa: CNPJ pra checar lista de especiais
      razao_social: razão social da empresa (pra REGRA 2)
      deptos_api: lista [{id, nome}] vinda de GET /departamentos
      departamento_sugerido: string livre extraída pelo Claude (opcional)
      departamentos_json_paths: paths pra procurar departamentos.json
      permitir_sem_departamento: v2.14.0 — empresa com 0 deptos no eContador
          deixa de virar pendência interna e devolve (SEM_DEPARTAMENTO, "ok").
          6 das 11 pendências abertas em 12/06/2026 eram exatamente esse caso.
    """
    cnpj_d = _cnpj_digitos(cnpj_empresa)

    if not deptos_api:
        if permitir_sem_departamento:
            log.info(
                f"[REGRA 0] Empresa {empresa_id} sem deptos no eContador → "
                f"POST sem departamento (DP atribui no Desktop)"
            )
            return SEM_DEPARTAMENTO, "ok (sem departamento — DP atribui no Desktop)"
        return None, (
            f"Empresa {empresa_id} sem departamentos cadastrados no eContador. "
            f"Saída rápida: cadastrar 1 depto (ex: GERAL) no eContador e clicar "
            f"'Reprocessar email'. Alternativa: ligar "
            f"postar_sem_departamento_quando_vazio no config.json (validar com "
            f"cobaia antes — ver PATCHES.md)."
        )

    # REGRA 3: empresas especiais com múltiplos deptos reais
    if cnpj_d in CNPJS_ESPECIAIS:
        return _resolver_fuzzy_especial(
            cnpj_d, deptos_api, departamento_sugerido, departamentos_json_paths
        )

    # REGRA 1: apenas 1 departamento
    if len(deptos_api) == 1:
        d = deptos_api[0]
        log.info(f"[REGRA 1] 1 depto → {d['nome']} (id={d['id']})")
        return d["id"], "ok"

    # REGRA 2: 2 deptos, um chamado GERAL e outro = nome empresa
    if len(deptos_api) == 2:
        nao_geral = [d for d in deptos_api if _norm(d["nome"]) != "geral"]
        if len(nao_geral) == 1:
            d = nao_geral[0]
            log.info(f"[REGRA 2] 2 deptos (GERAL+empresa) → {d['nome']} (id={d['id']})")
            return d["id"], "ok"
        # Fallback: 2 deptos sem GERAL — escolher o mais próximo da razão social
        razao_norm = _norm(razao_social)
        melhor = max(
            deptos_api,
            key=lambda d: SequenceMatcher(None, _norm(d["nome"]), razao_norm).ratio(),
        )
        log.info(f"[REGRA 2-fallback] 2 deptos, melhor match razão social → {melhor['nome']}")
        return melhor["id"], "ok"

    # Múltiplos deptos mas empresa NÃO está na lista especial:
    # tenta match fuzzy contra departamento_sugerido por similaridade simples
    if departamento_sugerido:
        sug_norm = _norm(departamento_sugerido)
        candidato = max(
            deptos_api,
            key=lambda d: SequenceMatcher(None, _norm(d["nome"]), sug_norm).ratio(),
        )
        conf = SequenceMatcher(None, _norm(candidato["nome"]), sug_norm).ratio()
        if conf >= 0.6:
            log.info(
                f"[fuzzy-livre] {len(deptos_api)} deptos → {candidato['nome']} "
                f"(id={candidato['id']}, conf={conf:.0%})"
            )
            return candidato["id"], "ok"

    return None, (
        f"Empresa {empresa_id} (CNPJ {cnpj_d}) tem {len(deptos_api)} departamentos "
        f"e não está em departamentos.json. Departamento sugerido: "
        f"{departamento_sugerido or '(nenhum)'}. DP precisa resolver manual."
    )


def _resolver_fuzzy_especial(
    cnpj_d: str,
    deptos_api: list[dict],
    departamento_sugerido: str | None,
    departamentos_json_paths: list[Path],
) -> tuple[str | None, str]:
    """REGRA 3 — usa departamentos.json + match fuzzy contra variantes."""
    cfg = _carregar_departamentos_json(cnpj_d, departamentos_json_paths)
    if not cfg:
        return None, (
            f"CNPJ {cnpj_d} listado como especial mas não está em "
            f"departamentos.json — DP precisa popular."
        )

    if not departamento_sugerido:
        # Fallback: se a empresa tem `departamento_default_id` configurado,
        # usa esse direto sem precisar do Claude extrair (mais tolerante).
        default_id = cfg.get("departamento_default_id")
        if default_id:
            log.info(
                f"[REGRA 3-default] {cfg.get('razao_social', cnpj_d)}: "
                f"sem sugestão do Claude → usando departamento_default_id={default_id}"
            )
            return str(default_id), "ok"
        return None, (
            f"Empresa {cfg.get('razao_social', cnpj_d)} é multi-departamento; "
            f"Claude não extraiu departamento_sugerido do email/anexos e a "
            f"empresa não tem `departamento_default_id` configurado em "
            f"departamentos.json."
        )

    sug_norm = _norm(departamento_sugerido)
    deptos_api_by_id = {str(d["id"]): d for d in deptos_api}

    melhor: tuple[float, dict, str] | None = None
    for d_cfg in cfg.get("departamentos", []):
        id_cfg = str(d_cfg.get("id"))
        for v in d_cfg.get("nome_variantes", []):
            v_norm = _norm(v)
            # Match exato ou containment
            if sug_norm == v_norm or v_norm in sug_norm or sug_norm in v_norm:
                conf = 1.0
            else:
                conf = SequenceMatcher(None, sug_norm, v_norm).ratio()
            if melhor is None or conf > melhor[0]:
                melhor = (conf, d_cfg, v)

    if not melhor:
        return None, "Lista de departamentos vazia em departamentos.json"

    conf, d_cfg, variante = melhor
    id_resolved = str(d_cfg.get("id"))

    if conf < 0.6:
        return None, (
            f"Departamento '{departamento_sugerido}' não bate com nenhuma variante "
            f"de {cnpj_d} (melhor: '{variante}' {conf:.0%})"
        )

    # Verifica se o id resolvido bate com algum depto que veio da API
    if deptos_api_by_id and id_resolved not in deptos_api_by_id:
        log.warning(
            f"[REGRA 3] id={id_resolved} (de departamentos.json) não está em "
            f"GET /departamentos da empresa — pode ser desconfig."
        )

    nome_api = deptos_api_by_id.get(id_resolved, {}).get("nome", "?")
    log.info(
        f"[REGRA 3] '{departamento_sugerido}' → variante '{variante}' "
        f"→ id={id_resolved} ({nome_api}, conf={conf:.0%})"
    )
    return id_resolved, "ok"
