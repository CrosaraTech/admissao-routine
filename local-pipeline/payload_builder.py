"""Payload builder — injeta IDs resolvidos e faz sanitização final.

O Claude já devolve quase tudo pronto seguindo o briefing. Este módulo:
  1. Substitui os IDs placeholder de empresa/departamento/funcao
  2. Garante que `data.type = "candidatos"`
  3. Aplica regras de segurança: CPF como int, numero=0 se ausente, etc.
  4. Remove campos top-level extras (cnpj_empresa, departamento_sugerido,
     _pendente, etc.) que ficam no envelope mas não no payload final
"""

from __future__ import annotations

import logging
import re
from typing import Any


log = logging.getLogger("admissao.payload")


CHAVES_TOPO_PARA_REMOVER = {
    "cnpj_empresa", "departamento_sugerido", "cbo_sugerido", "cargo_extraido",
    "_pendente", "_motivo", "_dados_parciais",
}


# Campos que SEMPRE precisam ser preenchidos manualmente no Alterdata Desktop
# (limitações de produto + bugs do sync E-plugin). Referência: briefing.md
# seção 9 e lookups.json:campos_faltando_no_payload.
CAMPOS_MANUAIS_DP = [
    "Matrícula eSocial",
    "Categoria eSocial (default: 101 Empregado)",
    "Natureza da atividade (default: Trabalhador urbano)",
    "Tipo de jornada",
    "Regime de Jornada (Horário de Trabalho)",
    "Horas semanais (default: 44)",
    "Horário (código — varia por empresa)",
    "Tipo de salário contratual (Mensal + data=admissão)",
    "Adiantamento (☑ marcar)",
    "Não atualiza salário (☑ marcar)",
    "Dias para prorrogação (default: 60)",
    "FGTS (Conta, Data opção=admissão, UF, Saldo)",
    "Tipo de Identidade (bug sync — chega vazio mesmo com id=1)",
    "Cor/Raça (bug off-by-one — confirmar no Desktop)",
]


# Campos obrigatórios pra subir a admissão (lista do eContador, captura tela).
# Faltando qualquer um → pendência (DP completa manual e o e-mail vira pendente).
#
# `numero` é NÃO obrigatório por design — briefing diz que 0 significa "sem número"
# e a API exige Integer (não aceita "SN" string).
ATTRS_OBRIGATORIOS = [
    "nome",
    "cpf",
    "admissao",
    "nascimento",
    "nomedamae",
    "municipionascimento",
    "cep",
    "rua",
    "bairro",
    "cidade",
    "diascontratoexperiencia",
    "primeiroemprego",
    "salario",
]

RELS_OBRIGATORIOS = [
    "empresa",
    "departamento",
    "funcao",
    "estadocivil",
    "sexo",
    "raca",  # Cor
    "escolaridade",
    "naturalidade",
    "paisnascimento",
    "estado",  # estado do endereço
    "tipoadmissao",
    "categoriawdp",  # Categoria
    "formapagamento",
]

LABELS_AMIGAVEIS = {
    "nome": "Nome",
    "cpf": "CPF",
    "admissao": "Data de Admissão",
    "nascimento": "Data de nascimento",
    "nomedamae": "Nome da Mãe",
    "municipionascimento": "Município de nascimento",
    "cep": "CEP",
    "rua": "Rua",
    "bairro": "Bairro",
    "cidade": "Cidade",
    "diascontratoexperiencia": "Quantidade de dias do contrato de experiência",
    "primeiroemprego": "Primeiro Emprego",
    "salario": "Salário Base",
    "empresa": "Empresa",
    "departamento": "Departamento",
    "funcao": "Função",
    "estadocivil": "Estado Civil",
    "sexo": "Sexo",
    "raca": "Cor",
    "escolaridade": "Escolaridade",
    "naturalidade": "Naturalidade",
    "paisnascimento": "País de Nascimento",
    "estado": "Estado (endereço)",
    "tipoadmissao": "Tipo de Admissão",
    "categoriawdp": "Categoria",
    "formapagamento": "Forma de Pagamento",
}


def validar_campos_obrigatorios(payload: dict) -> list[str]:
    """Verifica os campos exigidos pelo eContador. Retorna labels faltantes.

    Lista vazia → pode postar. Lista não-vazia → pendência.

    Regras especiais:
      - `primeiroemprego` é bool: só precisa existir (True ou False valem)
      - `salario` precisa ser > 0
      - `diascontratoexperiencia` precisa ser > 0
      - relationships: precisa ter `.data.id` não-vazio
    """
    faltando: list[str] = []
    data = payload.get("data") or {}
    attrs = data.get("attributes") or {}
    rels = data.get("relationships") or {}

    for k in ATTRS_OBRIGATORIOS:
        v = attrs.get(k)
        if k == "primeiroemprego":
            if v is None:
                faltando.append(LABELS_AMIGAVEIS.get(k, k))
            continue
        if k in ("salario", "diascontratoexperiencia"):
            try:
                if v is None or float(v) <= 0:
                    faltando.append(LABELS_AMIGAVEIS.get(k, k))
            except (TypeError, ValueError):
                faltando.append(LABELS_AMIGAVEIS.get(k, k))
            continue
        if v in (None, "", [], {}):
            faltando.append(LABELS_AMIGAVEIS.get(k, k))

    for k in RELS_OBRIGATORIOS:
        rel = rels.get(k) or {}
        rel_data = rel.get("data") or {}
        rid = rel_data.get("id")
        if not rid or str(rid).strip() in ("", "0", "None"):
            faltando.append(LABELS_AMIGAVEIS.get(k, k))

    return faltando


def _so_digitos(s: Any) -> str:
    return re.sub(r"\D", "", str(s or ""))


def _ensure_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    s = _so_digitos(v)
    return int(s) if s else None


def sanitizar_attributes(attrs: dict) -> dict:
    """Aplica regras críticas de payload (CPF int, numero=0, etc.)."""
    out = dict(attrs)

    # CPF como inteiro (Java rejeita string)
    if "cpf" in out:
        cpf = _ensure_int(out["cpf"])
        if cpf is not None:
            out["cpf"] = cpf
        else:
            out.pop("cpf", None)

    # numero do endereço como int (0 se ausente — briefing seção 3.2)
    if "numero" in out:
        num = _ensure_int(out["numero"])
        out["numero"] = num if num is not None else 0
    else:
        # Se tem rua/cep mas não tem numero, força 0
        if any(k in out for k in ("rua", "cep")):
            out["numero"] = 0

    # ctps como int
    if "ctps" in out:
        ctps = _ensure_int(out["ctps"])
        if ctps is not None:
            out["ctps"] = ctps
        else:
            out.pop("ctps", None)

    # email em lowercase
    if isinstance(out.get("email"), str):
        out["email"] = out["email"].strip().lower()

    # Remove None/"" — bug 9: datas null viram 30/12/1899 no Desktop
    return {k: v for k, v in out.items() if v not in (None, "", [], {})}


def finalizar_payload(
    payload_claude: dict,
    empresa_id: str,
    departamento_id: str | None,
    funcao_id: str,
) -> dict:
    """Recebe o JSON do Claude e produz o payload final pra POST /candidatos.

    `payload_claude` pode ter formato:
      {"cnpj_empresa": "...", "departamento_sugerido": "...",
       "data": {"type": "candidatos", "attributes": {...}, "relationships": {...}}}
    """
    if "data" not in payload_claude:
        raise ValueError("Payload do Claude não tem chave 'data' no nível raiz")

    data = dict(payload_claude["data"])
    data["type"] = "candidatos"

    # Attributes
    attrs = sanitizar_attributes(dict(data.get("attributes") or {}))
    data["attributes"] = attrs

    # Relationships — substitui IDs resolvidos
    rels = dict(data.get("relationships") or {})
    rels["empresa"] = {"data": {"type": "empresas", "id": str(empresa_id)}}
    rels["funcao"] = {"data": {"type": "funcoes", "id": str(funcao_id)}}
    if departamento_id:
        rels["departamento"] = {
            "data": {"type": "departamentos", "id": str(departamento_id)}
        }
    else:
        rels.pop("departamento", None)
        log.warning("Payload sem departamento — DP precisa preencher manual no Desktop")

    data["relationships"] = rels

    return {"data": data}


def extrair_dados_consulta(payload_claude: dict) -> dict:
    """Extrai os campos top-level que o pipeline usa pra resolver IDs.

    Retorna: {cnpj_empresa, departamento_sugerido, cargo, cbo}
    """
    cnpj = _so_digitos(payload_claude.get("cnpj_empresa"))
    if not cnpj:
        # Fallback: alguns formatos podem trazer dentro de attributes
        attrs = (payload_claude.get("data") or {}).get("attributes") or {}
        cnpj = _so_digitos(attrs.get("cnpj_empresa") or attrs.get("cnpj"))

    cargo = (
        payload_claude.get("cargo_extraido")
        or (payload_claude.get("data") or {}).get("attributes", {}).get("nomecargo")
    )

    return {
        "cnpj_empresa": cnpj,
        "departamento_sugerido": payload_claude.get("departamento_sugerido"),
        "cargo": cargo,
        "cbo": _so_digitos(payload_claude.get("cbo_sugerido")),
        "pendente": bool(payload_claude.get("_pendente")),
        "motivo_pendencia": payload_claude.get("_motivo"),
    }
