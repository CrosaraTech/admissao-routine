"""enrichment.py — preenche o payload JSON:API do candidato via fontes
externas, reduzindo dependência de OCR/Vision do Claude.

Fluxo (chamado em `processar_admissao` depois da resposta do Claude):

    payload_parcial (do Claude) → apply_fixed_defaults → enrich_from_cep →
    enrich_from_cpf → payload_enriquecido com `_enrich_meta`

Regra de ouro: NUNCA sobrescrever campo já preenchido. Enrichment só
preenche o que estava faltando.

Campos que CONTINUAM dependendo de OCR (não há API pública/barata):
  - identidade (RG): número, data emissão, órgão emissor
  - pis (NIS)
  - tituloeleitor, zonatituloeleitor, secaotituloeleitor
  - dataatestadoocupacional (ASO) + statusatestadoocupacional
  - cargo, salario, admissao, diascontratoexperiencia
  - cnpj_empresa (vem do contexto do email, não do candidato)
  - escolaridade (consulta do MEC não existe pública)
  - estadocivil (não há fonte oficial pública pra um CPF qualquer)

Campos derivados DETERMINISTICAMENTE (sem API externa):
  - ctps      = int(CPF[:7])        — regra do escritório
  - seriectps = CPF[7:11]
  - ufctps    = mesma UF da identidade (relationship)
  → Ver apply_ctps_from_cpf
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("admissao.enrich")

ROOT = Path(__file__).parent
LOOKUPS_FILE = ROOT / "lookups.json"

# Cache de UF→id em memória (lookups.json carregado uma vez)
_ESTADOS_CACHE: dict[str, str] | None = None

# Cache simples de ViaCEP em memória — CEPs raramente mudam
_VIACEP_CACHE: dict[str, dict] = {}
_VIACEP_TIMEOUT_S = 5.0


# ============================================================
# Helpers
# ============================================================

def _carregar_estados() -> dict[str, str]:
    """Carrega {UF: id} de lookups.json. Memoizado."""
    global _ESTADOS_CACHE
    if _ESTADOS_CACHE is not None:
        return _ESTADOS_CACHE
    try:
        with open(LOOKUPS_FILE, encoding="utf-8") as f:
            _ESTADOS_CACHE = (json.load(f).get("estados") or {})
    except Exception as e:
        log.warning(f"Falha lendo estados de lookups.json: {e}")
        _ESTADOS_CACHE = {}
    return _ESTADOS_CACHE


_RE_PONTUACAO_ENDERECO = re.compile(r"[,.;:/\\]+")


def _upper(s: Any) -> str | None:
    """UPPERCASE + strip + remove None/''. Retorna None se vazio."""
    if s is None:
        return None
    s = str(s).strip().upper()
    return s or None


def _upper_sem_pontuacao(s: Any) -> str | None:
    """UPPERCASE + remove pontuação (eContador rejeita em rua/bairro/cidade).
    Colapsa espaços. Mantém hífens e apóstrofos."""
    s = _upper(s)
    if not s:
        return None
    s = _RE_PONTUACAO_ENDERECO.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip() or None


def _so_digitos(s: Any) -> str:
    """Remove tudo que não for dígito."""
    return re.sub(r"\D", "", str(s or ""))


def _formatar_cep(cep: str) -> str | None:
    """Normaliza CEP pra formato '75250-000'. None se inválido (≠ 8 dígitos)."""
    d = _so_digitos(cep)
    if len(d) != 8:
        return None
    return f"{d[:5]}-{d[5:]}"


def _get_attr(payload: dict, key: str) -> Any:
    """Lê data.attributes.<key>, retornando None se ausente."""
    return ((payload.get("data") or {}).get("attributes") or {}).get(key)


def _set_attr(payload: dict, key: str, value: Any) -> None:
    """Escreve data.attributes.<key>. NÃO sobrescreve se já houver valor."""
    if value is None:
        return
    payload.setdefault("data", {}).setdefault("attributes", {}).setdefault(key, value)


def _get_rel_id(payload: dict, rel: str) -> str | None:
    """Lê data.relationships.<rel>.data.id, None se ausente."""
    rels = (payload.get("data") or {}).get("relationships") or {}
    return ((rels.get(rel) or {}).get("data") or {}).get("id")


def _set_rel_id(payload: dict, rel: str, type_: str, id_: str) -> None:
    """Escreve relationship JSON:API. NÃO sobrescreve se já existe."""
    if not id_:
        return
    rels = payload.setdefault("data", {}).setdefault("relationships", {})
    if rel in rels:
        # Verifica se já tem id válido
        atual = ((rels[rel].get("data") or {}).get("id"))
        if atual:
            return
    rels[rel] = {"data": {"type": type_, "id": str(id_)}}


# ============================================================
# TAREFA 1 — ViaCEP
# ============================================================

def _enrich_from_cep_brasilapi(cep_digits: str, cep_fmt: str) -> dict:
    """v2.16.41: fallback quando ViaCEP retorna erro. BrasilAPI agrega Correios
    Direto, OpenCEP e outros — pega CEPs que o ViaCEP isolado perde.
    Retorna dict no MESMO formato de enrich_from_cep (logo o caller não distingue).
    Vazio em qualquer falha.
    """
    url = f"https://brasilapi.com.br/api/cep/v2/{cep_digits}"
    try:
        with httpx.Client(timeout=_VIACEP_TIMEOUT_S) as client:
            r = client.get(url)
        if r.status_code != 200:
            log.info(f"   BrasilAPI: CEP {cep_fmt} também não encontrado "
                     f"(status {r.status_code}). CEP provavelmente foi "
                     f"digitado errado pelo cliente.")
            return {}
        data = r.json()
    except Exception as e:
        log.warning(f"   ⚠ BrasilAPI falhou pra {cep_fmt}: "
                    f"{type(e).__name__}: {e}")
        return {}

    estados = _carregar_estados()
    uf = (data.get("state") or "").strip().upper()
    resultado = {
        "rua": _upper_sem_pontuacao(data.get("street")),
        "bairro": _upper_sem_pontuacao(data.get("neighborhood")),
        "cidade": _upper_sem_pontuacao(data.get("city")),
        "cep": cep_fmt,
        "_estado_id": estados.get(uf),
        "_uf": uf or None,
    }
    resultado = {k: v for k, v in resultado.items() if v is not None}
    if resultado.get("rua") or resultado.get("cidade"):
        log.info(
            f"   BrasilAPI {cep_fmt} → {resultado.get('cidade', '?')}/{uf}, "
            f"{resultado.get('rua', '(sem logradouro)')}"
        )
    return resultado


def enrich_from_cep(cep: str) -> dict:
    """Consulta ViaCEP e retorna campos prontos pra mesclar no payload.

    Args:
        cep: string com 8 dígitos (com ou sem hífen).

    Returns:
        Dict com chaves que casam com attributes do payload eContador:
            {
              "rua":     "RUA XYZ",        # UPPERCASE
              "bairro":  "SETOR ABC",
              "cidade":  "GOIANIA",
              "cep":     "75250-000",      # normalizado
              "_estado_id": "9",            # pra alimentar relationship estado
              "_uf": "GO",                  # auxiliar
            }
        Dict vazio em qualquer falha (CEP inválido, ViaCEP fora do ar, etc.).
        NUNCA levanta exceção — pipeline não pode quebrar por enrichment.
    """
    cep_fmt = _formatar_cep(cep)
    if not cep_fmt:
        log.warning(f"   ⚠ CEP inválido (≠ 8 dígitos): {cep!r}")
        return {}

    # Cache hit
    if cep_fmt in _VIACEP_CACHE:
        return dict(_VIACEP_CACHE[cep_fmt])

    cep_digits = _so_digitos(cep_fmt)
    url = f"https://viacep.com.br/ws/{cep_digits}/json/"

    try:
        with httpx.Client(timeout=_VIACEP_TIMEOUT_S) as client:
            r = client.get(url)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.warning(f"   ⚠ ViaCEP falhou pra {cep_fmt}: {type(e).__name__}: {e}")
        return {}

    # ViaCEP retorna {"erro": true} (bool ou string) pra CEP que não existe.
    # v2.16.41: fallback BrasilAPI antes de desistir — agrega várias fontes
    # internamente e às vezes acha o que o ViaCEP perdeu.
    if data.get("erro"):
        log.info(f"   ViaCEP: CEP {cep_fmt} não encontrado — tentando BrasilAPI")
        fallback = _enrich_from_cep_brasilapi(cep_digits, cep_fmt)
        _VIACEP_CACHE[cep_fmt] = dict(fallback)  # cacheia mesmo se vazio
        return fallback

    estados = _carregar_estados()
    uf = (data.get("uf") or "").strip().upper()

    resultado = {
        # Endereço SEM pontuação — eContador rejeita vírgula/ponto/barra
        "rua": _upper_sem_pontuacao(data.get("logradouro")),
        "bairro": _upper_sem_pontuacao(data.get("bairro")),
        "cidade": _upper_sem_pontuacao(data.get("localidade")),
        "cep": cep_fmt,
        "_estado_id": estados.get(uf),
        "_uf": uf or None,
    }
    # Remove chaves None pra ficar limpo
    resultado = {k: v for k, v in resultado.items() if v is not None}

    _VIACEP_CACHE[cep_fmt] = dict(resultado)
    log.info(
        f"   ViaCEP {cep_fmt} → {resultado.get('cidade', '?')}/{uf}, "
        f"{resultado.get('rua', '(sem logradouro)')}"
    )
    return resultado


# ============================================================
# TAREFA 2 — Enriquecimento via CPF
# ============================================================
#
# ESTADO ATUAL: não há cliente de consulta CPF configurado no projeto.
# Provedores comuns no mercado brasileiro:
#   - Serpro CPF (consulta oficial — exige CNPJ corporativo + contrato)
#   - Konsi, Direct Data, CpfCheck, BrasilAPI (com fontes terceiras),
#     SintegraWS — cobram ~R$0,05-0,20 por consulta
#
# Pra habilitar, defina no .env:
#   CPF_LOOKUP_PROVIDER=konsi  (ou serpro/directdata/cpfcheck)
#   CPF_LOOKUP_TOKEN=<token do provedor>
#
# E implemente o cliente correspondente em `_lookup_cpf_<provider>`.

def enrich_from_cpf(cpf: str) -> dict:
    """Consulta serviço de CPF (se configurado) e retorna campos mapeados.

    Sem provedor configurado → retorna dict vazio (sem quebrar).

    Returns:
        {
          "nome":               "JOAO DA SILVA",     # UPPERCASE
          "nascimento":         "1990-03-12",        # ISO 8601
          "nomedamae":          "MARIA DA SILVA",
          "nomedopai":          "JOSE DA SILVA",     # opcional
          "municipionascimento":"GOIANIA",
          "_sexo_id":           "1",                  # 1=M, 2=F
          "_naturalidade_uf":   "GO",
          "_naturalidade_id":   "9",                  # relationship naturalidade
        }
    """
    cpf_digits = _so_digitos(cpf)
    if len(cpf_digits) != 11:
        log.warning(f"   ⚠ CPF inválido (≠ 11 dígitos): {cpf!r}")
        return {}

    provider = (os.getenv("CPF_LOOKUP_PROVIDER") or "").strip().lower()
    token = os.getenv("CPF_LOOKUP_TOKEN")

    if not provider:
        log.debug(
            "CPF lookup: sem provedor configurado "
            "(set CPF_LOOKUP_PROVIDER no .env pra habilitar)"
        )
        return {}

    if not token:
        log.warning(f"CPF lookup: provider '{provider}' configurado mas sem CPF_LOOKUP_TOKEN")
        return {}

    # TODO: implementar provedores específicos conforme contrato.
    # Por enquanto retorna vazio mas loga pra debug.
    log.warning(
        f"CPF lookup provider '{provider}' não implementado ainda. "
        "Stub retornando dict vazio. Implemente em "
        "_lookup_cpf_<provider> em enrichment.py"
    )
    return {}


# ============================================================
# TAREFA 3 — Defaults fixos
# ============================================================

# Valores fixos derivados do payload de produção (validado por admissões reais).
# Estes valores TAMBÉM aparecem em lookups.json:defaults_pipeline — mas esse
# arquivo é APENAS documentação histórica. O código não lê defaults de
# lookups.json (só UF→id em _carregar_estados). FONTE DE VERDADE = este dict.
# Se mudar algo aqui, atualize também briefing.md (system prompt do Claude).
FIXED_DEFAULTS_RELS = {
    "statusadmissao":            ("tipos-status-admissao", "1"),  # Análise (verde, desce direto)
    "tipoidentidade":            ("tipos-identidade", "1"),       # off-by-one → UI exibe "RG"
    "nacionalidade":             ("paises", "105"),                # Brasil
    "paisnascimento":            ("paises", "105"),
    "pais":                      ("paises", "105"),
    "tipovinculotrabalhista":    ("tipos-vinculos-trabalhista", "60"),  # CLT determinado urbano
    "categoriawdp":              ("tipos-categoria", "1"),
    # raca REMOVIDO daqui em v2.16.44 — virou FORCED_OVERRIDE (nunca respeita
    # extracao do Claude). Regra escritorio: SEMPRE Parda id=8, mesmo que doc
    # mencione outra cor. Ver FORCED_OVERRIDES_RELS abaixo.
    "tipoadmissao":              ("tipos-admissao", "1"),
    "formapagamento":            ("tipos-forma-de-pagamento", "4"),     # Mensal
    "tipoDeDeficiencia":         ("tipos-deficiencia", "0"),            # Não Possui
    "statusatestadoocupacional": ("tipos-status-atestado-ocupacional", "1"),  # Apto
    # Defaults do escritório — NUNCA sobrescrevem se Claude já extraiu valor real
    # (via _set_rel_id setdefault). Solteiro/Médio cobrem ~85% dos casos.
    "estadocivil":               ("tipos-estado-civil", "1"),     # Solteiro
    "escolaridade":              ("tipos-escolaridade", "7"),     # Médio completo
    # v2.16.60: naturalidade default Goiás (~99% dos candidatos sao GO/vizinhos).
    # Se cliente informar UF diferente, respeita (setdefault nao sobrescreve).
    "naturalidade":              ("estados", "9"),                # Goiás
}

# v2.16.44: overrides FORÇADOS — sobrescrevem MESMO que Claude tenha extraido
# valor do doc. Diferente de FIXED_DEFAULTS_RELS que so entra quando campo
# estava vazio. Regra escritorio: raca sempre Parda id=8, decisao DP em
# 2026-07-02.
FORCED_OVERRIDES_RELS = {
    "raca": ("tipos-raca", "8"),  # Parda — SEMPRE, ignora leitura do doc
}


FIXED_DEFAULTS_ATTRS = {
    "primeiroemprego":          False,
    "possuideficiencia":        False,
    "requersegurodesemprego":   False,
    "usuariocriacao":           "PIPELINE-V3",
}


def apply_ctps_from_cpf(payload: dict) -> dict:
    """Regra do escritório (lookups.json:regras_escritorio.ctps_do_cpf):
        ctps      = int(CPF[:7])
        seriectps = CPF[7:11]
        ufctps    = mesma UF da identidade (relationship)

    EXCEÇÃO À REGRA DE OURO: o CPF GANHA sempre contra o Claude.
    Se Claude extraiu CTPS diferente da derivada do CPF, SUBSTITUI pela
    derivada — padronização do escritório vence o que tá na carteira física.
    O operador ainda pode sobrescrever via dialog "Resolver pendência" depois,
    porque o `_aplicar_form_e_postar` não passa por esta função.

    Retorna (payload, mudou: bool) — `mudou` é True quando ctps/seriectps
    foi sobrescrito ou setado pela primeira vez.
    """
    cpf = _get_attr(payload, "cpf")
    if cpf is None or cpf == "":
        return payload, False

    # Valida ANTES do zfill — sem isso "abc" vira "00000000000" e gera CTPS=0.
    # Aceita 10-11 dígitos (CPF int pode ter perdido zero à esquerda).
    cpf_clean = re.sub(r"\D", "", str(cpf))
    if len(cpf_clean) < 10 or len(cpf_clean) > 11:
        log.warning(f"   ⚠ CPF com tamanho inválido pra derivar CTPS: {cpf!r}")
        return payload, False
    cpf_str = cpf_clean.zfill(11)

    ctps_derivado = int(cpf_str[:7])
    serie_derivada = cpf_str[7:11]

    attrs = payload.setdefault("data", {}).setdefault("attributes", {})
    antes_ctps = attrs.get("ctps")
    antes_serie = attrs.get("seriectps")

    # Força os valores (regra do CPF ganha sempre — exceto se vazios já bate)
    mudou = False
    if antes_ctps != ctps_derivado:
        attrs["ctps"] = ctps_derivado
        mudou = True
    if str(antes_serie or "") != serie_derivada:
        attrs["seriectps"] = serie_derivada
        mudou = True

    # ufctps = mesma UF da identidade — só preenche se ufctps ainda vazia
    # (essa parte segue regra de ouro: não sobrescreve)
    uf_id = _get_rel_id(payload, "ufidentidade")
    if uf_id:
        _set_rel_id(payload, "ufctps", "estados", uf_id)

    if mudou:
        if antes_ctps and antes_ctps != ctps_derivado:
            log.info(
                f"   ✨ CTPS sobrescrita pela regra do CPF: "
                f"{antes_ctps}/{antes_serie} → {ctps_derivado}/{serie_derivada}"
            )
        else:
            log.info(f"   ✨ CTPS derivada do CPF: {ctps_derivado}/{serie_derivada}")

    return payload, mudou


def _inferir_sexo_pelo_nome(nome: str) -> str | None:
    """v2.16.60: infere sexo id a partir do PRIMEIRO nome brasileiro.
    Retorna '1' (M), '2' (F) ou None se ambiguo.

    Heuristica conservadora:
      - Termina em -A -> F (Maria, Ana, Cristina, Fernanda)
      - Termina em -O -> M (Pedro, Marcelo, Bruno, Ricardo)
      - Termina em consoante -> M (Alexander, Israel, Rafael, Daniel)
      - Termina em -E -> ambiguo, cai em lista de excecoes
      - Termina em -I ou -U -> ambiguo, cai em lista

    Casos claros nao ambiguos cobrem ~95% dos nomes BR.
    """
    if not nome:
        return None
    import unicodedata as _ud
    import re as _re
    # Normaliza: tira acentos, upper, pega primeiro token
    s = _ud.normalize("NFD", str(nome).strip())
    s = "".join(c for c in s if not _ud.combining(c)).upper()
    partes = _re.split(r"\s+", s)
    if not partes:
        return None
    primeiro = partes[0].strip(".,-")
    if not primeiro or len(primeiro) < 2:
        return None

    # Excecoes conhecidas (nomes masculinos em -A, femininos em -O etc)
    _EXCECOES = {
        # M terminando em -A (raros)
        "COSTA": "1", "SILVA": "1",  # sobrenomes as primeiro nome
        # Nomes ambiguos ou em -E: F
        "DAIANE": "2", "ROSANE": "2", "ELIANE": "2", "ARIANE": "2",
        "LILIANE": "2", "MARIANE": "2", "CRISTIANE": "2", "SUZANE": "2",
        "SIMONE": "2", "JULIANE": "2", "ADRIANE": "2", "IONE": "2",
        # Nomes em -E: M
        "ANDRE": "1", "JORGE": "1", "FELIPE": "1", "ROBERT": "1",
        "ENRIQUE": "1", "ARIQUE": "1",
        # Em -I ambiguos
        "DANI": "2", "SAMI": "1", "YURI": "1", "GABI": "2",
    }
    if primeiro in _EXCECOES:
        return _EXCECOES[primeiro]

    ultima = primeiro[-1]
    if ultima == "A":
        return "2"  # F
    if ultima == "O":
        return "1"  # M
    if ultima not in ("E", "I", "U"):
        # Consoante -> M (Alexander, Rafael, Daniel, Israel)
        return "1"
    # Termina em E/I/U sem match -> ambiguo, deixa None (nao aplica)
    return None


def apply_fixed_defaults(payload: dict) -> dict:
    """Preenche os defaults fixos do escritório SEM consulta externa.

    NÃO sobrescreve campos já preenchidos (regra de ouro). Retorna o MESMO
    payload mutado (in-place + retorno pra encadeamento).
    """
    for attr, valor in FIXED_DEFAULTS_ATTRS.items():
        _set_attr(payload, attr, valor)

    for rel, (tipo, id_) in FIXED_DEFAULTS_RELS.items():
        _set_rel_id(payload, rel, tipo, id_)

    # v2.16.44: overrides forcados — sobrescrevem mesmo se ja existe
    rels = payload.setdefault("data", {}).setdefault("relationships", {})
    for rel, (tipo, id_) in FORCED_OVERRIDES_RELS.items():
        rels[rel] = {"data": {"type": tipo, "id": str(id_)}}

    # v2.16.60: se sexo ainda nao veio nem do Claude nem de FIXED_DEFAULTS,
    # tenta inferir do primeiro nome (Pedro=M, Maria=F, etc). Se conseguir,
    # aplica. Se nao (nome ambiguo tipo Alex, Yuri sem lista), deixa vazio
    # -> operador escolhe via select.
    if not _get_rel_id(payload, "sexo"):
        nome_attr = ((payload.get("data") or {}).get("attributes")
                     or {}).get("nome")
        sexo_inf = _inferir_sexo_pelo_nome(nome_attr or "")
        if sexo_inf:
            _set_rel_id(payload, "sexo", "tipos-sexo", sexo_inf)
            log.info(f"   🧠 sexo inferido do nome {nome_attr!r} -> id={sexo_inf}")

    return payload


# ============================================================
# TAREFA 4 — Orquestrador
# ============================================================

# Campos que precisam virem do OCR/Claude (nem CEP nem CPF preenchem).
# Usado pra calcular `fields_still_missing`.
CAMPOS_DEPENDEM_DE_OCR_ATTRS = (
    "identidade", "dataidentidade", "orgaoemissoridentidade",
    # ctps/seriectps NÃO listados — derivam do CPF via apply_ctps_from_cpf
    "pis",
    "tituloeleitor", "zonatituloeleitor", "secaotituloeleitor",
    "admissao", "salario", "nomecargo", "dataatestadoocupacional",
    "numero",  # número do endereço
)

CAMPOS_DEPENDEM_DE_OCR_RELS = (
    "empresa", "departamento", "funcao",
    # escolaridade/estadocivil NÃO listados — têm default fixo
    "ufidentidade",
    # ufctps NÃO listada — copia de ufidentidade via apply_ctps_from_cpf
    "sexo",  # se CPF lookup não configurado
)


# Cliente Direct Data — singleton lazy. Reusado entre chamadas de
# enrich_candidato pra que o cache de CPF persista por toda a sessão.
_DD_CLIENT = None


def _get_dd_client():
    """Lazy load do cliente Direct Data. Sem DIRECTDATA_TOKEN no .env,
    retorna cliente desabilitado (chamadas retornam {} sem custo).

    v2.14.1 (ITEM 9): lê flags de config.json (pis/titulo habilitados)
    diretamente do arquivo pra não criar dependência cíclica com main.
    Defaults: ambos False (sucesso < 30% no mês 06/2026).
    """
    global _DD_CLIENT
    if _DD_CLIENT is None:
        from directdata_client import DirectDataClient
        # Lê flags do config.json sem importar main (evita ciclo)
        pis_on = False
        titulo_on = False
        try:
            from pathlib import Path as _P
            import json as _json
            cfg_path = _P(__file__).parent / "config.json"
            if cfg_path.exists():
                cfg = _json.loads(cfg_path.read_text(encoding="utf-8"))
                pis_on = bool(cfg.get("directdata_pis_habilitado", False))
                titulo_on = bool(cfg.get("directdata_titulo_habilitado", False))
        except Exception:
            pass
        _DD_CLIENT = DirectDataClient(
            pis_habilitado=pis_on, titulo_habilitado=titulo_on,
        )
    return _DD_CLIENT


def _enrich_from_directdata(payload: dict, cpf: Any) -> list[str]:
    """Chama APIs Direct Data e mescla campos novos no payload. Retorna
    lista de campos preenchidos pra meta de auditoria.

    Skip por economia (não chama API que retornaria só campos já preenchidos):
      - Cadastro Básico: skip se já temos nome, nomedamae, nascimento E sexo E endereço completo
      - PIS: skip se já temos pis
      - TSE: skip se já temos tituloeleitor (e nomeMae/dataNasc não disponíveis)

    Custo máximo por admissão: R$ 1,24. Skips reduzem proporcionalmente.
    """
    client = _get_dd_client()
    if not client.habilitado:
        return []

    from directdata_mapper import (
        map_cadastro_basico,
        map_pis,
        map_titulo,
    )

    novos: list[str] = []
    attrs = (payload.get("data") or {}).get("attributes") or {}

    # --- API 1: Cadastro Básico ---
    tem_nome = bool(attrs.get("nome"))
    tem_mae = bool(attrs.get("nomedamae"))
    tem_nasc = bool(attrs.get("nascimento"))
    tem_sexo = bool(_get_rel_id(payload, "sexo"))
    tem_endereco_completo = all(
        attrs.get(c) for c in ("cep", "rua", "bairro", "cidade")
    ) and bool(_get_rel_id(payload, "estado"))

    nome_mae_api = ""
    data_nasc_br = ""

    if not (tem_nome and tem_mae and tem_nasc and tem_sexo and tem_endereco_completo):
        dados_basico = client.cadastro_basico(cpf)
        if dados_basico:
            campos = map_cadastro_basico(dados_basico)
            # Guarda info crua pra usar na API 4 (TSE precisa de nomeMae cru
            # e dataNascimento em formato BR)
            nome_mae_api = dados_basico.get("nomeMae", "") or ""
            data_nasc_api = str(dados_basico.get("dataNascimento") or "").split(" ")[0]
            if data_nasc_api:
                data_nasc_br = data_nasc_api

            for k, v in campos.items():
                if k.startswith("_"):
                    continue  # relationships processados abaixo
                if not attrs.get(k):  # respeita regra de ouro
                    _set_attr(payload, k, v)
                    novos.append(k)
            if campos.get("_sexo_id") and not _get_rel_id(payload, "sexo"):
                _set_rel_id(payload, "sexo", "tipos-sexo", campos["_sexo_id"])
                novos.append("sexo")
            if campos.get("_estado_id") and not _get_rel_id(payload, "estado"):
                _set_rel_id(payload, "estado", "estados", campos["_estado_id"])
                novos.append("estado")
    else:
        log.debug("DirectData skip cadastro_basico: payload já completo")

    # Reaproveita do que Claude/CEP extraiu pra alimentar a API 4
    if not nome_mae_api:
        nome_mae_api = attrs.get("nomedamae") or ""
    if not data_nasc_br:
        nasc_iso = attrs.get("nascimento")
        if nasc_iso and len(str(nasc_iso)) == 10:
            y, mo, d = str(nasc_iso).split("-")
            data_nasc_br = f"{d}/{mo}/{y}"

    # --- API 3: PIS ---
    if not attrs.get("pis"):
        dados_pis = client.pis(cpf)
        if dados_pis:
            campos = map_pis(dados_pis)
            if campos.get("pis"):
                _set_attr(payload, "pis", campos["pis"])
                novos.append("pis")
    else:
        log.debug("DirectData skip PIS: pis já presente")

    # --- API 4: TSE — só se tiver insumos e ainda não tiver título ---
    if not attrs.get("tituloeleitor") and nome_mae_api and data_nasc_br:
        dados_titulo = client.titulo_eleitor(cpf, nome_mae_api, data_nasc_br)
        if dados_titulo:
            campos = map_titulo(dados_titulo)
            for k in ("tituloeleitor", "zonatituloeleitor", "secaotituloeleitor"):
                if campos.get(k) and not attrs.get(k):
                    _set_attr(payload, k, campos[k])
                    novos.append(k)
    elif attrs.get("tituloeleitor"):
        log.debug("DirectData skip TSE: tituloeleitor já presente")
    else:
        log.debug("DirectData skip TSE: faltam nomeMae ou dataNascimento")

    return novos


def enrich_candidato(partial_payload: dict) -> dict:
    """Orquestrador: aplica defaults, ViaCEP, lookup CPF — nessa ordem.

    Args:
        partial_payload: payload JSON:API parcial vindo do Claude.
            Espera-se estrutura {"data": {"attributes": {...}, "relationships": {...}}}.
            Pode ter cep/cpf populados pelo Claude (extraídos dos docs).

    Returns:
        O MESMO payload (mutado) + chave raiz `_enrich_meta` com auditoria:
            {
              "fields_filled_by_cep": [...],
              "fields_filled_by_cpf": [...],
              "fields_still_missing": [...],
              "ocr_required": bool
            }
    """
    if not isinstance(partial_payload, dict):
        return partial_payload

    meta = {
        "fields_filled_by_cep": [],
        "fields_filled_by_cpf": [],
        "fields_still_missing": [],
        "ocr_required": False,
    }

    # 1. Defaults fixos
    apply_fixed_defaults(partial_payload)

    # 2. CEP
    cep = _get_attr(partial_payload, "cep")
    if cep:
        antes_cep = set((partial_payload.get("data") or {}).get("attributes", {}).keys())
        antes_rel_estado = _get_rel_id(partial_payload, "estado")

        # Capturar cidade que Claude leu ANTES do enrichment — usado pra
        # sanity check do CEP. Se Claude leu cidade X e ViaCEP retorna
        # cidade Y pro mesmo CEP, é forte indício que Claude errou o CEP.
        # Caso real: Pedro Henrique — Claude leu CEP 74912-420, ViaCEP confirmou
        # como Aparecida de Goiânia (que é a cidade certa), mas era endereço
        # de outro logradouro. Cidade bateu, então o sanity check NÃO pegou —
        # mas pega outros casos onde Claude troca dígito e cai em outra cidade.
        cidade_claude = (
            _get_attr(partial_payload, "cidade") or ""
        ).strip().upper()

        dados_cep = enrich_from_cep(cep)

        # Sanity check: cidade do CEP bate com cidade que Claude leu?
        cidade_viacep = (dados_cep.get("cidade") or "").strip().upper()
        cep_suspeito = bool(
            cidade_claude and cidade_viacep and cidade_claude != cidade_viacep
        )
        if cep_suspeito:
            log.warning(
                f"   ⚠ CEP suspeito: Claude leu cidade='{cidade_claude}' mas "
                f"CEP {cep} pertence a '{cidade_viacep}' (via ViaCEP). "
                f"NÃO sobrescrevendo dados originais — DP revisa endereço."
            )
            # Não sobrescreve nada — preserva o que Claude extraiu E sinaliza
            # nos metadados pra UI mostrar. Só preenche campos que Claude
            # deixou vazios (não há conflito nesses).
            attrs_atuais = (partial_payload.get("data") or {}).get("attributes") or {}
            for k in ("rua", "bairro", "cidade", "cep"):
                if dados_cep.get(k) and not attrs_atuais.get(k):
                    _set_attr(partial_payload, k, dados_cep[k])
            meta["cep_suspeito"] = {
                "cidade_claude": cidade_claude,
                "cidade_viacep": cidade_viacep,
                "cep": cep,
            }
        else:
            # Cidades batem (ou Claude não preencheu cidade) — comportamento normal
            for k in ("rua", "bairro", "cidade", "cep"):
                if dados_cep.get(k):
                    _set_attr(partial_payload, k, dados_cep[k])
        if dados_cep.get("_estado_id"):
            _set_rel_id(partial_payload, "estado", "estados", dados_cep["_estado_id"])

        depois_cep = set((partial_payload.get("data") or {}).get("attributes", {}).keys())
        novos = sorted(depois_cep - antes_cep)
        if not antes_rel_estado and _get_rel_id(partial_payload, "estado"):
            novos.append("estado")
        meta["fields_filled_by_cep"] = novos

    # 3. CPF
    cpf = _get_attr(partial_payload, "cpf")
    if cpf:
        antes_cpf = set((partial_payload.get("data") or {}).get("attributes", {}).keys())
        dados_cpf = enrich_from_cpf(cpf)
        for k in ("nome", "nascimento", "nomedamae", "nomedopai", "municipionascimento"):
            if dados_cpf.get(k):
                _set_attr(partial_payload, k, dados_cpf[k])
        if dados_cpf.get("_sexo_id"):
            _set_rel_id(partial_payload, "sexo", "tipos-sexo", dados_cpf["_sexo_id"])
        if dados_cpf.get("_naturalidade_id"):
            _set_rel_id(partial_payload, "naturalidade", "estados",
                        dados_cpf["_naturalidade_id"])

        depois_cpf = set((partial_payload.get("data") or {}).get("attributes", {}).keys())
        meta["fields_filled_by_cpf"] = sorted(depois_cpf - antes_cpf)

    # 3.5. CTPS derivada do CPF (regra do escritório — CPF ganha contra Claude)
    if cpf:
        _, ctps_mudou = apply_ctps_from_cpf(partial_payload)
        if ctps_mudou:
            for campo in ("ctps", "seriectps"):
                if campo not in meta["fields_filled_by_cpf"]:
                    meta["fields_filled_by_cpf"].append(campo)

    # 3.7. Direct Data — APIs pagas (R$ 1,24/admissão completa).
    # Skip por economia: se já temos TODOS os campos que uma API retornaria,
    # não chamamos. Cache em RAM evita 2ª chamada do mesmo CPF na sessão.
    if cpf:
        novos_dd = _enrich_from_directdata(partial_payload, cpf)
        if novos_dd:
            meta["fields_filled_by_cpf"].extend(
                c for c in novos_dd if c not in meta["fields_filled_by_cpf"]
            )
            meta["fields_filled_by_cpf"].sort()
            meta["fields_filled_by_cpf"].sort()

    # 4. Calcula fields_still_missing
    attrs = (partial_payload.get("data") or {}).get("attributes") or {}
    rels = (partial_payload.get("data") or {}).get("relationships") or {}
    faltando = []
    for campo in CAMPOS_DEPENDEM_DE_OCR_ATTRS:
        if campo not in attrs or attrs.get(campo) in (None, "", []):
            faltando.append(campo)
    for rel in CAMPOS_DEPENDEM_DE_OCR_RELS:
        if not _get_rel_id(partial_payload, rel):
            faltando.append(rel)
    meta["fields_still_missing"] = faltando
    meta["ocr_required"] = len(faltando) > 0

    partial_payload["_enrich_meta"] = meta

    log.info(
        f"   ✨ enrichment: +{len(meta['fields_filled_by_cep'])} via CEP, "
        f"+{len(meta['fields_filled_by_cpf'])} via CPF, "
        f"{len(faltando)} ainda dependem de OCR"
    )

    return partial_payload
