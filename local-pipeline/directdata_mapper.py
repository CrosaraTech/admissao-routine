"""directdata_mapper.py — converte retornos da Direct Data pro formato
do payload eContador (attrs/rels JSON:API).

Regras críticas (lookups.json:regras_escritorio):
  - Strings → UPPERCASE
  - Datas → ISO 8601 (YYYY-MM-DD)
  - PIS → STRING preservando zeros à esquerda
  - numero → INTEGER (0 se ausente)
  - Endereço sem pontuação (, . ; : / \)

UF→ID lido de lookups.json (fonte de verdade do eContador) via
`enrichment._carregar_estados()` — não usar tabela hardcoded.
"""
from __future__ import annotations

import re
from typing import Any

from enrichment import _carregar_estados, _so_digitos

# Mapeamento sexo Direct Data → id eContador
SEXO_TO_ID = {
    "MASCULINO": "1",
    "FEMININO": "2",
}

# Pontuação que o eContador rejeita em campos de endereço
_RE_PONTUACAO_ENDERECO = re.compile(r"[,.;:/\\]+")


def limpar_texto(texto: Any) -> str | None:
    """UPPERCASE + remove pontuação proibida em endereço + colapsa espaços.
    Retorna None se vazio."""
    if not texto:
        return None
    s = str(texto).strip().upper()
    s = _RE_PONTUACAO_ENDERECO.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def upper(texto: Any) -> str | None:
    """UPPERCASE + strip. Retorna None se vazio."""
    if not texto:
        return None
    s = str(texto).strip().upper()
    return s or None


def parse_data_nascimento(data_str: Any) -> str | None:
    """Converte 'DD/MM/AAAA [HH:MM:SS]' → 'YYYY-MM-DD' (ISO 8601).
    Aceita também já no formato ISO. Retorna None em qualquer falha."""
    if not data_str:
        return None
    s = str(data_str).strip().split(" ")[0]  # corta hora se vier
    # Tenta DD/MM/AAAA primeiro
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", s)
    if m:
        d, mo, y = m.groups()
        return f"{y}-{mo}-{d}"
    # Tenta YYYY-MM-DD
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
    if m:
        return s
    return None


def map_cadastro_basico(retorno: dict) -> dict:
    """Mapeia retorno da API 1 (CadastroPessoaFisica) → campos do payload.

    Retorna dict {chave: valor} pronto pra mesclar via _set_attr/_set_rel_id.
    Chaves prefixadas com '_' são relationships (sufixo _id).

    Campos retornados:
        nome, nomedamae, nomedopai, nascimento, telefone, celular
        cep, rua, numero, complemento, bairro, cidade
        _sexo_id, _estado_id
    """
    if not isinstance(retorno, dict) or not retorno:
        return {}

    out: dict[str, Any] = {}

    nome = upper(retorno.get("nome"))
    if nome:
        out["nome"] = nome

    nomemae = upper(retorno.get("nomeMae"))
    if nomemae:
        out["nomedamae"] = nomemae

    nomepai = upper(retorno.get("nomePai"))
    if nomepai:
        out["nomedopai"] = nomepai

    nasc = parse_data_nascimento(retorno.get("dataNascimento"))
    if nasc:
        out["nascimento"] = nasc

    sexo = str(retorno.get("sexo") or "").strip().upper()
    if sexo in SEXO_TO_ID:
        out["_sexo_id"] = SEXO_TO_ID[sexo]

    # Endereço (pega o primeiro registro)
    enderecos = retorno.get("enderecos") or []
    if enderecos and isinstance(enderecos[0], dict):
        end = enderecos[0]
        if end.get("cep"):
            out["cep"] = str(end["cep"]).strip()
        rua = limpar_texto(end.get("logradouro"))
        if rua:
            out["rua"] = rua
        # numero como INTEGER (0 se ausente)
        num_raw = end.get("numero")
        try:
            num_int = int(_so_digitos(num_raw)) if num_raw else 0
        except (ValueError, TypeError):
            num_int = 0
        out["numero"] = num_int
        comp = limpar_texto(end.get("complemento"))
        if comp:
            out["complemento"] = comp
        bairro = limpar_texto(end.get("bairro"))
        if bairro:
            out["bairro"] = bairro
        cidade = upper(end.get("cidade"))  # cidade não tem pontuação tipicamente
        if cidade:
            out["cidade"] = cidade
        uf = str(end.get("uf") or "").strip().upper()
        if uf:
            estado_id = _carregar_estados().get(uf)
            if estado_id:
                out["_estado_id"] = estado_id

    # Contatos (pega primeiro telefone com DDD)
    telefones = retorno.get("telefones") or []
    for tel in telefones:
        if not isinstance(tel, dict):
            continue
        num = tel.get("telefoneComDDD") or tel.get("telefone")
        if not num:
            continue
        # Tipo: TELEFONE MÓVEL → celular; outros → telefone fixo
        tipo = str(tel.get("tipoTelefone") or "").upper()
        num_d = _so_digitos(num)
        if not num_d:
            continue
        if "MÓVEL" in tipo or "MOVEL" in tipo or "CELULAR" in tipo:
            if "celular" not in out:
                out["celular"] = num_d
        else:
            if "telefone" not in out:
                out["telefone"] = num_d

    # Email (pega o primeiro)
    emails = retorno.get("emails") or []
    for em in emails:
        if isinstance(em, dict) and em.get("enderecoEmail"):
            out["email"] = str(em["enderecoEmail"]).strip().lower()
            break

    return out


def map_pis(retorno: dict) -> dict:
    """Mapeia retorno da API 3 (MinisterioTrabalhoPIS) → {pis: STRING}.

    Remove pontuação preservando zeros à esquerda.
    Ex: '139.13012.98-1' → '13913012981'
    """
    if not isinstance(retorno, dict):
        return {}
    pis_raw = retorno.get("pis")
    if not pis_raw:
        return {}
    pis_clean = _so_digitos(pis_raw)
    if not pis_clean:
        return {}
    return {"pis": pis_clean}


def map_titulo(retorno: dict) -> dict:
    """Mapeia retorno da API 4 (TituloLocalVotacao) → campos de título.

    Retorna {} quando eleitor não cadastrado (inscricao=null) — NÃO marca
    como campo vazio. Pipeline trata isso como "informação não disponível"
    em vez de erro.

    Returns:
        {tituloeleitor, zonatituloeleitor, secaotituloeleitor}
    """
    if not isinstance(retorno, dict):
        return {}

    identificacao = retorno.get("identificacao") or {}
    domicilio = retorno.get("domicilioEleitoral") or {}

    inscricao = identificacao.get("inscricao")
    if not inscricao:
        return {}  # eleitor não cadastrado — não adiciona campos

    out: dict[str, Any] = {"tituloeleitor": str(inscricao)}
    zona = domicilio.get("zona")
    secao = domicilio.get("secao")
    if zona:
        out["zonatituloeleitor"] = str(zona)
    if secao:
        out["secaotituloeleitor"] = str(secao)
    return out
