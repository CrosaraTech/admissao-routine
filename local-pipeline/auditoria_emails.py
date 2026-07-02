"""auditoria_emails.py — registro de TODO email enviado pelo sistema.

v2.16.32: criado em resposta a incidente sério (2026-06-22) — o sistema
mandou email pra Esther (cliente da MAMBORE) sem o DP saber, com texto
ruim ("Não consegui identificar nenhuma admissão completa"). O DP não
tinha visibilidade nenhuma de que emails saíram, porque o pipeline
chamava gmail.responder_no_thread() sem registrar em audit local.

Princípio: TODO email sai por aqui. Sem exceção. Operador sempre tem
visibilidade de o que foi mandado, pra quem, quando, e com qual corpo.

Storage: NDJSON append-only em `emails_enviados.ndjson`. Fácil de
inspecionar, fácil de exportar, fácil de buscar.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger("admissao.auditoria_emails")

_DIR = Path(__file__).parent
AUDIT_FILE = _DIR / "emails_enviados.ndjson"


def registrar_envio(
    *,
    msg_id_original: str,
    thread_id: str,
    destinatario_email: str,
    destinatario_nome: str,
    assunto: str,
    corpo: str,
    cc: str | None = None,
    origem: str = "?",                  # "pipeline_direto", "web_aprovado", etc.
    operador: str = "",                 # nome/email do operador que aprovou
    sucesso: bool = True,
    erro: str = "",
    contexto: dict | None = None,
) -> None:
    """Append uma linha NDJSON com o envio. Nunca levanta exception
    (audit não pode quebrar o fluxo)."""
    try:
        entrada = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "origem": origem,
            "operador": operador,
            "sucesso": sucesso,
            "erro": erro,
            "msg_id_original": msg_id_original,
            "thread_id": thread_id,
            "para": {
                "email": destinatario_email,
                "nome": destinatario_nome,
            },
            "assunto": assunto,
            "cc": cc or "",
            "corpo": corpo,
            "contexto": contexto or {},
        }
        with AUDIT_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entrada, ensure_ascii=False) + "\n")
        log.info(
            f"[audit] email registrado: para={destinatario_email} "
            f"thread={thread_id[:16]} origem={origem}"
        )
    except Exception as e:
        # Best-effort — nunca quebra o fluxo
        log.warning(f"[audit] falha registrando envio: {type(e).__name__}: {e}")


def listar(
    desde_dias: int = 30,
    apenas_sucesso: bool = False,
) -> list[dict]:
    """Lê o NDJSON e devolve registros, mais recentes primeiro."""
    if not AUDIT_FILE.exists():
        return []
    from datetime import timedelta
    limite = datetime.now() - timedelta(days=desde_dias)
    out: list[dict] = []
    try:
        with AUDIT_FILE.open("r", encoding="utf-8") as f:
            for linha in f:
                try:
                    e = json.loads(linha)
                    ts = e.get("ts", "")
                    if ts:
                        try:
                            dt = datetime.fromisoformat(ts)
                            if dt < limite:
                                continue
                        except ValueError:
                            pass
                    if apenas_sucesso and not e.get("sucesso"):
                        continue
                    out.append(e)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    out.sort(key=lambda e: e.get("ts", ""), reverse=True)
    return out


def contar_ultimas_24h() -> int:
    """Conta envios nas últimas 24h (pra badge no dashboard)."""
    return len(listar(desde_dias=1))
