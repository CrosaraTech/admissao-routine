"""Resolução de função via planilha CBO.

Planilha esperada (funcoes_cbo.xlsx) com colunas:
  - nome_cargo   (string — nome do cargo cadastrado no eContador)
  - cbo          (string ou int — código CBO de 6 dígitos)
  - funcao_id    (string ou int — id da função no eContador)

Estratégia:
  1. Match exato por CBO (se Claude extraiu o CBO da ficha)
  2. Match fuzzy por nome do cargo
  3. Se múltiplos candidatos com score alto, retorna a lista pro pipeline
     repassar pro Claude desambiguar
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
        items.append({"nome_cargo": nome, "cbo": cbo, "funcao_id": fid})
    return items


def resolver_funcao(
    planilha: list[dict],
    cargo_extraido: str | None,
    cbo_extraido: str | int | None = None,
) -> tuple[str | None, float, list[dict], str]:
    """Tenta resolver a função.

    Retorna (funcao_id, confianca, candidatos_ambiguos, motivo).
      - funcao_id != None e candidatos_ambiguos vazio → resolveu sozinho
      - funcao_id None e candidatos_ambiguos não-vazio → ambíguo, repassar pro Claude
      - funcao_id None e candidatos_ambiguos vazio → não encontrou nada
    """
    if not planilha:
        return None, 0.0, [], "Planilha CBO vazia"

    cbo = _cbo_digitos(cbo_extraido)
    cargo_norm = _norm(cargo_extraido or "")

    # ---- Passo 1: match exato por CBO --------------------------
    if cbo:
        por_cbo = [f for f in planilha if f["cbo"] == cbo]
        if len(por_cbo) == 1:
            f = por_cbo[0]
            log.info(f"[CBO único] {cbo} → {f['nome_cargo']} (id={f['funcao_id']})")
            return f["funcao_id"], 1.0, [], "ok"
        if len(por_cbo) > 1:
            # Tenta desempatar com nome
            if cargo_norm:
                melhor_score = 0.0
                melhor: dict | None = None
                for f in por_cbo:
                    s = SequenceMatcher(None, cargo_norm, _norm(f["nome_cargo"])).ratio()
                    if s > melhor_score:
                        melhor_score, melhor = s, f
                if melhor and melhor_score >= CONFIANCA_ALTA:
                    log.info(
                        f"[CBO+nome] {cbo} → {melhor['nome_cargo']} "
                        f"(id={melhor['funcao_id']}, conf={melhor_score:.0%})"
                    )
                    return melhor["funcao_id"], melhor_score, [], "ok"
            # Múltiplos com mesmo CBO → ambíguo, pede ao Claude
            log.info(f"[CBO ambíguo] {cbo} tem {len(por_cbo)} funções, repassar pro Claude")
            return None, 0.0, por_cbo, f"CBO {cbo} ambíguo ({len(por_cbo)} cargos)"

    # ---- Passo 2: match fuzzy por nome --------------------------
    if not cargo_norm:
        return None, 0.0, [], "Cargo não informado"

    scores: list[tuple[float, dict]] = []
    for f in planilha:
        nome_norm = _norm(f["nome_cargo"])
        s = SequenceMatcher(None, cargo_norm, nome_norm).ratio()
        # Bonus: todos os tokens do cargo aparecem no nome
        if cargo_norm and all(t in nome_norm for t in cargo_norm.split()):
            s = max(s, 0.9)
        scores.append((s, f))

    scores.sort(key=lambda x: x[0], reverse=True)
    if not scores:
        return None, 0.0, [], "Nenhum cargo na planilha"

    top_score, top = scores[0]
    if top_score >= CONFIANCA_ALTA:
        # Verifica se o segundo lugar é próximo (ambiguidade)
        if len(scores) >= 2 and (top_score - scores[1][0]) < 0.05:
            # Ambíguo — pega os top 5 com score alto
            ambiguos = [f for s, f in scores[:5] if s >= CONFIANCA_AMBIGUA]
            return None, top_score, ambiguos, "Match alto mas ambíguo (vários cargos parecidos)"
        log.info(f"[fuzzy alto] '{cargo_extraido}' → {top['nome_cargo']} (id={top['funcao_id']}, conf={top_score:.0%})")
        return top["funcao_id"], top_score, [], "ok"

    if top_score >= CONFIANCA_AMBIGUA:
        ambiguos = [f for s, f in scores[:5] if s >= CONFIANCA_AMBIGUA]
        return None, top_score, ambiguos, f"Match com dúvida (melhor: {top['nome_cargo']} {top_score:.0%})"

    return None, top_score, [], (
        f"Cargo '{cargo_extraido}' não encontrado na planilha "
        f"(melhor: '{top['nome_cargo']}' {top_score:.0%})"
    )
