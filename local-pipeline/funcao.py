"""Resolução de função via planilha CBO.

Planilha esperada (funcoes_cbo.xlsx) com colunas:
  - usar         (opcional; "X" pra marcar cargos que o escritório usa)
  - nome_cargo   (string — nome do cargo cadastrado no eContador)
  - cbo          (string ou int — código CBO de 6 dígitos)
  - funcao_id    (string ou int — id da função no eContador)

Estratégia:
  1. Calcula score semântico (CBO exato + fuzzy por nome) pra TODA a planilha
  2. Se algum cargo marcado com X tem score >= alto → usa
  3. Se algum X tem score >= ambíguo → repassa lista X pro Claude desempatar
  4. Fallback: aplica mesma lógica em toda a planilha (sem filtrar por X)

Isso permite o escritório curar uma whitelist de cargos "oficiais" sem perder
a capacidade de matchear contra os 9k+ cargos do eContador quando preciso.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

from openpyxl import load_workbook


log = logging.getLogger("admissao.funcao")


CONFIANCA_ALTA = 0.85
CONFIANCA_AMBIGUA = 0.65


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s.lower()).strip()


def _cbo_digitos(s: str | int | None) -> str:
    return re.sub(r"\D", "", str(s or ""))


def carregar_planilha(path: Path) -> list[dict]:
    """Lê funcoes_cbo.xlsx e retorna lista de {nome_cargo, cbo, funcao_id}."""
    if not path.exists():
        raise FileNotFoundError(
            f"Planilha CBO não encontrada em {path}. "
            f"Crie com colunas: nome_cargo, cbo, funcao_id"
        )
    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb.active

    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []

    # Primeira linha = cabeçalho
    header = [_norm(str(c or "")) for c in rows[0]]

    def col_idx(*nomes_aceitos: str) -> int:
        for i, h in enumerate(header):
            if h in nomes_aceitos:
                return i
        return -1

    i_nome = col_idx("nome_cargo", "nome", "cargo")
    i_cbo = col_idx("cbo")
    i_id = col_idx("funcao_id", "id", "funcaoid")
    i_usar = col_idx("usar")  # opcional

    if min(i_nome, i_cbo, i_id) < 0:
        raise ValueError(
            f"Planilha {path} sem colunas obrigatórias (nome_cargo, cbo, funcao_id). "
            f"Cabeçalho encontrado: {header}"
        )

    items: list[dict] = []
    for row in rows[1:]:
        if not row or not any(row):
            continue
        nome = str(row[i_nome] or "").strip()
        cbo = _cbo_digitos(row[i_cbo])
        fid = str(row[i_id] or "").strip()
        if not nome or not fid:
            continue
        usar = False
        if i_usar >= 0:
            usar = str(row[i_usar] or "").strip().upper() == "X"
        items.append({"nome_cargo": nome, "cbo": cbo, "funcao_id": fid, "usar": usar})
    return items


def _score_linha(cargo_norm: str, cbo: str, linha: dict) -> float:
    """Score combinado: bonus alto se CBO bate, senão similaridade textual."""
    nome_norm = _norm(linha["nome_cargo"])
    s = SequenceMatcher(None, cargo_norm, nome_norm).ratio() if cargo_norm else 0.0
    # Bonus tokens: todos os tokens do cargo aparecem no nome
    if cargo_norm and all(t in nome_norm for t in cargo_norm.split()):
        s = max(s, 0.9)
    # Bonus CBO: se o CBO bate, considera quase certo
    if cbo and linha.get("cbo") == cbo:
        s = max(s, 0.95)
    return s


def _resolver_em(
    candidatos: list[dict],
    cargo_norm: str,
    cbo: str,
    contexto: str,
) -> tuple[str | None, float, list[dict], str]:
    """Roda a lógica de match num subconjunto da planilha.

    Retorna o mesmo formato de `resolver_funcao`.
    `contexto`: string descritiva pro log ("X-marcado" ou "planilha completa").
    """
    if not candidatos:
        return None, 0.0, [], f"Nenhum candidato em {contexto}"

    scores: list[tuple[float, dict]] = [
        (_score_linha(cargo_norm, cbo, f), f) for f in candidatos
    ]
    scores.sort(key=lambda x: x[0], reverse=True)
    top_score, top = scores[0]

    if top_score >= CONFIANCA_ALTA:
        # Ambiguidade: top empata com o segundo
        if len(scores) >= 2 and (top_score - scores[1][0]) < 0.05:
            empatados = [f for s, f in scores if abs(s - top_score) < 0.05]

            # Caso especial: cargos eContador tem DUPLICATAS (mesmo nome
            # exato + mesmo CBO, IDs diferentes). Pra esses, qualquer ID
            # serve — pegamos o primeiro determinístico (menor id) sem
            # incomodar o cliente.
            nome_top = _norm(top["nome_cargo"])
            cbo_top = str(top.get("cbo") or "")
            duplicatas = [
                f for f in empatados
                if _norm(f["nome_cargo"]) == nome_top
                and str(f.get("cbo") or "") == cbo_top
            ]
            if len(duplicatas) == len(empatados):
                escolhido = min(duplicatas, key=lambda f: int(str(f["funcao_id"]) or "0"))
                log.info(
                    f"[{contexto}] '{escolhido['nome_cargo']}' "
                    f"(id={escolhido['funcao_id']}, conf={top_score:.0%}) "
                    f"— {len(duplicatas)} duplicata(s) do eContador, escolhi a 1ª"
                )
                return escolhido["funcao_id"], top_score, [], "ok"

            ambiguos = [f for s, f in scores[:5] if s >= CONFIANCA_AMBIGUA]
            return (
                None, top_score, ambiguos,
                f"Match alto mas ambíguo em {contexto} ({len(ambiguos)} cargos parecidos)",
            )
        log.info(
            f"[{contexto}] '{top['nome_cargo']}' (id={top['funcao_id']}, "
            f"conf={top_score:.0%})"
        )
        return top["funcao_id"], top_score, [], "ok"

    if top_score >= CONFIANCA_AMBIGUA:
        ambiguos = [f for s, f in scores[:5] if s >= CONFIANCA_AMBIGUA]
        return (
            None, top_score, ambiguos,
            f"Match com dúvida em {contexto} (melhor: {top['nome_cargo']} {top_score:.0%})",
        )

    return None, top_score, [], (
        f"Sem match em {contexto} (melhor: '{top['nome_cargo']}' {top_score:.0%})"
    )


def resolver_funcao(
    planilha: list[dict],
    cargo_extraido: str | None,
    cbo_extraido: str | int | None = None,
) -> tuple[str | None, float, list[dict], str]:
    """Tenta resolver a função.

    Estratégia em 2 passos:
      1. Tenta resolver SÓ entre os cargos marcados com X na coluna `usar`
         (whitelist curada do escritório).
      2. Se nenhum cargo X bate, faz fallback pra toda a planilha.

    Retorna (funcao_id, confianca, candidatos_ambiguos, motivo).
      - funcao_id != None e candidatos_ambiguos vazio → resolveu sozinho
      - funcao_id None e candidatos_ambiguos não-vazio → ambíguo, repassar pro Claude
      - funcao_id None e candidatos_ambiguos vazio → não encontrou nada
    """
    if not planilha:
        return None, 0.0, [], "Planilha CBO vazia"

    cbo = _cbo_digitos(cbo_extraido)
    cargo_norm = _norm(cargo_extraido or "")

    if not cargo_norm and not cbo:
        return None, 0.0, [], "Cargo e CBO não informados"

    marcados = [f for f in planilha if f.get("usar")]

    # Passo 1: tenta só nos marcados com X
    if marcados:
        log.info(f"🔎 Tentando match em {len(marcados)} cargo(s) marcado(s) com X")
        fid, conf, amb, msg = _resolver_em(marcados, cargo_norm, cbo, "X-marcado")
        if fid is not None:
            return fid, conf, [], "ok"
        if amb:
            # Ambíguo entre X — repassa pro Claude desempatar SÓ entre os X
            return None, conf, amb, msg
        log.info(f"   Sem match entre X-marcados — fallback pra planilha completa")

    # Passo 2: fallback pra planilha inteira
    return _resolver_em(planilha, cargo_norm, cbo, "planilha completa")
