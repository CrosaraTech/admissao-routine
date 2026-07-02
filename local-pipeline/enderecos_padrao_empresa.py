"""enderecos_padrao_empresa.py — cadastro de endereço padrão por CNPJ.

Mesmo padrão de `salarios_padrao.py`, mas pra endereço.

Caso de uso v2.16.27: fazendas/sítios onde os peões moram no alojamento da
propriedade — o endereço de RESIDÊNCIA dos funcionários é o endereço da
EMPRESA. Quando o Claude não consegue extrair endereço residencial do
documento (ou só vem o endereço da empresa no comprovante), em vez de
virar pendência cliente, o pipeline usa o endereço cadastrado aqui.

NÃO É PRA USAR EM EMPRESAS URBANAS comuns (transportadora, comércio,
indústria). Lá cada funcionário tem seu próprio endereço.

Estrutura: `enderecos_padrao_empresa.json`
```json
{
  "17833589000292": {
    "_razao_social": "MAMBORE AGROPECUARIA LTDA",
    "_observacao": "Endereço da fazenda — peões moram no alojamento",
    "endereco": {
      "cep": "76720-000",
      "rua": "EST CORREGO SECO",
      "numero": 0,
      "bairro": "ZONA RURAL",
      "cidade": "ARAGUAPAZ",
      "uf": "GO"
    },
    "criado_em": "2026-06-22T...",
    "ultima_atualizacao": "2026-06-22T...",
    "fonte": "manual"
  }
}
```
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

log = logging.getLogger("admissao.enderecos_padrao_empresa")

ENDERECOS_PADRAO_FILE = Path(__file__).parent / "enderecos_padrao_empresa.json"


def _so_digitos(s: str) -> str:
    return re.sub(r"\D", "", str(s or ""))


def carregar() -> dict:
    """Lê o JSON inteiro. {} se não existe ou inválido."""
    if not ENDERECOS_PADRAO_FILE.exists():
        return {}
    try:
        with ENDERECOS_PADRAO_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"Falha lendo {ENDERECOS_PADRAO_FILE}: {e}")
        return {}


def _salvar_estado(data: dict) -> None:
    try:
        with ENDERECOS_PADRAO_FILE.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except OSError as e:
        log.warning(f"Falha salvando {ENDERECOS_PADRAO_FILE}: {e}")


def salvar(
    cnpj: str,
    endereco: dict,
    razao_social: str = "",
    observacao: str = "",
    fonte: str = "manual",
) -> None:
    """Salva/atualiza endereço padrão pra um CNPJ. Idempotente.

    Args:
        cnpj: 14 dígitos (com ou sem pontuação)
        endereco: dict com pelo menos {cep, rua, bairro, cidade}. uf/numero opcionais
        razao_social: nome da empresa pra log/auditoria
        observacao: por que esse endereço é o default (ex: "fazenda c/ alojamento")
        fonte: 'manual' (operador via UI) ou 'auto' (aprendizado futuro)
    """
    cnpj_d = _so_digitos(cnpj)
    if not cnpj_d or len(cnpj_d) not in (11, 14):
        log.warning(f"CPF/CNPJ inválido pra salvar endereço padrão: {cnpj!r}")
        return
    if not isinstance(endereco, dict) or not any(endereco.values()):
        log.warning(f"Endereço vazio pra cnpj {cnpj_d!r}")
        return

    # Normaliza CEP (sempre com hífen)
    cep = _so_digitos(endereco.get("cep") or "")
    if len(cep) == 8:
        endereco["cep"] = f"{cep[:5]}-{cep[5:]}"

    # Upper case dos campos textuais
    for k in ("rua", "bairro", "cidade", "uf", "complemento"):
        if endereco.get(k) and isinstance(endereco[k], str):
            endereco[k] = endereco[k].upper().strip()

    # numero como int (0 = sem número)
    if "numero" in endereco:
        try:
            endereco["numero"] = int(endereco["numero"] or 0)
        except (TypeError, ValueError):
            endereco["numero"] = 0

    data = carregar()
    now = datetime.now().isoformat(timespec="seconds")
    existente = data.get(cnpj_d, {})

    novo = {
        "_razao_social": razao_social or existente.get("_razao_social", ""),
        "_observacao": observacao or existente.get("_observacao", ""),
        "endereco": endereco,
        "criado_em": existente.get("criado_em", now),
        "ultima_atualizacao": now,
        "fonte": fonte,
    }
    data[cnpj_d] = novo
    _salvar_estado(data)
    log.info(
        f"[endereço padrão {fonte}] {cnpj_d} ({razao_social[:30]}) → "
        f"{endereco.get('cidade', '?')}/{endereco.get('uf', '?')} "
        f"CEP {endereco.get('cep', '?')}"
    )


def consultar(cnpj: str) -> dict | None:
    """Retorna o dict {endereco, ...} cadastrado pra esse CNPJ, ou None."""
    cnpj_d = _so_digitos(cnpj)
    if not cnpj_d or len(cnpj_d) not in (11, 14):
        return None
    return carregar().get(cnpj_d)


def aplicar_em_attrs(attrs: dict, cnpj: str) -> list[str]:
    """Se o CNPJ tem endereço cadastrado E os campos de endereço em `attrs`
    estão vazios, preenche. NUNCA sobrescreve campo já preenchido.

    Retorna lista de campos preenchidos (pra log)."""
    cad = consultar(cnpj)
    if not cad:
        return []
    end_cad = cad.get("endereco") or {}
    preenchidos = []
    for k_form, k_payload in [
        ("rua", "rua"), ("numero", "numero"), ("complemento", "complemento"),
        ("bairro", "bairro"), ("cidade", "cidade"), ("uf", "estado"),
        ("cep", "cep"),
    ]:
        val_cad = end_cad.get(k_form)
        if val_cad in (None, "") and val_cad != 0:
            continue
        if attrs.get(k_payload) not in (None, "", 0) and k_form != "numero":
            continue
        attrs[k_payload] = val_cad
        preenchidos.append(f"{k_payload}={val_cad!r}")
    return preenchidos


def remover(cnpj: str) -> bool:
    """Remove cadastro. Retorna True se removeu."""
    cnpj_d = _so_digitos(cnpj)
    data = carregar()
    if cnpj_d in data:
        data.pop(cnpj_d)
        _salvar_estado(data)
        log.info(f"[endereço padrão removido] {cnpj_d}")
        return True
    return False


def listar() -> list[dict]:
    """Lista todos os cadastros pra UI."""
    out = []
    for cnpj, rec in carregar().items():
        out.append({"cnpj": cnpj, **rec})
    out.sort(key=lambda r: r.get("_razao_social", "") or r.get("cnpj", ""))
    return out
