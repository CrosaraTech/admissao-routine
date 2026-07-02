"""post_admissao.py — choke point ÚNICO de POST /candidatos (v2.14.1).

Toda escrita no eContador (UI, orquestrador, scripts avulsos, testes) passa
por `postar_candidato_registrado`. Sem isso, a v2.14.0 já protegia os
caminhos da UI mas o reprocesso de email multi-pessoa pelo orquestrador
ainda podia duplicar — e cada caminho paralelo de escrita reaparecia como
bug em produção (caso YURI: 3 POSTs sem log; caso JENIFFY: 7 POSTs).

Responsabilidades concentradas:

  1. Idempotência: consulta_duplicata ANTES, registra_post DEPOIS.
     Se mesma_empresa → não POSTa, retorna sucesso "pulado" com o
     candidato_id antigo. Outra empresa → avisa (pode ser typo de CNPJ)
     mas deixa a decisão pro caller via `permitir_duplicata`.

  2. Atualização do payload em disco: se `payload_path` for passado,
     grava `resultado` (sucesso ou falha) no JSON pra auditoria —
     o que estava faltando nos POSTs manuais da UI antes da v2.14.0.

  3. Gmail (opcional): após sucesso, aplica `label_processado` na msg
     e remove `label_pendente` (lista de msg_ids). Sem isso a thread
     pendente continua sendo reprocessada e cria duplicata na próxima
     passada — pega o ciclo do RAIMUNDO (8 reprocessos).

  4. Telemetria: log estruturado em `admissao_log.ndjson` (status,
     candidato_id, origem, latência da chamada).

Tudo numa chamada — caller só monta o payload e chama.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import idempotencia

log = logging.getLogger("admissao.post")

# NDJSON de auditoria (mesmo arquivo do orquestrador — todas as escritas
# concentradas pra `jq` filtrar por origem depois).
_ADMISSAO_LOG = Path(__file__).parent / "admissao_log.ndjson"


def _nomes_compatíveis(nome_a: str, nome_b: str) -> bool:
    """v2.16.20: True quando dois nomes podem ser a MESMA pessoa.

    Critério prático: compartilham ≥1 palavra significativa (>3 chars,
    ignorando preposições comuns). Devolve True quando um dos nomes está
    vazio (não dá pra afirmar divergência).

    Casos cobertos:
      - 'MARIA SILVA'  vs  'MARIA SILVA SANTOS'   → True (casamento)
      - 'JOAO PEDRO'   vs  'JOÃO PEDRO'           → True (acento)
      - 'JOYCE LOPES'  vs  'THAYNARA SERAFIM'     → False (docs trocados)
      - 'JOSE A LIMA'  vs  'JOSÉ AUGUSTO LIMA'    → True ('JOSE','LIMA')
    """
    import re as _re
    import unicodedata as _ud
    PREPOSICOES = {"DE", "DA", "DO", "DAS", "DOS", "E"}
    def _palavras(s):
        s = _ud.normalize("NFD", str(s or "")).encode("ASCII", "ignore").decode("ASCII")
        s = _re.sub(r"[^A-Za-z\s]", " ", s).upper()
        return {w for w in s.split() if len(w) > 3 and w not in PREPOSICOES}
    a = _palavras(nome_a)
    b = _palavras(nome_b)
    if not a or not b:
        return True  # sem info pra comparar — deixa passar
    return len(a & b) >= 1


# ── Resultado estruturado ───────────────────────────────────────────

class PostResult:
    """Resultado de um POST registrado. Atributos:
      ok: True se candidato existe no eContador depois da chamada
      candidato_id: id do candidato (novo OU já existente quando pulado)
      pulou: True quando idempotência cortou o POST (já cadastrado)
      duplicata: True quando POSTou mesmo havendo histórico noutra empresa
      status_http: int do POST (None quando pulou)
      erro_ref: string curta do erro (None em sucesso)
      body_err: corpo do erro (truncado a 2k)
      hits_anteriores: lista de POSTs anteriores (mesmo CPF, qualquer empresa)
      origem: 'orquestrador', 'ui_resolver', 'ui_envio_forcado', ...
      duration_ms: latência do POST (0 quando pulou)
    """
    __slots__ = (
        "ok", "candidato_id", "pulou", "duplicata", "status_http",
        "erro_ref", "body_err", "hits_anteriores", "origem", "duration_ms",
        # v2.16.20: campos opcionais usados quando o resultado representa
        # pendência cliente (ex: divergência de nome na duplicata) — caller
        # deve usá-los pra rotear pra "pendente cliente" em vez de "técnica".
        "erro_tecnico", "motivo_cliente",
    )

    def __init__(
        self,
        ok: bool,
        candidato_id: Optional[str] = None,
        pulou: bool = False,
        duplicata: bool = False,
        status_http: Optional[int] = None,
        erro_ref: Optional[str] = None,
        body_err: str = "",
        hits_anteriores: Optional[list[dict]] = None,
        origem: str = "?",
        duration_ms: int = 0,
        erro_tecnico: Optional[str] = None,
        motivo_cliente: Optional[str] = None,
    ):
        self.ok = ok
        self.candidato_id = candidato_id
        self.erro_tecnico = erro_tecnico
        self.motivo_cliente = motivo_cliente
        self.pulou = pulou
        self.duplicata = duplicata
        self.status_http = status_http
        self.erro_ref = erro_ref
        self.body_err = body_err
        self.hits_anteriores = hits_anteriores or []
        self.origem = origem
        self.duration_ms = duration_ms

    def __repr__(self) -> str:
        if self.pulou:
            return f"<PostResult PULOU candidato={self.candidato_id} origem={self.origem}>"
        if self.ok:
            return f"<PostResult OK candidato={self.candidato_id} origem={self.origem}>"
        return f"<PostResult FAIL {self.erro_ref!r} origem={self.origem}>"


# ── Wrapper único ──────────────────────────────────────────────────

def postar_candidato_registrado(
    api,
    payload: dict,
    *,
    cpf,
    cnpj,
    nome: str,
    origem: str,
    msg_id: str = "",
    permitir_duplicata: bool = False,
    payload_path: Optional[Path] = None,
    gmail=None,
    label_processado: Optional[str] = None,
    label_pendente_remover: Optional[list[str]] = None,
) -> PostResult:
    """Choke point ÚNICO de POST /candidatos. Toda escrita passa aqui.

    Fluxo:
      1. idempotencia.consultar_duplicata(cpf, cnpj)
         → se mesma_empresa E !permitir_duplicata → SKIP como sucesso
      2. api.post_candidato(payload)
      3. idempotencia.registrar_post(cpf, cnpj, ref, nome, origem)
      4. se payload_path → grava resultado no JSON
      5. se gmail + label_processado → label processado + remove pendente
      6. log NDJSON

    Args:
        api: instância de EContadorAPI
        payload: dict JSON:API completo (já sanitizado)
        cpf: do candidato (qualquer formato, normalizado pra digitos)
        cnpj: da empresa (qualquer formato)
        nome: pra logs e registro
        origem: identifica o caminho ('orquestrador', 'ui_resolver', etc.)
        msg_id: id do email Gmail (entra no log + remove label dele depois)
        permitir_duplicata: True ignora hits da idempotência (force POST)
        payload_path: se passado, grava resultado no JSON (auditoria)
        gmail: GmailClient (opcional); sem ele os labels são no-op
        label_processado: aplicar na msg após sucesso
        label_pendente_remover: lista de msg_ids onde remover pendente

    Returns:
        PostResult com tudo necessário pro caller decidir próximos passos.
    """
    t0 = time.perf_counter()

    # ── 1. Idempotência: consulta ──────────────────────────────────
    try:
        hits = idempotencia.consultar_duplicata(cpf, cnpj)
    except Exception as e:
        log.warning(f"[post] idempotência consulta falhou ({type(e).__name__}: {e}) — seguindo sem hint")
        hits = []

    if hits and not permitir_duplicata:
        mesma_empresa = [h for h in hits if h.get("mesma_empresa")]
        if mesma_empresa:
            ja = mesma_empresa[0]
            cid = str(ja.get("candidato_id"))
            nome_registrado = str(ja.get("nome") or "")
            # v2.16.20: SAFETY NET — antes de pular silenciosamente, comparar
            # o nome ATUAL com o nome do candidato JÁ cadastrado. Bug real
            # (caso JOYCE/Thaynara, 2026-06-19): CPF da Joyce achou
            # candidato 11700 (registrado como "JOYCE LOPES DE OLIVEIRA"),
            # mas Claude extraiu "THAYNARA SERAFIM TOLENTINO" → pulou como
            # sucesso, ninguém viu que era confusão de documento.
            # Critério: se os nomes não compartilharem palavras significativas,
            # NÃO pula — gera erro pra caller decidir (vira pendência cliente).
            if not _nomes_compatíveis(nome, nome_registrado):
                log.warning(
                    f"[post] ⚠ DIVERGÊNCIA DE NOME: CPF=***{str(cpf or '')[-4:]} "
                    f"já é candidato {cid} cadastrado como "
                    f"'{nome_registrado}', mas a admissão atual diz "
                    f"'{nome}'. Pode ser docs trocados ou nome social — "
                    f"NÃO vou pular silencioso, gerando alerta pra revisão."
                )
                _log_ndjson({
                    "evento": "post_divergencia_nome",
                    "origem": origem,
                    "msg_id": msg_id,
                    "nome_atual": nome,
                    "nome_registrado": nome_registrado,
                    "candidato_id": cid,
                })
                return PostResult(
                    ok=False, candidato_id=None, pulou=False,
                    erro_tecnico=(
                        f"DIVERGÊNCIA: este CPF já é candidato {cid} "
                        f"cadastrado como '{nome_registrado}', mas o email "
                        f"diz que é admissão de '{nome}'. Documentos podem "
                        f"estar trocados — favor revisar."
                    ),
                    motivo_cliente=(
                        f"Identifiquei uma divergência: o CPF nos documentos "
                        f"está vinculado a {nome_registrado} (já cadastrado "
                        f"em {(ja.get('ts') or '')[:10]}), mas o pedido diz "
                        f"que é admissão de {nome}. Pode confirmar se está "
                        f"correto ou se houve troca de documentos?"
                    ),
                    hits_anteriores=hits, origem=origem, duration_ms=0,
                )
            log.info(
                f"[post] PULADO: {nome} (CPF=***{str(cpf or '')[-4:]}) "
                f"já é candidato {cid} ({ja.get('ts')}) na mesma empresa — origem={origem}"
            )
            # Mesmo PULADO: aplica label e atualiza payload na pasta (a thread
            # JÁ DEVE ser fechada — senão fica reprocessando no próximo polling)
            _atualizar_payload_em_disco(
                payload_path,
                {"status": "sucesso", "candidato_id": cid, "erro": None,
                 "origem": f"{origem}_skip"},
            )
            _aplicar_labels_gmail(
                gmail, msg_id, label_processado, label_pendente_remover
            )
            _log_ndjson({
                "evento": "post_pulado",
                "origem": origem,
                "msg_id": msg_id,
                "nome": nome,
                "candidato_id": cid,
                "motivo": "duplicata_mesma_empresa",
                "hits_count": len(hits),
            })
            return PostResult(
                ok=True, candidato_id=cid, pulou=True,
                hits_anteriores=hits, origem=origem, duration_ms=0,
            )
        # Hit noutra empresa: avisa mas POSTa (pode ser typo de CNPJ entre
        # tentativas — caller pode passar permitir_duplicata=True se já
        # confirmou com o usuário).
        log.warning(
            f"[post] {nome} (CPF=***{str(cpf or '')[-4:]}) tem {len(hits)} POST(s) "
            f"anterior(es) em OUTRA empresa — possível typo de CNPJ. "
            f"Seguindo com POST porque permitir_duplicata={permitir_duplicata}."
        )

    # ── 2. POST ───────────────────────────────────────────────────
    try:
        ok, ref, body_err = api.post_candidato(payload)
    except Exception as e:
        duration_ms = int((time.perf_counter() - t0) * 1000)
        log.exception(f"[post] exception no POST de {nome}: {e}")
        _atualizar_payload_em_disco(
            payload_path,
            {"status": "falha_post", "candidato_id": None,
             "erro": f"EXCEPTION: {e}", "body": "", "origem": origem},
        )
        _log_ndjson({
            "evento": "post_falha", "origem": origem, "msg_id": msg_id,
            "nome": nome, "erro": f"EXCEPTION: {e}",
            "duration_ms": duration_ms,
        })
        return PostResult(
            ok=False, erro_ref=f"EXCEPTION: {e}", body_err="",
            hits_anteriores=hits, origem=origem,
            duration_ms=duration_ms,
        )

    duration_ms = int((time.perf_counter() - t0) * 1000)

    if not ok:
        log.error(f"[post] FAIL {nome}: {ref} (status_http parse abaixo)")
        # Extrai status code da ref ("HTTP 422" → 422); usado pra classificar
        # como falha técnica vs erro de validação
        status_http = _extrair_status_http(ref)
        _atualizar_payload_em_disco(
            payload_path,
            {"status": "falha_post", "candidato_id": None,
             "erro": ref, "body": (body_err or "")[:2000], "origem": origem},
        )
        _log_ndjson({
            "evento": "post_falha", "origem": origem, "msg_id": msg_id,
            "nome": nome, "erro": ref, "status_http": status_http,
            "body_preview": (body_err or "")[:300],
            "duration_ms": duration_ms,
        })
        return PostResult(
            ok=False, erro_ref=ref, body_err=(body_err or "")[:2000],
            status_http=status_http, hits_anteriores=hits,
            origem=origem, duration_ms=duration_ms,
        )

    # ── 3. Sucesso: registra na idempotência ───────────────────────
    candidato_id = str(ref)
    # idempotencia.registrar_post é best-effort (nunca levanta); ainda assim
    # encapsulamos pra deixar explícito que falha aqui não é fatal.
    try:
        idempotencia.registrar_post(
            cpf, cnpj, candidato_id, nome=nome, origem=origem,
        )
    except Exception as e:  # noqa: BLE001 — defesa em profundidade
        log.warning(f"[post] falha registrando idempotência (não-fatal): {e}")

    # Hit noutra empresa que virou POST: marca duplicata=True pra UI tratar
    duplicata = bool(hits) and permitir_duplicata

    # ── 4. Atualiza payload na pasta ──────────────────────────────
    _atualizar_payload_em_disco(
        payload_path,
        {"status": "sucesso", "candidato_id": candidato_id,
         "erro": None, "origem": origem},
    )

    # ── 5. Gmail: label processado + remove pendente ──────────────
    _aplicar_labels_gmail(
        gmail, msg_id, label_processado, label_pendente_remover
    )

    # ── 6. Log NDJSON ─────────────────────────────────────────────
    _log_ndjson({
        "evento": "post_sucesso", "origem": origem, "msg_id": msg_id,
        "nome": nome, "candidato_id": candidato_id,
        "duracao_ms": duration_ms,
        "hits_anteriores": len(hits),
    })

    log.info(
        f"[post] OK {nome} → candidato {candidato_id} "
        f"({duration_ms}ms, origem={origem})"
    )
    return PostResult(
        ok=True, candidato_id=candidato_id, duplicata=duplicata,
        status_http=201, hits_anteriores=hits,
        origem=origem, duration_ms=duration_ms,
    )


# ── helpers ────────────────────────────────────────────────────────

def _extrair_status_http(ref: str) -> Optional[int]:
    """Pega o número de strings tipo "HTTP 422" / "HTTP 500"."""
    if not ref:
        return None
    s = str(ref)
    if "HTTP " in s:
        try:
            return int(s.split("HTTP ", 1)[1].split()[0])
        except (ValueError, IndexError):
            return None
    return None


def _atualizar_payload_em_disco(
    payload_path: Optional[Path],
    resultado: dict,
) -> None:
    """Best-effort: lê o JSON, atualiza `resultado` e regrava.
    Falha NUNCA bloqueia o fluxo (payload é auditoria — POST já aconteceu)."""
    if not payload_path:
        return
    try:
        doc = json.loads(Path(payload_path).read_text(encoding="utf-8"))
        doc["resultado"] = resultado
        Path(payload_path).write_text(
            json.dumps(doc, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:  # noqa: BLE001
        log.warning(
            f"[post] falha atualizando payload {payload_path}: "
            f"{type(e).__name__}: {e}"
        )


def _aplicar_labels_gmail(
    gmail,
    msg_id: str,
    label_processado: Optional[str],
    label_pendente_remover: Optional[list[str]],
) -> None:
    """Aplica label processado na msg_id e remove pendente de outras.
    Sem msg_id ou sem gmail = no-op (caso da importação manual)."""
    if not gmail:
        return
    if msg_id and label_processado:
        try:
            gmail.aplicar_label(msg_id, label_processado)
            log.info(f"[post] label '{label_processado}' aplicada em {msg_id[:16]}")
        except Exception as e:  # noqa: BLE001
            log.warning(f"[post] falha aplicando label processado: {e}")
    for mid in (label_pendente_remover or []):
        if not mid:
            continue
        # Tenta os 3 nomes mais comuns; a lib do gmail_client tem `remover_label`
        # que aceita o nome do label, então usamos isso quando possível.
        try:
            # Caller passou label_processado? Inferimos o nome do pendente
            # como o irmão dele ("ADMISSÃO/processado" → "ADMISSÃO/pendente").
            # Mas pra ficar simples, deixamos o caller decidir: ele já sabe
            # qual label remover, e o orquestrador atual passa explicitamente.
            # Aqui só repassamos com um nome derivado se houver convenção:
            if label_processado and "/" in label_processado:
                base = label_processado.rsplit("/", 1)[0]
                gmail.remover_label(mid, f"{base}/pendente")
        except Exception as e:  # noqa: BLE001
            log.warning(f"[post] falha removendo pendente de {mid[:16]}: {e}")


def _log_ndjson(entry: dict) -> None:
    """Append-only NDJSON. Falha silenciosa — auditoria não pode quebrar
    o fluxo já que o POST pode já ter sucedido."""
    entry["timestamp"] = datetime.now().isoformat(timespec="seconds")
    try:
        with open(_ADMISSAO_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning(f"[post] falha gravando log: {e}")
