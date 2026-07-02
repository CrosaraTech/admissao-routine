"""salarios_padrao.py — cadastro de salário padrão por CNPJ + cargo (v2.12.0).

Quando um cliente recorrente manda "salário base" (sem valor numérico) E o
escritório sabe quanto esse cliente paga pra esse cargo, em vez de virar
pendência cliente, o pipeline usa o valor cadastrado.

Estrutura: `salarios_padrao.json`
```
{
  "10560396000185": {
    "_razao_social": "MODELOFARMA LTDA",
    "cargos": {
      "auxiliar de loja": {
        "salario": 1518.00,
        "criado_em": "2026-06-11T10:00:00",
        "fonte": "manual" | "auto",
        "ultima_atualizacao": "2026-06-12T15:30:00"
      },
      "estagio em supermercado": {
        "salario": 600.00,
        ...
      }
    }
  },
  "04584726000331": { ... }
}
```

3 caminhos de gravação:
  1. Manual via UI: operador resolve pendência, marca checkbox "salvar"
  2. Auto-aprendizado: admissão bem-sucedida com salário explícito
  3. Edição direta do JSON (operador power-user)

Match é por CNPJ exato + cargo normalizado (sem acento, lowercase).
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import datetime
from pathlib import Path

log = logging.getLogger("admissao.salarios_padrao")

SALARIOS_PADRAO_FILE = Path(__file__).parent / "salarios_padrao.json"


def _norm_cargo(cargo: str) -> str:
    """Normaliza cargo: lowercase, sem acentos, sem espaços duplos."""
    s = unicodedata.normalize("NFKD", cargo or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s.lower()).strip()


def _so_digitos(s: str) -> str:
    """Remove tudo que não é dígito (pra normalizar CNPJ)."""
    return re.sub(r"\D", "", str(s or ""))


# ============================================================
# Persistência
# ============================================================

def carregar() -> dict:
    """Lê todo o JSON. Retorna {} se não existe ou inválido."""
    if not SALARIOS_PADRAO_FILE.exists():
        return {}
    try:
        with SALARIOS_PADRAO_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"Falha lendo {SALARIOS_PADRAO_FILE}: {e}")
        return {}


def _salvar_estado(data: dict) -> None:
    try:
        with SALARIOS_PADRAO_FILE.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except OSError as e:
        log.warning(f"Falha salvando {SALARIOS_PADRAO_FILE}: {e}")


def salvar(
    cnpj: str,
    cargo: str,
    salario: float,
    razao_social: str = "",
    fonte: str = "manual",
) -> None:
    """Salva (ou atualiza) salário padrão pra (CNPJ + cargo). Idempotente.

    Args:
        cnpj: 14 dígitos (ou com formatação — só dígitos são guardados)
        cargo: nome do cargo (será normalizado)
        salario: valor em reais (float)
        razao_social: nome da empresa pra log/auditoria
        fonte: 'manual' (operador via UI) ou 'auto' (aprendizado em sucesso)
    """
    cnpj_d = _so_digitos(cnpj)
    if not cnpj_d or len(cnpj_d) != 14:
        log.warning(f"CNPJ inválido pra salvar salário padrão: {cnpj!r}")
        return
    cargo_norm = _norm_cargo(cargo or "")
    if not cargo_norm:
        log.warning(f"Cargo vazio pra salvar salário padrão (cnpj={cnpj_d})")
        return
    if not salario or salario <= 0:
        log.warning(f"Salário inválido pra salvar: {salario}")
        return

    data = carregar()
    empresa = data.setdefault(cnpj_d, {})
    if razao_social and not empresa.get("_razao_social"):
        empresa["_razao_social"] = razao_social.strip()
    cargos = empresa.setdefault("cargos", {})

    existente = cargos.get(cargo_norm)
    now = datetime.now().isoformat(timespec="seconds")
    if existente:
        # Update — só muda se valor mudou
        valor_antigo = existente.get("salario")
        if abs(float(valor_antigo) - float(salario)) < 0.01:
            return  # sem mudança real
        cargos[cargo_norm] = {
            "salario": float(salario),
            "criado_em": existente.get("criado_em", now),
            "ultima_atualizacao": now,
            "fonte": fonte,
            "valor_anterior": float(valor_antigo),
        }
        log.info(
            f"[salário padrão ATUALIZADO {fonte}] {cnpj_d} '{cargo}' "
            f"R$ {valor_antigo} → R$ {salario}"
        )
    else:
        cargos[cargo_norm] = {
            "salario": float(salario),
            "criado_em": now,
            "ultima_atualizacao": now,
            "fonte": fonte,
        }
        log.info(
            f"[salário padrão NOVO {fonte}] {cnpj_d} '{cargo}' R$ {salario}"
        )

    _salvar_estado(data)


def consultar(cnpj: str, cargo: str) -> dict | None:
    """Procura `{salario, criado_em, fonte}` por CNPJ + cargo.

    Retorna entry completa OU None se não tem cadastro.
    Match: CNPJ exato (só dígitos) + cargo normalizado.
    """
    cnpj_d = _so_digitos(cnpj)
    if not cnpj_d or len(cnpj_d) != 14:
        return None
    cargo_norm = _norm_cargo(cargo or "")
    if not cargo_norm:
        return None
    data = carregar()
    empresa = data.get(cnpj_d)
    if not empresa:
        return None
    cargos = empresa.get("cargos", {})
    return cargos.get(cargo_norm)


def consultar_valor(cnpj: str, cargo: str) -> float | None:
    """Atalho — retorna só o valor numérico ou None."""
    entry = consultar(cnpj, cargo)
    if entry and entry.get("salario"):
        return float(entry["salario"])
    return None
