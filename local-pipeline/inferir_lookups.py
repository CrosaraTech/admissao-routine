"""inferir_lookups.py — rede de segurança pra mapear texto livre em ids
de relationships do eContador quando o Claude esquece de incluir.

Cobertura conservadora: só infere quando há um sinal DETERMINÍSTICO no
texto (ex: município termina com "- UF" ou "/UF"). Sem chute heurístico.

v2.16.18: criado em resposta ao caso JOHN LENNON (sexo + naturalidade
ausentes mesmo com municipionascimento extraído como "SITIO NOVO DO
TOCANTINS"). O Claude tinha a info mas não mapeou pros relationships.
"""
from __future__ import annotations

import logging
import re
import unicodedata

log = logging.getLogger("admissao.inferir_lookups")

# Tabela §7.1 do briefing — UFs do eContador
UF_PARA_ID = {
    "AC": 1, "AL": 2, "AP": 3, "AM": 4, "BA": 5, "CE": 6, "DF": 7,
    "ES": 8, "GO": 9, "MA": 10, "MT": 11, "MS": 12, "MG": 13,
    "PA": 14, "PB": 15, "PR": 16, "PE": 17, "PI": 18, "RR": 19,
    "RO": 20, "RJ": 21, "RN": 22, "RS": 23, "SC": 24, "SP": 25,
    "SE": 26, "TO": 27,
}

# Nomes completos de estados → id (fallback quando o doc escreveu por extenso)
NOME_ESTADO_PARA_ID = {
    "ACRE": 1, "ALAGOAS": 2, "AMAPA": 3, "AMAZONAS": 4, "BAHIA": 5,
    "CEARA": 6, "DISTRITO FEDERAL": 7, "ESPIRITO SANTO": 8, "GOIAS": 9,
    "MARANHAO": 10, "MATO GROSSO": 11, "MATO GROSSO DO SUL": 12,
    "MINAS GERAIS": 13, "PARA": 14, "PARAIBA": 15, "PARANA": 16,
    "PERNAMBUCO": 17, "PIAUI": 18, "RORAIMA": 19, "RONDONIA": 20,
    "RIO DE JANEIRO": 21, "RIO GRANDE DO NORTE": 22, "RIO GRANDE DO SUL": 23,
    "SANTA CATARINA": 24, "SAO PAULO": 25, "SERGIPE": 26, "TOCANTINS": 27,
}


def _sem_acento(s: str) -> str:
    s = unicodedata.normalize("NFD", s).encode("ASCII", "ignore").decode("ASCII")
    return s.upper().strip()


def uf_de_municipio(municipio: str) -> tuple[str | None, int | None]:
    """Extrai a UF de uma string de município. Retorna (sigla, id_estado).

    Aceita formatos:
      - "GOIANIA - GO"      → ("GO", 9)
      - "GOIANIA-GO"        → ("GO", 9)
      - "GOIANIA/GO"        → ("GO", 9)
      - "GOIANIA, GO"       → ("GO", 9)
      - "SITIO NOVO DO TOCANTINS - TO" → ("TO", 27)
      - "São Paulo SP"      → ("SP", 25)  (espaço no fim, UF maiúscula)

    Devolve (None, None) se não conseguir identificar.
    """
    if not municipio:
        return None, None
    s = _sem_acento(str(municipio))
    # Padrão 1: termina com separador + 2 letras (UF)
    m = re.search(r"[\-/,]\s*([A-Z]{2})\s*$", s)
    if not m:
        # Padrão 2: espaço + 2 letras maiúsculas no final
        m = re.search(r"\s+([A-Z]{2})\s*$", s)
    if m:
        sigla = m.group(1)
        if sigla in UF_PARA_ID:
            return sigla, UF_PARA_ID[sigla]
    # Padrão 3: nome de estado por extenso
    for nome, id_est in NOME_ESTADO_PARA_ID.items():
        if s.endswith(" " + nome) or s.endswith("- " + nome) or s == nome:
            # Acha a sigla pelo id
            for sigla, sid in UF_PARA_ID.items():
                if sid == id_est:
                    return sigla, id_est
    return None, None


def inferir_relationships_ausentes(payload: dict) -> dict:
    """Examina attributes e relationships do payload, infere o que dá pra
    inferir SEM CHUTAR, e retorna as relationships novas pra adicionar.

    Inferências determinísticas implementadas (v2.16.18):
      - naturalidade: se `municipionascimento` termina com "- UF" ou similar,
        deriva o id do estado.

    NÃO implementado (precisa de fonte explícita):
      - sexo: não dá pra inferir do nome (Marcia masculino, John feminino).
              Aviso é levantado no log pra DP saber que tem que pedir.
    """
    data = payload.get("data") or {}
    attrs = data.get("attributes") or {}
    rels = data.get("relationships") or {}
    novas: dict = {}

    # naturalidade ← municipionascimento
    if "naturalidade" not in rels:
        municipio = attrs.get("municipionascimento") or ""
        sigla, id_est = uf_de_municipio(municipio)
        if id_est:
            novas["naturalidade"] = {
                "data": {"type": "estados", "id": str(id_est)}
            }
            log.info(
                f"   🧭 Naturalidade inferida: '{municipio}' → "
                f"{sigla} (id={id_est})"
            )
        elif municipio:
            log.warning(
                f"   ⚠ municipionascimento='{municipio}' sem UF detectável — "
                f"naturalidade fica em branco (Claude precisava ter mandado a rel)"
            )

    return novas
