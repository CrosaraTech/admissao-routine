"""Cliente da API E-plugin Alterdata (eContador).

Cada operação contra a API é logada com par BEFORE/AFTER + duração e
gravada em `econtador_audit.ndjson` (NDJSON append-only). Isso permite
rastrear qualquer chamada que tenha dado errado ou demorado demais:

  grep '"operation":"POST /candidatos"' econtador_audit.ndjson
  jq 'select(.success==false)' econtador_audit.ndjson
  jq 'select(.duration_ms>5000)' econtador_audit.ndjson
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

import httpx


log = logging.getLogger("admissao.ecotador")

# Audit file (append-only) na mesma pasta do módulo
AUDIT_FILE = Path(__file__).parent / "econtador_audit.ndjson"


def cnpj_limpo(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")


def _audit_write(entry: dict) -> None:
    """Append uma linha JSON em econtador_audit.ndjson.
    Falha silenciosamente — auditoria não pode quebrar o pipeline."""
    try:
        entry["timestamp"] = datetime.now().isoformat(timespec="milliseconds")
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning(f"Falha gravando audit log: {e}")


class EContadorAPI:
    def __init__(self, base_url: str, token: str):
        self.base = base_url.rstrip("/")
        self.client = httpx.Client(
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/vnd.api+json",
                "Accept": "application/vnd.api+json",
            },
            timeout=60.0,
        )
        # Token só pra contexto de log (mascarado — nunca grava completo)
        self._token_hash = token[-8:] if token else ""

    def close(self) -> None:
        self.client.close()

    # ---- Empresa -------------------------------------------------

    def resolver_empresa(self, cnpj: str) -> tuple[str | None, dict]:
        """Retorna (empresa_id, attributes) ou (None, {}).
        BEFORE/AFTER logado em log.info + econtador_audit.ndjson."""
        cnpj_d = cnpj_limpo(cnpj)
        if not cnpj_d:
            return None, {}

        corr_id = uuid.uuid4().hex[:8]
        url = f"{self.base}/empresas"
        params = {"filter[cpfcnpj]": cnpj_d, "page[limit]": 5}

        log.info(f"[{corr_id}] econtador.before GET /empresas cnpj={cnpj_d}")
        _audit_write({
            "corr_id": corr_id,
            "phase": "before",
            "operation": "GET /empresas",
            "url": url,
            "params": params,
            "input": {"cnpj": cnpj_d},
        })

        t0 = time.perf_counter()
        try:
            r = self.client.get(url, params=params)
        except Exception as e:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            log.exception(f"[{corr_id}] econtador.after GET /empresas EXCEPTION {elapsed_ms}ms")
            _audit_write({
                "corr_id": corr_id, "phase": "after",
                "operation": "GET /empresas", "success": False,
                "exception": str(e), "duration_ms": elapsed_ms,
            })
            return None, {}

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        if r.status_code != 200:
            log.error(
                f"[{corr_id}] econtador.after GET /empresas FAIL "
                f"status={r.status_code} duration_ms={elapsed_ms} body={r.text[:200]}"
            )
            _audit_write({
                "corr_id": corr_id, "phase": "after",
                "operation": "GET /empresas", "success": False,
                "status_code": r.status_code, "duration_ms": elapsed_ms,
                "body_preview": r.text[:500],
            })
            return None, {}

        data = r.json().get("data", [])
        if not data:
            log.info(
                f"[{corr_id}] econtador.after GET /empresas OK status=200 "
                f"duration_ms={elapsed_ms} resultados=0"
            )
            _audit_write({
                "corr_id": corr_id, "phase": "after",
                "operation": "GET /empresas", "success": True,
                "status_code": 200, "duration_ms": elapsed_ms, "n_resultados": 0,
            })
            return None, {}

        empresa_id = str(data[0]["id"])
        attrs = data[0].get("attributes", {})
        razao = attrs.get("nome", "?")
        log.info(
            f"[{corr_id}] econtador.after GET /empresas OK status=200 "
            f"duration_ms={elapsed_ms} empresa_id={empresa_id} razao='{razao}'"
        )
        _audit_write({
            "corr_id": corr_id, "phase": "after",
            "operation": "GET /empresas", "success": True,
            "status_code": 200, "duration_ms": elapsed_ms,
            "n_resultados": len(data), "empresa_id": empresa_id, "razao_social": razao,
        })
        return empresa_id, attrs

    def iterar_todas_empresas(self, page_size: int = 200, log_progresso: bool = True):
        """Gerador que pagina GET /empresas inteira. Cada item yieldado é um
        dict com pelo menos {id, cpfcnpj, nome}.

        Usado pra popular o cache local de CNPJs cadastrados (whitelist pra
        auto-correção de typos do OCR). Robust: erros HTTP em uma página não
        interrompem — apenas paramos e devolvemos o que conseguimos.

        Args:
            page_size: tamanho por página (cap real da API é 200)
            log_progresso: loga "...N empresas carregadas" a cada página
        """
        offset = 0
        total_emitido = 0
        while True:
            corr_id = uuid.uuid4().hex[:8]
            url = f"{self.base}/empresas"
            params = {"page[limit]": page_size, "page[offset]": offset}
            try:
                r = self.client.get(url, params=params)
            except Exception as e:
                log.warning(f"[{corr_id}] iterar_todas_empresas exception @offset={offset}: {e}")
                break
            if r.status_code != 200:
                log.warning(
                    f"[{corr_id}] iterar_todas_empresas HTTP {r.status_code} @offset={offset} — parando"
                )
                break

            page_data = r.json().get("data", []) or []
            if not page_data:
                break

            for it in page_data:
                a = it.get("attributes") or {}
                yield {
                    "id": it.get("id"),
                    "cpfcnpj": a.get("cpfcnpj") or a.get("cnpj") or "",
                    "nome": a.get("nome") or "",
                }
            total_emitido += len(page_data)
            if log_progresso:
                log.info(f"   ...{total_emitido} empresas carregadas")
            if len(page_data) < page_size:
                break
            offset += page_size

    # ---- Departamento -------------------------------------------

    def listar_departamentos(self, empresa_id: str) -> list[dict]:
        """Retorna lista de {id, nome} dos departamentos da empresa.
        BEFORE/AFTER logado em log.info + econtador_audit.ndjson."""
        corr_id = uuid.uuid4().hex[:8]
        url = f"{self.base}/departamentos"
        params = {"filter[empresaId]": empresa_id, "page[limit]": 200}

        log.info(f"[{corr_id}] econtador.before GET /departamentos empresa_id={empresa_id}")
        _audit_write({
            "corr_id": corr_id, "phase": "before",
            "operation": "GET /departamentos",
            "url": url, "params": params, "input": {"empresa_id": empresa_id},
        })

        t0 = time.perf_counter()
        try:
            r = self.client.get(url, params=params)
        except Exception as e:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            log.exception(f"[{corr_id}] econtador.after GET /departamentos EXCEPTION {elapsed_ms}ms")
            _audit_write({
                "corr_id": corr_id, "phase": "after",
                "operation": "GET /departamentos", "success": False,
                "exception": str(e), "duration_ms": elapsed_ms,
            })
            return []

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        if r.status_code != 200:
            log.error(
                f"[{corr_id}] econtador.after GET /departamentos FAIL "
                f"status={r.status_code} duration_ms={elapsed_ms} body={r.text[:200]}"
            )
            _audit_write({
                "corr_id": corr_id, "phase": "after",
                "operation": "GET /departamentos", "success": False,
                "status_code": r.status_code, "duration_ms": elapsed_ms,
                "body_preview": r.text[:500],
            })
            return []

        deptos = [
            {"id": str(it["id"]), "nome": (it.get("attributes") or {}).get("nome", "?")}
            for it in r.json().get("data", [])
        ]
        log.info(
            f"[{corr_id}] econtador.after GET /departamentos OK status=200 "
            f"duration_ms={elapsed_ms} n_deptos={len(deptos)}"
        )
        _audit_write({
            "corr_id": corr_id, "phase": "after",
            "operation": "GET /departamentos", "success": True,
            "status_code": 200, "duration_ms": elapsed_ms,
            "n_resultados": len(deptos),
            "deptos_resumo": [{"id": d["id"], "nome": d["nome"][:40]} for d in deptos[:10]],
        })
        return deptos

    # ---- Candidato ----------------------------------------------

    # v2.16.35: lista padrão de relationships pra include no GET de candidato.
    # Sem isso, a API JSON:API retorna só `links` — `data.id` fica oculto e
    # parece que o candidato está com tudo None (bug encontrado no caso
    # GABRIEL/MAMBORE 2026-06-22). Lista enxuta porque incluir TUDO infla
    # o response sem ganho.
    INCLUDE_CANDIDATO_PADRAO = ",".join([
        "empresa", "departamento", "funcao", "statusadmissao",
        "tipoadmissao", "tipovinculotrabalhista", "categoriawdp",
        "formapagamento", "tipoidentidade", "sexo", "raca",
        "estadocivil", "escolaridade", "nacionalidade",
        "paisnascimento", "naturalidade", "ufidentidade",
        "ufctps", "estado", "statusatestadoocupacional",
        "tipoDeDeficiencia", "pais",
    ])

    def get_candidato(
        self,
        candidato_id: str,
        include: str | None = None,
    ) -> tuple[bool, dict]:
        """v2.16.35: GET de candidato com relationships populadas por padrão.

        Sem `?include=`, a API JSON:API só devolve `links` nas relationships,
        sem `data.id`. Quando alguém lê o resultado pensa que está tudo
        vazio. Caso real 2026-06-22 (GABRIEL/MAMBORE): operador achou que
        candidato 11896 tinha sido cadastrado com tudo None — na verdade
        tinha empresa=1277, statusadmissao=1, etc., só não vinham no JSON
        sem o include.

        Returns:
            (ok, dict) — dict contém data + included (rels expandidas)
        """
        inc = include or self.INCLUDE_CANDIDATO_PADRAO
        try:
            r = self.client.get(
                f"{self.base}/candidatos/{candidato_id}",
                params={"include": inc},
            )
            if r.status_code == 200:
                return True, r.json()
            log.warning(
                f"[ecotador] GET /candidatos/{candidato_id} HTTP {r.status_code}"
            )
            return False, {"erro": f"HTTP {r.status_code}", "body": r.text[:500]}
        except Exception as e:
            log.warning(f"[ecotador] GET /candidatos/{candidato_id} falhou: {e}")
            return False, {"erro": f"{type(e).__name__}: {e}"}


    def post_candidato(self, payload: dict) -> tuple[bool, str, str]:
        """POST principal — cria candidato no eContador.
        Logging EXTENSIVO (BEFORE com snapshot dos campos chave, AFTER com
        candidato_id ou body do erro). Operação mais crítica do pipeline."""
        corr_id = uuid.uuid4().hex[:8]
        url = f"{self.base}/candidatos"

        # Snapshot do payload pro log (campos chave + tamanho)
        attrs = (payload.get("data") or {}).get("attributes") or {}
        rels = (payload.get("data") or {}).get("relationships") or {}
        try:
            payload_bytes = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        except Exception:
            payload_bytes = -1

        contexto = {
            "nome": attrs.get("nome", "?"),
            "cpf": attrs.get("cpf", "?"),
            "admissao": attrs.get("admissao", "?"),
            "salario": attrs.get("salario", "?"),
            "empresa_id": (rels.get("empresa") or {}).get("data", {}).get("id"),
            "funcao_id": (rels.get("funcao") or {}).get("data", {}).get("id"),
            "departamento_id": (rels.get("departamento") or {}).get("data", {}).get("id"),
            "n_attrs": len(attrs),
            "n_rels": len(rels),
            "payload_bytes": payload_bytes,
        }

        log.info(
            f"[{corr_id}] econtador.before POST /candidatos "
            f"nome='{contexto['nome']}' cpf={contexto['cpf']} "
            f"empresa_id={contexto['empresa_id']} funcao_id={contexto['funcao_id']} "
            f"depto_id={contexto['departamento_id']} "
            f"size={payload_bytes}B ({len(attrs)} attrs + {len(rels)} rels)"
        )
        _audit_write({
            "corr_id": corr_id, "phase": "before",
            "operation": "POST /candidatos",
            "url": url, "input": contexto,
        })

        t0 = time.perf_counter()
        try:
            r = self.client.post(url, json=payload)
        except Exception as e:
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            log.exception(
                f"[{corr_id}] econtador.after POST /candidatos EXCEPTION "
                f"duration_ms={elapsed_ms} erro={e}"
            )
            _audit_write({
                "corr_id": corr_id, "phase": "after",
                "operation": "POST /candidatos", "success": False,
                "exception": str(e), "duration_ms": elapsed_ms,
                "input_snapshot": contexto,
            })
            return False, f"EXCEPTION: {e}", ""

        elapsed_ms = int((time.perf_counter() - t0) * 1000)

        if r.status_code == 201:
            try:
                candidato_id = str(r.json()["data"]["id"])
            except (KeyError, ValueError, TypeError) as e:
                log.error(
                    f"[{corr_id}] econtador.after POST /candidatos 201 mas resposta "
                    f"sem data.id: {e} — body={r.text[:300]}"
                )
                _audit_write({
                    "corr_id": corr_id, "phase": "after",
                    "operation": "POST /candidatos", "success": False,
                    "status_code": 201, "duration_ms": elapsed_ms,
                    "parse_error": str(e), "body_preview": r.text[:500],
                })
                return False, "HTTP 201 sem data.id", r.text[:2000]

            log.info(
                f"[{corr_id}] econtador.after POST /candidatos OK status=201 "
                f"duration_ms={elapsed_ms} candidato_id={candidato_id} "
                f"nome='{contexto['nome']}'"
            )
            _audit_write({
                "corr_id": corr_id, "phase": "after",
                "operation": "POST /candidatos", "success": True,
                "status_code": 201, "duration_ms": elapsed_ms,
                "candidato_id": candidato_id, "input_snapshot": contexto,
            })
            return True, candidato_id, ""

        # Falha (4xx/5xx)
        log.error(
            f"[{corr_id}] econtador.after POST /candidatos FAIL status={r.status_code} "
            f"duration_ms={elapsed_ms} nome='{contexto['nome']}' "
            f"body={r.text[:500]}"
        )
        _audit_write({
            "corr_id": corr_id, "phase": "after",
            "operation": "POST /candidatos", "success": False,
            "status_code": r.status_code, "duration_ms": elapsed_ms,
            "body_preview": r.text[:2000], "input_snapshot": contexto,
        })
        return False, f"HTTP {r.status_code}", r.text[:2000]
