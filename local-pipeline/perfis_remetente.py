"""perfis_remetente.py — memória do sistema sobre cada cliente (v2.16.0).

Mantém um perfil agregado por REMETENTE de email (ex: rh@empresa.com):
  - Quais CNPJs ele já usou (uma mesma pessoa pode mandar de múltiplas filiais)
  - Histórico: n_processadas, n_pendencias, tempos médios
  - Cargos que costuma admitir (com salário e funcao_id frequentes)
  - Padrões aprendidos: campos que ele sempre omite nos últimos 5 emails
  - Observações livres do operador (campo manual)

Auto-consolida lendo:
  - payloads/*.json (cada um tem remetente + resolucao + resultado)
  - remetente_aliases.json (mapeamento confirmado)
  - salarios_padrao.json (salários cadastrados)
  - funcao_aliases.json (funções aprendidas)

Salva em perfis_remetente.json (lazy: só popula quando alguém chama
`consolidar_todos()`). Operador pode forçar refresh pelo botão na web.

NÃO salva PII bruta (CPF, nomes completos) no perfil — só agregados.
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger("admissao.perfis")

_DIR = Path(__file__).parent
PERFIS_FILE = _DIR / "perfis_remetente.json"
PAYLOADS_DIR = _DIR / "payloads"

# Quantas últimas admissões olhar pra detectar "omissão habitual"
JANELA_OMISSAO = 5
# Campos que valem a pena detectar omissão (atalho pra padrões repetitivos)
CAMPOS_TRACKEAVEIS = [
    "salario", "admissao", "dataatestadoocupacional",
    "pis", "datapis",
    "celular", "telefone", "email",
    "banco", "agencia", "conta",
    "nomedopai",
    "nomecargo",
]


# ── helpers ─────────────────────────────────────────────────────

def _so_digitos(s: Any) -> str:
    return re.sub(r"\D", "", str(s or ""))


def _normalizar_email(email: str) -> str:
    """Lowercase + strip. Não tenta resolver alias — confia no que está salvo."""
    return (email or "").strip().lower()


def _extrair_email(remetente: str) -> str:
    """De 'Nome <email@dominio>' tira só 'email@dominio'. Se já vier limpo, devolve."""
    if not remetente:
        return ""
    m = re.search(r"<([^>]+)>", remetente)
    if m:
        return _normalizar_email(m.group(1))
    return _normalizar_email(remetente)


# ── persistência ────────────────────────────────────────────────

def carregar() -> dict:
    """Lê todo o JSON dos perfis. {} se não existe."""
    if not PERFIS_FILE.exists():
        return {}
    try:
        data = json.loads(PERFIS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as e:
        log.warning(f"Falha lendo {PERFIS_FILE}: {e}")
        return {}


def salvar(perfis: dict) -> None:
    """Salvar atômico via temp + replace."""
    import os
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=str(PERFIS_FILE.parent),
                                prefix=PERFIS_FILE.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(perfis, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, PERFIS_FILE)
    except OSError as e:
        log.warning(f"Falha salvando {PERFIS_FILE}: {e}")
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ── consolidação ────────────────────────────────────────────────

def _coletar_eventos_por_remetente() -> dict[str, list[dict]]:
    """Vasculha payloads/*.json e agrupa por remetente normalizado.

    Cada evento = dict com timestamp, msg_id, cnpj, razao_social, cargo,
    salario, attrs_presentes (chaves não-vazias), status (sucesso/pendente).
    """
    if not PAYLOADS_DIR.exists():
        return {}
    eventos: dict[str, list[dict]] = defaultdict(list)
    for arq in sorted(PAYLOADS_DIR.glob("*.json")):
        try:
            doc = json.loads(arq.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rem = _extrair_email(doc.get("remetente") or "")
        if not rem:
            continue
        attrs = ((doc.get("payload") or {}).get("data") or {}).get("attributes") or {}
        resol = doc.get("resolucao") or {}
        resultado = doc.get("resultado") or {}
        status = str(resultado.get("status") or "")
        attrs_presentes = {k for k, v in attrs.items()
                           if v not in (None, "", 0, [], {})}
        # v2.16.20: tolerar salário string ("NAO INFORMADO", "SALARIO BASE",
        # "NAO INFORMADO — aplicado mínimo R$ 1.621,00 provisoriamente").
        # Antes: float() explodia → consolidar_perfis quebrava na passada.
        sal_raw = attrs.get("salario")
        try:
            sal_num = float(sal_raw) if sal_raw not in (None, "") else 0.0
        except (TypeError, ValueError):
            sal_num = 0.0
        eventos[rem].append({
            "ts": doc.get("timestamp") or "",
            "msg_id": doc.get("msg_id") or "",
            "cnpj": _so_digitos(resol.get("cnpj_empresa")),
            "razao_social": str(resol.get("razao_social") or ""),
            "cargo": str(resol.get("cargo_extraido") or attrs.get("nomecargo") or ""),
            "salario": sal_num if sal_num > 0 else None,
            "funcao_id": str(resol.get("funcao_id") or ""),
            "attrs_presentes": list(attrs_presentes),
            "status": status,
            "ok": status == "sucesso",
            "candidato_id": resultado.get("candidato_id"),
        })
    return dict(eventos)


def _detectar_omissoes_habituais(eventos: list[dict]) -> list[str]:
    """Olha as últimas JANELA_OMISSAO admissões e marca como 'omissão habitual'
    os campos que estão AUSENTES em TODAS elas."""
    if len(eventos) < 2:
        return []
    janela = sorted(eventos, key=lambda e: e["ts"], reverse=True)[:JANELA_OMISSAO]
    # Pra cada campo, conta quantas admissões NÃO o têm
    omissoes: list[str] = []
    for campo in CAMPOS_TRACKEAVEIS:
        ausente_em_todas = all(campo not in (e["attrs_presentes"] or []) for e in janela)
        if ausente_em_todas:
            omissoes.append(campo)
    return omissoes


def _consolidar_um(eventos: list[dict],
                    perfil_anterior: dict | None = None) -> dict:
    """Calcula stats agregados pra um remetente a partir da lista de eventos."""
    if not eventos:
        return perfil_anterior or {}

    eventos_ord = sorted(eventos, key=lambda e: e["ts"])
    n_ok = sum(1 for e in eventos if e["ok"])
    n_total = len(eventos)
    n_pend = n_total - n_ok

    # Agrupa CNPJs com contagem + razão social
    cnpj_counter: Counter = Counter()
    razao_por_cnpj: dict[str, str] = {}
    for e in eventos:
        if e["cnpj"]:
            cnpj_counter[e["cnpj"]] += 1
            if e["razao_social"]:
                razao_por_cnpj[e["cnpj"]] = e["razao_social"]

    cnpjs = [
        {
            "cnpj": cnpj,
            "razao_social": razao_por_cnpj.get(cnpj, "?"),
            "n_admissoes": n,
        }
        for cnpj, n in cnpj_counter.most_common()
    ]

    # Cargos frequentes (com salário/funcao_id mais comum)
    cargos: dict[str, dict] = {}
    for e in eventos:
        c = (e["cargo"] or "").upper().strip()
        if not c:
            continue
        rec = cargos.setdefault(c, {
            "n_vezes": 0,
            "salarios": [],
            "funcoes_ids": Counter(),
        })
        rec["n_vezes"] += 1
        if e["salario"]:
            rec["salarios"].append(e["salario"])
        if e["funcao_id"]:
            rec["funcoes_ids"][e["funcao_id"]] += 1
    cargos_resumo = {}
    for c, rec in cargos.items():
        sal = rec["salarios"]
        cargos_resumo[c] = {
            "n_vezes": rec["n_vezes"],
            "salario_padrao": round(sum(sal) / len(sal), 2) if sal else None,
            "funcao_id_frequente": (
                rec["funcoes_ids"].most_common(1)[0][0]
                if rec["funcoes_ids"] else None
            ),
        }

    # Preserva campos editáveis manualmente — não derivados de payloads
    observacoes = ""
    nome_apresentacao = ""
    defaults_ausente: dict = {}
    salarios_manuais: dict = {}
    if perfil_anterior:
        observacoes = perfil_anterior.get("observacoes_operador", "")
        nome_apresentacao = perfil_anterior.get("nome_apresentacao", "")
        defaults_ausente = perfil_anterior.get("defaults_quando_ausente", {}) or {}
        # v2.16.39: salário cadastrado manualmente por cargo (sobrescreve média)
        salarios_manuais = perfil_anterior.get("salarios_manuais_por_cargo", {}) or {}

    return {
        "nome_apresentacao": nome_apresentacao,
        "cnpjs": cnpjs,
        "estatisticas": {
            "primeira_admissao": eventos_ord[0]["ts"],
            "ultima_admissao": eventos_ord[-1]["ts"],
            "n_processadas": n_ok,
            "n_pendencias": n_pend,
            "n_total": n_total,
        },
        "cargos_frequentes": cargos_resumo,
        "padroes_aprendidos": {
            "omissoes_habituais": _detectar_omissoes_habituais(eventos),
        },
        "observacoes_operador": observacoes,
        # v2.16.4: defaults cadastrados pelo operador quando o cliente
        # não envia certos documentos (ex.: fazenda que não manda comprovante
        # de endereço — usa endereço da própria propriedade).
        "defaults_quando_ausente": defaults_ausente,
        # v2.16.39: cargo → salário fixo definido pelo operador. Quando o
        # remetente omite salário (ex.: "CONFIRMAR COM O DA MESMA FUNÇÃO"),
        # este valor entra direto, sem depender de histórico/média.
        "salarios_manuais_por_cargo": salarios_manuais,
        "atualizado_em": datetime.now().isoformat(timespec="seconds"),
    }


def consolidar_todos() -> dict:
    """Refaz todos os perfis a partir do disco. Preserva observações
    livres do operador. Salva no JSON e retorna o dict completo."""
    log.info("[perfis] Consolidando todos os remetentes...")
    perfis_antigos = carregar()
    eventos_por_rem = _coletar_eventos_por_remetente()
    perfis_novos: dict = {}
    for rem, evs in eventos_por_rem.items():
        perfis_novos[rem] = _consolidar_um(evs, perfis_antigos.get(rem))
    # Preserva perfis que existem no antigo mas não têm payloads recentes
    # (ex: cliente que parou de mandar — não quero apagar suas observações)
    for rem, perf in perfis_antigos.items():
        if rem not in perfis_novos:
            perf["_orfao"] = True  # marca pra UI
            perfis_novos[rem] = perf
    salvar(perfis_novos)
    log.info(f"[perfis] Consolidados {len(perfis_novos)} perfis")
    return perfis_novos


def perfil_de(remetente: str, consolidar_se_faltar: bool = True) -> dict:
    """Retorna o perfil de UM remetente. Se não existe e consolidar_se_faltar,
    refaz tudo (custoso — chamar pontualmente)."""
    rem_norm = _extrair_email(remetente)
    perfis = carregar()
    if rem_norm not in perfis and consolidar_se_faltar:
        perfis = consolidar_todos()
    return perfis.get(rem_norm, {})


def listar_resumido() -> list[dict]:
    """Lista de remetentes com info mínima — pra renderizar a tabela /perfis."""
    perfis = carregar()
    out = []
    for rem, p in perfis.items():
        stats = p.get("estatisticas") or {}
        out.append({
            "remetente": rem,
            "nome_apresentacao": p.get("nome_apresentacao") or rem,
            "n_cnpjs": len(p.get("cnpjs") or []),
            "n_total": stats.get("n_total", 0),
            "n_processadas": stats.get("n_processadas", 0),
            "n_pendencias": stats.get("n_pendencias", 0),
            "ultima_admissao": stats.get("ultima_admissao", ""),
            "n_omissoes": len((p.get("padroes_aprendidos") or {}).get("omissoes_habituais") or []),
            "tem_observacoes": bool(p.get("observacoes_operador")),
            "orfao": bool(p.get("_orfao")),
        })
    # Ordena por volume DESC
    out.sort(key=lambda x: (-x["n_total"], x["remetente"]))
    return out


def atualizar_observacoes(remetente: str, observacoes: str,
                          nome_apresentacao: str = "") -> bool:
    """Atualiza o campo livre de observações do operador.
    Retorna True se gravou."""
    rem_norm = _extrair_email(remetente)
    if not rem_norm:
        return False
    perfis = carregar()
    perf = perfis.setdefault(rem_norm, {})
    perf["observacoes_operador"] = (observacoes or "").strip()
    if nome_apresentacao:
        perf["nome_apresentacao"] = nome_apresentacao.strip()
    perf["atualizado_em"] = datetime.now().isoformat(timespec="seconds")
    salvar(perfis)
    log.info(f"[perfis] Observações de {rem_norm} atualizadas")
    return True


# ── Fase 2: resumo pra injetar no prompt do Claude ──────────────

def _fmt_doc(d: str) -> str:
    """v2.16.4: formata 11 dig como CPF e 14 dig como CNPJ. Senão devolve cru."""
    d = re.sub(r"\D", "", str(d or ""))
    if len(d) == 14:
        return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"
    if len(d) == 11:
        return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"
    return d


def resumo_pra_prompt(remetente: str) -> str:
    """Retorna texto curto pra colocar no prompt do Claude — dá contexto
    sobre o remetente. Vazio se o remetente é desconhecido.

    v2.16.4: passou a:
      - Chamar contratantes de "Contratantes" (e não "CNPJs") porque
        eContador aceita CPF como contratante.
      - Mencionar omissões garantidas + endereço padrão cadastrado.
    """
    perf = perfil_de(remetente, consolidar_se_faltar=False)
    if not perf:
        return ""
    # v2.16.4: aceita perfil sem CNPJs ainda contanto que tenha algo útil
    # (defaults manuais cadastrados antes da primeira admissão bem-sucedida).
    tem_conteudo = (
        perf.get("cnpjs")
        or perf.get("defaults_quando_ausente")
        or perf.get("observacoes_operador")
    )
    if not tem_conteudo:
        return ""
    stats = perf.get("estatisticas") or {}
    omissoes = (perf.get("padroes_aprendidos") or {}).get("omissoes_habituais") or []
    cargos = perf.get("cargos_frequentes") or {}
    obs = perf.get("observacoes_operador") or ""
    defaults = perf.get("defaults_quando_ausente") or {}

    linhas = [
        f"## CONTEXTO DO REMETENTE",
        f"Remetente: {perf.get('nome_apresentacao') or remetente}",
    ]
    if stats.get("n_total"):
        linhas.append(
            f"Histórico: {stats.get('n_processadas', 0)} admissões cadastradas, "
            f"{stats.get('n_pendencias', 0)} pendências."
        )
    cnpjs = perf.get("cnpjs") or []
    if cnpjs:
        cnpjs_str = ", ".join(
            f"{_fmt_doc(c['cnpj'])} ({c.get('razao_social', '?')[:30]})"
            for c in cnpjs[:3]
        )
        linhas.append(f"Contratantes que esse remetente já usou: {cnpjs_str}")
    if cargos:
        cargos_top = sorted(cargos.items(), key=lambda x: -x[1]["n_vezes"])[:3]
        cargos_str = ", ".join(
            f"{c} ({d['n_vezes']}x"
            + (f", R$ {d['salario_padrao']:.2f}" if d.get("salario_padrao") else "")
            + ")"
            for c, d in cargos_top
        )
        linhas.append(f"Cargos frequentes: {cargos_str}")
    if omissoes:
        linhas.append(
            f"⚠ Padrão histórico: esse remetente costuma OMITIR estes campos: "
            f"{', '.join(omissoes)}. "
            f"Procure mesmo assim — pode estar em algum anexo."
        )
    # v2.16.4: defaults cadastrados pelo operador (mais forte que padrão histórico)
    end_def = defaults.get("endereco") or {}
    if end_def:
        partes = [f"{k}={v}" for k, v in end_def.items()
                  if v not in (None, "") and not k.startswith("_")]
        if partes:
            linhas.append(
                f"📌 ENDEREÇO PADRÃO cadastrado pra este remetente "
                f"(usar quando o comprovante de endereço não vier): "
                f"{'; '.join(partes)}."
            )
    if defaults.get("omitir_aso"):
        linhas.append(
            f"📌 Este remetente NÃO costuma enviar ASO. "
            f"Não marque a admissão como pendente por causa disso — "
            f"DP completa a data depois manualmente."
        )
    if obs:
        linhas.append(f"Anotações do DP: {obs}")
    return "\n".join(linhas) + "\n"


# ── Fase 3: aplicar defaults do perfil em um payload ────────────

def aplicar_defaults_do_perfil(payload: dict, remetente: str,
                                cnpj_empresa: str = "") -> list[str]:
    """Quando o pipeline detecta que um campo está faltando E o perfil do
    remetente tem padrão pra ele, preenche. Só preenche valores
    CADASTRADOS (não chuta). Respeita a regra de ouro: não sobrescreve
    o que já veio.

    Duas fontes de default:
      1. Aprendido automaticamente: `cargos_frequentes` + `omissoes_habituais`
         (salário padrão por cargo quando ele costuma ser omitido)
      2. Cadastrado manualmente: `defaults_quando_ausente` (endereço da
         empresa pra remetentes que não mandam comprovante, etc.)

    Retorna lista de campos preenchidos pra log.
    """
    perf = perfil_de(remetente, consolidar_se_faltar=False)
    if not perf:
        return []

    attrs = payload.setdefault("data", {}).setdefault("attributes", {})
    preenchidos = []

    # 1) Aprendido: salário padrão por cargo histórico
    # v2.16.15: detecta sentinela string ("NÃO INFORMADO", "SALARIO BASE", etc.)
    def _sal_vazio(v):
        if v is None or v == "" or isinstance(v, bool):
            return True
        if isinstance(v, (int, float)):
            return v <= 0
        try:
            s = str(v).replace("R$", "").replace(".", "").replace(",", ".").strip()
            return float(s) <= 0
        except (ValueError, TypeError):
            return True
    # v2.16.39: NOVA precedência pra preencher salário ausente:
    #   1. salarios_manuais_por_cargo (cadastrado pelo operador) — sempre vence
    #   2. salario_padrao histórico (média) — só se cargo está em omissoes_habituais
    if _sal_vazio(attrs.get("salario")):
        cargo_atual = (attrs.get("nomecargo") or "").upper().strip()
        # v2.16.51: fallback cargo em _dados_parciais quando Claude marcou
        # _pendente na raiz/bloco sem preencher attrs. Caso real ELIAS/EMERSON
        # (2026-07-06): perfil tem 'OPERADOR DE DESTILACAO' com R$1988.28
        # cadastrado, mas attrs vazio -> nao aplicava salario manual e
        # pendencia continuava sendo criada.
        if not cargo_atual:
            dp = payload.get("_dados_parciais") or {}
            if isinstance(dp, dict):
                for _k in ("cargo", "nomecargo", "nome_cargo", "funcao",
                           "cargo_nome", "nomefuncao"):
                    _v = dp.get(_k)
                    if _v:
                        cargo_atual = str(_v).upper().strip()
                        # Promove tambem pra attrs pra proximas etapas verem
                        attrs.setdefault("nomecargo", cargo_atual)
                        preenchidos.append(f"nomecargo={cargo_atual!r} (dp->attrs)")
                        break
        # 1) Manual cadastrado
        sal_manuais = perf.get("salarios_manuais_por_cargo") or {}
        sal_manual = sal_manuais.get(cargo_atual)
        if sal_manual and float(sal_manual) > 0:
            sal = float(sal_manual)
            attrs["salario"] = sal
            preenchidos.append(f"salario={sal} (manual do perfil)")
        else:
            # 2) Fallback: média histórica, só se padrão de omissão
            omissoes = (perf.get("padroes_aprendidos") or {}).get(
                "omissoes_habituais") or []
            if "salario" in omissoes:
                rec_cargo = (perf.get("cargos_frequentes") or {}).get(cargo_atual)
                if rec_cargo and rec_cargo.get("salario_padrao"):
                    sal = float(rec_cargo["salario_padrao"])
                    attrs["salario"] = sal
                    preenchidos.append(f"salario={sal} (média histórica)")

    # 2) Cadastrado: defaults manuais quando campos vêm vazios
    cad = perf.get("defaults_quando_ausente") or {}
    end_default = cad.get("endereco") or {}
    if end_default:
        # Endereço inteiro só entra se o que veio tá realmente vazio.
        # Match a match — preserva o que o cliente mandou (parcial vence default).
        for chave_form, chave_payload in [
            ("rua", "rua"), ("numero", "numero"), ("bairro", "bairro"),
            ("cidade", "cidade"), ("uf", "uf"), ("cep", "cep"),
        ]:
            if chave_payload in attrs and attrs[chave_payload] not in (None, ""):
                continue
            val = end_default.get(chave_form)
            if val in (None, ""):
                continue
            attrs[chave_payload] = val
            preenchidos.append(f"{chave_payload}={val!r} (endereço default)")

    return preenchidos


def remetente_omite_aso(remetente: str) -> bool:
    """v2.16.4: remetente marcado com `omitir_aso=True` no perfil.
    Pipeline usa pra não bloquear admissão por ausência de ASO."""
    perf = perfil_de(remetente, consolidar_se_faltar=False)
    if not perf:
        return False
    return bool((perf.get("defaults_quando_ausente") or {}).get("omitir_aso"))


def atualizar_salario_manual_cargo(remetente: str, cargo: str,
                                     valor: float | None) -> bool:
    """v2.16.39: cadastra (ou remove se valor=None/0) um salário fixo pra um
    cargo específico desse remetente. Sobrevive a consolidações.

    Próximas admissões em que esse cargo apareça SEM salário no email vão
    receber esse valor automaticamente — não depende mais de o cargo cair
    em `omissoes_habituais` nem de média histórica.
    """
    rem_norm = _extrair_email(remetente)
    cargo_norm = (cargo or "").upper().strip()
    if not rem_norm or not cargo_norm:
        return False
    perfis = carregar()
    perf = perfis.setdefault(rem_norm, {})
    sal_map = perf.setdefault("salarios_manuais_por_cargo", {})
    if valor is None or float(valor) <= 0:
        sal_map.pop(cargo_norm, None)
        acao = "removido"
    else:
        sal_map[cargo_norm] = round(float(valor), 2)
        acao = f"definido R$ {sal_map[cargo_norm]:.2f}"
    perf["atualizado_em"] = datetime.now().isoformat(timespec="seconds")
    salvar(perfis)
    log.info(f"[perfis] salário manual {cargo_norm!r} de {rem_norm}: {acao}")
    return True


def atualizar_defaults_quando_ausente(remetente: str,
                                       defaults: dict) -> bool:
    """v2.16.4: salva o dict `defaults_quando_ausente` no perfil.
    Sobrescreve o anterior. Retorna True se gravou.

    Estrutura esperada:
      {
        "endereco": {"rua": "...", "numero": 0, "bairro": "...",
                     "cidade": "...", "uf": "GO", "cep": "76530000"},
        "omitir_aso": True,
        "_observacao": "Texto livre opcional"
      }
    """
    rem_norm = _extrair_email(remetente)
    if not rem_norm:
        return False
    perfis = carregar()
    perf = perfis.setdefault(rem_norm, {})
    perf["defaults_quando_ausente"] = defaults or {}
    perf["atualizado_em"] = datetime.now().isoformat(timespec="seconds")
    salvar(perfis)
    log.info(f"[perfis] defaults_quando_ausente de {rem_norm} atualizado")
    return True
