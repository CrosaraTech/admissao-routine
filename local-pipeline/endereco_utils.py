"""endereco_utils.py — utilitários compartilhados de parsing de endereço.

v2.16.25: extraído pra evitar import circular (main.py importa
payload_builder.py, então o parser que ambos usam fica num módulo neutro).

Caso de uso principal: o Claude às vezes manda o endereço como UMA string
única em vez de campos separados (cep, rua, bairro, cidade). O eContador
não tem campo "endereco" — exige os campos separados. Esse parser quebra
a string em campos quando o Claude esquece de estruturar.
"""
from __future__ import annotations

import re


def parsear_endereco_string(endereco: str) -> dict:
    """Extrai cep/rua/numero/bairro/cidade de uma string de endereço.

    Conservador — só preenche o que conseguir identificar com confiança.

    Casos cobertos:
      - 'RUA CAOLENITA NT, Q 007 L 1B, VILA OLIVEIRA, APARECIDA DE GOIANIA - GO, CEP 74956-140'
        → rua=RUA CAOLENITA NT, bairro=VILA OLIVEIRA, cidade=APARECIDA DE GOIANIA, cep=74956-140
      - 'Rua das Camélias, 123, Setor Bueno, Goiânia-GO, 74390-100'
        → rua, numero, bairro, cidade, cep
      - 'Av. Brasil 1000, Centro, São Paulo/SP, 01000-000'
        → rua, numero, bairro, cidade, cep
    """
    if not endereco or not isinstance(endereco, str):
        return {}
    out: dict = {}

    # 1) CEP — sempre 8 dígitos (com ou sem hífen), opcional "CEP" prefix
    m_cep = re.search(
        r"\bCEP\s*[:\-]?\s*(\d{2}\.?\d{3}-?\d{3})\b|(\d{5}-?\d{3})\b",
        endereco, re.IGNORECASE,
    )
    if m_cep:
        digitos = re.sub(r"\D", "", m_cep.group(0))
        if len(digitos) == 8:
            out["cep"] = f"{digitos[:5]}-{digitos[5:]}"
            endereco = endereco.replace(m_cep.group(0), "").strip(" ,;")

    # 2) Cidade-UF no fim — padrão "CIDADE - UF" ou "CIDADE/UF"
    m_uf = re.search(
        r"([A-ZÀ-Ÿa-zà-ÿ][A-ZÀ-Ÿa-zà-ÿ\s]+?)\s*[\-/]\s*([A-Z]{2})\b",
        endereco,
    )
    if m_uf:
        out["cidade"] = m_uf.group(1).strip().upper()
        endereco = endereco.replace(m_uf.group(0), "").strip(" ,;-")

    # 3) Quebra o resto por vírgula
    partes = [p.strip() for p in endereco.split(",") if p.strip()]
    if not partes:
        return out

    # Primeira parte = rua (e talvez número junto)
    rua_e_num = partes[0]
    m_num = re.search(r"\b(\d{1,6})\s*$", rua_e_num)
    if m_num:
        out["numero"] = int(m_num.group(1))
        out["rua"] = rua_e_num[:m_num.start()].strip().upper()
    else:
        out["rua"] = rua_e_num.strip().upper()

    # Última parte (se não foi cidade) = bairro; pula complementos (Q X L Y)
    for p in reversed(partes[1:]):
        p_up = p.upper().strip()
        if re.match(r"^Q\.?\s*\d+\s*L\.?\s*", p_up) or re.match(
            r"^(N|N[ºO]\.?|NUM\.?)\s*\d+", p_up
        ):
            if "complemento" not in out:
                out["complemento"] = p_up
            continue
        if "bairro" not in out:
            out["bairro"] = p_up
            break

    return out


CHAVES_ENDERECO_STRING = (
    "endereco",
    "endereco_completo",
    "enderecoresidencial",
    "endereco_residencial",
    "logradouro_completo",
)


def expandir_endereco_string_em_attrs(attrs: dict) -> list[str]:
    """Se `attrs` tem uma chave de endereço como string única E os campos
    separados (cep, rua, bairro, cidade) estão vazios, parsea a string e
    preenche os campos. Sempre REMOVE as chaves de string única (não são
    campos do eContador). Retorna lista de chaves preenchidas pra log.

    Idempotente: se já tem campos separados, NÃO sobrescreve.
    """
    if not isinstance(attrs, dict):
        return []

    # Acha alguma chave de endereço string
    end_str = None
    for k in CHAVES_ENDERECO_STRING:
        v = attrs.get(k)
        if v and isinstance(v, str):
            end_str = v
            break

    # SEMPRE remove as chaves de string única (não são do eContador)
    # Mesmo se já tinha campos separados, a string só polui o payload
    for k in CHAVES_ENDERECO_STRING:
        attrs.pop(k, None)

    if not end_str:
        return []

    # Já tem todos os campos separados? Não precisa parsear
    if all(attrs.get(k) for k in ("cep", "rua", "bairro", "cidade")):
        return []

    parsed = parsear_endereco_string(end_str)
    preenchidos = []
    for k, v in parsed.items():
        if v and not attrs.get(k):
            attrs[k] = v
            preenchidos.append(k)
    return preenchidos
