"""rascunhos_resposta.py — fila de rascunhos de email pendentes de aprovação.

Quando `auto_email_pendencias_modo='rascunho'`, o pipeline NÃO manda o reply
direto pro cliente — grava aqui pra revisão humana. A analista vê o painel
/respostas, edita se quiser, e aprova → manda via Gmail.

v2.16.19: criado em resposta ao caso JOYCE/Thaynara (2026-06-19) — Claude
escreveu um texto técnico bruto que o pipeline mandaria literal pro cliente
("ATENÇÃO CRÍTICA: ... payload pronto ..."). Reputação salva por agora
porque a flag estava OFF, mas isso é exatamente o caso de uso da revisão.

Storage: 1 arquivo JSON por rascunho em `rascunhos/<id>.json`. Idempotente,
inspecionável, fácil de migrar. ID = hash curto do (msg_id + ts).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("admissao.rascunhos")

_DIR = Path(__file__).parent
RASCUNHOS_DIR = _DIR / "rascunhos"

STATUS_PENDENTE = "pendente"
STATUS_ENVIADO = "enviado"
STATUS_DESCARTADO = "descartado"


def _slug_id(msg_id: str, ts: str) -> str:
    """Hash curto pra ID do rascunho. Usa msg_id + ts."""
    h = hashlib.md5(f"{msg_id}|{ts}".encode("utf-8")).hexdigest()
    return h[:12]


def _slugify(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "")
    s = s.encode("ASCII", "ignore").decode("ASCII")
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s[:40] or "sem-nome"


def _atomic_write(path: Path, content: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def criar_rascunho(
    *,
    msg_id: str,
    thread_id: str | None,
    remetente_email: str,
    remetente_nome: str | None,
    assunto: str | None,
    corpo_proposto: str,
    contexto: dict | None = None,
) -> dict:
    """Salva um novo rascunho com status=pendente. Retorna o dict salvo.

    `contexto` pode trazer info útil pra UI: nomes dos candidatos, motivo
    da pendência, link pra payloads etc.
    """
    RASCUNHOS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().isoformat(timespec="seconds")
    rid = _slug_id(msg_id, ts)
    rec = {
        "id": rid,
        "ts_criado": ts,
        "ts_acao": None,
        "operador": None,
        "status": STATUS_PENDENTE,
        "msg_id": msg_id,
        "thread_id": thread_id,
        "remetente_email": remetente_email or "",
        "remetente_nome": remetente_nome or "",
        "assunto": assunto or "",
        "corpo_proposto": corpo_proposto,   # original (gerado pelo sistema)
        "corpo_editado": None,              # se a analista editar antes de aprovar
        "contexto": contexto or {},
    }
    path = RASCUNHOS_DIR / f"{rid}_{_slugify(remetente_email)}.json"
    _atomic_write(path, json.dumps(rec, ensure_ascii=False, indent=2))
    log.info(
        f"[rascunhos] novo rascunho id={rid} pra {remetente_email} "
        f"(thread {thread_id[:16] if thread_id else '?'})"
    )
    return rec


def _achar_arquivo(rid: str) -> Path | None:
    if not RASCUNHOS_DIR.exists():
        return None
    for p in RASCUNHOS_DIR.glob(f"{rid}_*.json"):
        return p
    return None


def carregar(rid: str) -> dict | None:
    p = _achar_arquivo(rid)
    if not p:
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning(f"[rascunhos] falha lendo {p.name}: {e}")
        return None


def atualizar(rid: str, **patch: Any) -> dict | None:
    """Aplica patch ao rascunho. Não muda status — usar `marcar_*` pra isso."""
    rec = carregar(rid)
    if not rec:
        return None
    rec.update(patch)
    p = _achar_arquivo(rid)
    if p:
        _atomic_write(p, json.dumps(rec, ensure_ascii=False, indent=2))
    return rec


def marcar_enviado(rid: str, operador: str = "") -> dict | None:
    return atualizar(
        rid,
        status=STATUS_ENVIADO,
        ts_acao=datetime.now().isoformat(timespec="seconds"),
        operador=operador,
    )


def marcar_descartado(rid: str, operador: str = "", motivo: str = "") -> dict | None:
    return atualizar(
        rid,
        status=STATUS_DESCARTADO,
        ts_acao=datetime.now().isoformat(timespec="seconds"),
        operador=operador,
        motivo_descarte=motivo,
    )


def descartar_por_msg_id(msg_id: str, operador: str = "auto",
                          motivo: str = "pendencia resolvida") -> int:
    """v2.16.46: descarta em lote todos rascunhos PENDENTES vinculados a
    um msg_id especifico. Usado apos POST /candidatos OK — se a pendencia
    foi resolvida, o rascunho de resposta ao cliente perde razao de existir.
    Retorna quantos foram marcados.
    """
    if not msg_id:
        return 0
    n = 0
    for rec in listar(status=STATUS_PENDENTE):
        if str(rec.get("msg_id", "")) == msg_id:
            marcar_descartado(rec["id"], operador=operador, motivo=motivo)
            n += 1
    return n


def listar(
    status: str | None = STATUS_PENDENTE,
    incluir_arquivados: bool = False,
) -> list[dict]:
    """Lista rascunhos. Por padrão só os pendentes."""
    if not RASCUNHOS_DIR.exists():
        return []
    out: list[dict] = []
    for p in sorted(RASCUNHOS_DIR.glob("*.json"), reverse=True):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if status and rec.get("status") != status and not incluir_arquivados:
            continue
        out.append(rec)
    # Mais recente primeiro
    out.sort(key=lambda r: r.get("ts_criado", ""), reverse=True)
    return out


def contar_pendentes() -> int:
    return len(listar(status=STATUS_PENDENTE))


def corpo_final(rec: dict) -> str:
    """Retorna o corpo a usar pra enviar: editado se foi editado, senão original."""
    return rec.get("corpo_editado") or rec.get("corpo_proposto", "")
