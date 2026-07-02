"""Idempotência de POST + guarda de reprocesso (v2.14.0).

Dois problemas reais que este módulo resolve (diagnóstico 12/06/2026):

1. CANDIDATOS DUPLICADOS NO ECONTADOR
   31 POSTs de sucesso no admissao_log.ndjson para ~17 pessoas reais
   (JENIFFY 7x, EDIMAURA 6x, LUANA 3x, YURI 3x via UI). Mecanismos:
     - reprocessar email multi-pessoa re-POSTa quem já tinha passado;
     - POST manual da UI ("Enviar mesmo assim" / "Aplicar form e POSTar")
       não checava histórico nem registrava em lugar nenhum;
     - a grafia do nome varia entre chamadas do Claude (EDIMAURA/EDMAURA,
       JENIFFY/JENNIFY), então dedup por NOME não funciona.
   → Registro local indexado por CPF+CNPJ, consultado ANTES de todo POST.

2. REPROCESSO CEGO (churn)
   RAIMUNDO: 8 tentativas idênticas no mesmo dia (~2 chamadas Claude
   cada, ~US$ 0,40/clique) sem NADA ter mudado nas tabelas locais.
   → Fingerprint das tabelas; se nada mudou desde a última tentativa
   daquele msg_id, a UI avisa antes de reprocessar.

Arquivos de estado (ao lado deste módulo, escrita atômica):
  candidatos_postados.json  {"<cpf>|<cnpj>": [{candidato_id, nome, ts, origem}]}
  reprocesso_fp.json        {"<msg_id>": {"fp": "...", "ts": "..."}}

Uso (UI e orquestrador):
  hits = idempotencia.consultar_duplicata(cpf, cnpj)
  if hits: ...avisar/abortar...
  ok, ref, _ = api.post_candidato(payload)
  if ok: idempotencia.registrar_post(cpf, cnpj, ref, nome, origem="ui")
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

log = logging.getLogger("admissao.idempotencia")

_DIR = Path(__file__).parent
REGISTRO_FILE = _DIR / "candidatos_postados.json"
FP_FILE = _DIR / "reprocesso_fp.json"
PAYLOADS_DIR = _DIR / "payloads"

# Tabelas locais cuja mudança justifica reprocessar uma pendência.
# (arquivo ausente é simplesmente ignorado no fingerprint)
_TABELAS_FP = [
    "funcao_aliases.json",
    "salarios_padrao.json",
    "funcoes_cbo.xlsx",
    "departamentos.json",
    "cnpj_overrides.json",
    "funcao_overrides.json",
    "remetente_aliases.json",
    "regras.json",
    "config.json",
]


# ── helpers ──────────────────────────────────────────────────────────────────

def _so_digitos(v) -> str:
    return re.sub(r"\D", "", str(v or ""))


def _chave(cpf, cnpj) -> str:
    return f"{_so_digitos(cpf)}|{_so_digitos(cnpj)}"


def _carregar(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as e:
        log.warning(f"Falha lendo {path.name}: {e} — tratando como vazio")
        return {}


def _salvar_atomico(path: Path, data: dict) -> None:
    """Escreve via arquivo temporário + os.replace — resiste a crash no meio."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── registro de candidatos postados ─────────────────────────────────────────

def backfill_de_payloads(registro: dict) -> int:
    """Popula o registro a partir dos payloads/*.json com resultado=sucesso.

    Cobre o histórico anterior à v2.14.0. Os POSTs manuais antigos da UI
    não atualizavam o resultado no disco, então o backfill é PARCIAL por
    natureza — daqui pra frente todo POST passa por registrar_post().
    Retorna quantas entradas novas foram adicionadas.
    """
    if not PAYLOADS_DIR.exists():
        return 0
    novas = 0
    for p in sorted(PAYLOADS_DIR.glob("*.json")):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        res = doc.get("resultado") or {}
        if str(res.get("status") or "") != "sucesso" or not res.get("candidato_id"):
            continue
        att = ((doc.get("payload") or {}).get("data") or {}).get("attributes") or {}
        resol = doc.get("resolucao") or {}
        cpf = att.get("cpf")
        cnpj = resol.get("cnpj_empresa")
        if not _so_digitos(cpf):
            continue
        ch = _chave(cpf, cnpj)
        lista = registro.setdefault(ch, [])
        cid = str(res.get("candidato_id"))
        if any(str(e.get("candidato_id")) == cid for e in lista):
            continue
        lista.append({
            "candidato_id": cid,
            "nome": str(att.get("nome") or resol.get("nome") or ""),
            "ts": str(doc.get("timestamp") or ""),
            "origem": "backfill_payload",
        })
        novas += 1
    return novas


def garantir_registro() -> dict:
    """Carrega o registro; na primeira vez, faz backfill dos payloads."""
    registro = _carregar(REGISTRO_FILE)
    if not registro:
        n = backfill_de_payloads(registro)
        if n:
            log.info(f"Registro de candidatos: backfill de {n} POST(s) de payloads/")
            try:
                _salvar_atomico(REGISTRO_FILE, registro)
            except OSError as e:
                log.warning(f"Falha salvando {REGISTRO_FILE.name}: {e}")
    return registro


def consultar_duplicata(cpf, cnpj=None) -> list[dict]:
    """POSTs anteriores do mesmo CPF. Cada hit ganha `mesma_empresa: bool`.

    Match por CPF em OUTRA empresa também volta (com mesma_empresa=False):
    serve pra pegar CNPJ digitado com typo entre tentativas.
    Retorna [] quando o CPF nunca foi postado (caminho normal).
    """
    cpf_d = _so_digitos(cpf)
    if not cpf_d:
        return []
    registro = garantir_registro()
    cnpj_d = _so_digitos(cnpj)
    hits: list[dict] = []
    for ch, lista in registro.items():
        ch_cpf, _, ch_cnpj = ch.partition("|")
        if ch_cpf != cpf_d:
            continue
        for e in lista:
            h = dict(e)
            h["cnpj"] = ch_cnpj
            h["mesma_empresa"] = bool(cnpj_d) and ch_cnpj == cnpj_d
            hits.append(h)
    hits.sort(key=lambda h: str(h.get("ts") or ""), reverse=True)
    return hits


def registrar_post(cpf, cnpj, candidato_id, nome: str = "", origem: str = "ui") -> None:
    """Registra um POST de sucesso. NUNCA levanta exceção (não pode quebrar
    o fluxo depois que o candidato já foi criado no eContador)."""
    try:
        registro = garantir_registro()
        ch = _chave(cpf, cnpj)
        lista = registro.setdefault(ch, [])
        cid = str(candidato_id)
        if not any(str(e.get("candidato_id")) == cid for e in lista):
            lista.append({
                "candidato_id": cid,
                "nome": str(nome or ""),
                "ts": datetime.now().isoformat(timespec="seconds"),
                "origem": origem,
            })
            _salvar_atomico(REGISTRO_FILE, registro)
        log.info(f"[idempotência] registrado candidato {cid} ({origem}) chave={ch}")
    except Exception as e:  # noqa: BLE001 — registro é best-effort
        log.warning(f"[idempotência] falha registrando POST: {e}")


def descricao_duplicatas(hits: list[dict]) -> str:
    """Texto amigável pros messagebox da UI."""
    linhas = []
    for h in hits[:5]:
        onde = "MESMA empresa" if h.get("mesma_empresa") else f"outra empresa (CNPJ {h.get('cnpj') or '?'})"
        ts = str(h.get("ts") or "?")[:16].replace("T", " ")
        linhas.append(
            f"  • candidato {h.get('candidato_id')} — {h.get('nome') or '(sem nome)'}\n"
            f"    em {ts}, {onde}, via {h.get('origem') or '?'}"
        )
    if len(hits) > 5:
        linhas.append(f"  • ... e mais {len(hits) - 5}")
    return "\n".join(linhas)


# ── guarda de reprocesso (fingerprint das tabelas locais) ───────────────────

def fingerprint_tabelas() -> str:
    """Hash do estado (mtime+tamanho) das tabelas que afetam a resolução."""
    h = hashlib.md5()
    for nome in _TABELAS_FP:
        p = _DIR / nome
        try:
            st = p.stat()
            h.update(f"{nome}:{st.st_mtime_ns}:{st.st_size};".encode())
        except OSError:
            h.update(f"{nome}:ausente;".encode())
    return h.hexdigest()


def aviso_reprocesso(msg_id: str) -> str | None:
    """Se NADA mudou nas tabelas desde a última tentativa deste msg_id,
    retorna um texto de aviso pra UI exibir. Senão, None (segue normal).

    Importante: resposta nova do cliente no Gmail NÃO entra no fingerprint
    (é estado remoto) — por isso o aviso pergunta, não bloqueia.
    """
    if not msg_id:
        return None
    salvo = _carregar(FP_FILE).get(str(msg_id))
    if not salvo:
        return None
    if salvo.get("fp") != fingerprint_tabelas():
        return None
    ts = str(salvo.get("ts") or "?")[:16].replace("T", " ")
    return (
        f"⚠ Nada mudou nas tabelas locais (aliases, salários padrão, planilha "
        f"CBO, departamentos, overrides) desde a última tentativa deste email "
        f"({ts}).\n\n"
        f"Se o CLIENTE respondeu no email ou você cadastrou algo direto no "
        f"eContador, pode reprocessar. Senão o resultado será IDÊNTICO e vai "
        f"custar ~US$ 0,40 em chamadas Claude à toa."
    )


def salvar_fingerprint_reprocesso(msg_id: str,
                                   thread_msgs_count: int | None = None) -> None:
    """Grava o fingerprint após uma tentativa (best-effort, nunca levanta).

    v2.16.1: também grava `thread_msgs_count` (quantas mensagens a thread
    Gmail tinha no momento da tentativa). Permite ao polling detectar quando
    o cliente respondeu (n_msgs aumentou) e disparar reprocesso automático.
    """
    if not msg_id:
        return
    try:
        data = _carregar(FP_FILE)
        registro = {
            "fp": fingerprint_tabelas(),
            "ts": datetime.now().isoformat(timespec="seconds"),
        }
        if thread_msgs_count is not None:
            registro["thread_msgs_count"] = int(thread_msgs_count)
        data[str(msg_id)] = registro
        _salvar_atomico(FP_FILE, data)
    except Exception as e:  # noqa: BLE001
        log.warning(f"[idempotência] falha salvando fingerprint: {e}")


def deve_pular_reprocesso_auto(
    msg_id: str,
    thread_msgs_count: int | None = None,
) -> tuple[bool, str]:
    """Decide se o polling AUTOMÁTICO deve pular este pendente.

    v2.16.1: diferente de `aviso_reprocesso` (que só ALERTA pro operador
    no clique manual), esta função BLOQUEIA o reprocesso automático
    quando nada mudou desde a última tentativa.

    Critério de mudança (qualquer um destes destrava o reprocesso):
      - thread Gmail recebeu mensagem nova (cliente respondeu)
      - tabelas locais mudaram (DP cadastrou alias, salário padrão, etc.)

    Retorna (pular, motivo). Motivo é texto curto pro log.

    Nunca pula:
      - msg_id sem fingerprint salvo (nunca foi tentado ainda)
      - msg_id="" (defensivo)
    """
    if not msg_id:
        return False, ""
    salvo = _carregar(FP_FILE).get(str(msg_id))
    if not salvo:
        return False, "primeira tentativa (sem histórico)"
    if salvo.get("fp") != fingerprint_tabelas():
        return False, "tabelas locais mudaram (alias/salário/etc.)"
    msgs_salvo = salvo.get("thread_msgs_count")
    if (msgs_salvo is not None and thread_msgs_count is not None
            and int(thread_msgs_count) != int(msgs_salvo)):
        return (False,
                f"thread mudou ({msgs_salvo}→{thread_msgs_count} msgs)")
    ts = str(salvo.get("ts") or "?")[:16].replace("T", " ")
    return True, f"nada mudou desde {ts}"
