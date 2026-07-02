"""directdata_client.py — cliente das APIs da Direct Data pra enriquecimento
de cadastro de pessoa física via CPF.

APIs implementadas (custo total: R$ 1,24 por admissão completa):
  1. CadastroPessoaFisica       R$ 0,16  — nome, mãe, nascimento, sexo, endereço
  2. MinisterioTrabalhoPIS      R$ 0,36  — número do PIS
  3. TituloLocalVotacao         R$ 0,72  — título eleitor, zona, seção

Skip automático: se o payload já tem TODOS os campos de uma API, NÃO chama —
economiza dinheiro real. Cache em RAM: CPF consultado 2× na mesma sessão
(ex: reprocessamento) reusa resposta sem custo adicional.

Token: lido de DIRECTDATA_TOKEN no .env. Se ausente, cliente fica desabilitado
e degrada graciosamente (pipeline segue sem enriquecimento Direct Data).

Documentação: https://directd.com.br (painel → Marketplace).
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

log = logging.getLogger("admissao.directdata")

BASE_URL = "https://apiv3.directd.com.br/api"

# Custos por chamada (centavos de R$). Mantidos pra logging/billing.
CUSTOS = {
    "cadastro_basico": 0.16,
    "pis":             0.36,
    "titulo":          0.72,
}

# Audit append-only — todas as chamadas pra rastrear custo + efetividade.
# Usa o mesmo diretório do módulo (igual ao econtador_audit.ndjson).
AUDIT_FILE = Path(__file__).parent / "directdata_audit.ndjson"

# v2.14.1 (ITEM 9) — cache negativo persistente em disco. Bug real 12/06:
# 194 chamadas pagas pra ~45 admissões = retentativa paga em CPF que
# falhou. Agora CPF que falha (qualquer API) não é reconsultado por 7 dias.
NEG_CACHE_FILE = Path(__file__).parent / "directdata_neg_cache.json"
NEG_CACHE_DIAS = 7

# v2.14.1: latência mínima esperada pra detectar exception engolida.
# Caso real: Cadastro Básico média 3ms = exceção local instantânea, não
# resposta da API. Abaixo desse limiar logamos warning específico.
LATENCIA_MIN_REAL_MS = 50


def _mascarar_cpf(cpf: str) -> str:
    """Mostra apenas os últimos 4 dígitos pra privacidade nos logs."""
    d = "".join(c for c in str(cpf or "") if c.isdigit())
    return f"***{d[-4:]}" if len(d) >= 4 else "***"


def _audit_write(entry: dict) -> None:
    """Append uma linha JSON em directdata_audit.ndjson. Nunca quebra fluxo."""
    entry.setdefault("timestamp", datetime.now().isoformat())
    try:
        AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        log.debug(f"audit write falhou: {e}")


def _so_digitos(s: Any) -> str:
    """Remove tudo que não for dígito."""
    return "".join(c for c in str(s or "") if c.isdigit())


# ── Cache negativo (v2.14.1 ITEM 9) ──────────────────────────────

def _carregar_cache_neg() -> dict:
    """{api: {cpf: ts_iso}} — registra CPFs que falharam por API. Persiste
    em disco entre runs pra valer entre passadas do polling."""
    if not NEG_CACHE_FILE.exists():
        return {}
    try:
        data = json.loads(NEG_CACHE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as e:
        log.warning(f"directdata neg_cache lendo: {e}")
        return {}


def _salvar_cache_neg(data: dict) -> None:
    try:
        NEG_CACHE_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as e:
        log.warning(f"directdata neg_cache salvando: {e}")


def _esta_em_neg_cache(api: str, cpf_d: str) -> bool:
    """True se este (api, cpf) falhou nos últimos NEG_CACHE_DIAS."""
    if not cpf_d:
        return False
    data = _carregar_cache_neg()
    ts_str = (data.get(api) or {}).get(cpf_d)
    if not ts_str:
        return False
    try:
        ts = datetime.fromisoformat(ts_str)
    except ValueError:
        return False
    return (datetime.now() - ts) < timedelta(days=NEG_CACHE_DIAS)


def _marcar_neg_cache(api: str, cpf_d: str) -> None:
    """Marca o CPF como falho pra esta API. Best-effort."""
    if not cpf_d:
        return
    try:
        data = _carregar_cache_neg()
        data.setdefault(api, {})[cpf_d] = datetime.now().isoformat(timespec="seconds")
        _salvar_cache_neg(data)
    except Exception:  # noqa: BLE001
        pass


class DirectDataClient:
    """Cliente das APIs Direct Data. Cache em memória + skip por economia.

    Uso:
        client = DirectDataClient()  # lê token do .env
        if not client.habilitado:
            return  # token não configurado, segue sem enriquecimento

        dados = client.cadastro_basico("85788774543")
        if dados:
            ...
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        pis_habilitado: bool = False,
        titulo_habilitado: bool = False,
    ):
        # Token pode vir explícito (testes) ou do .env (produção). Sem token,
        # o cliente fica desabilitado — chamadas retornam {} sem bater na API.
        self.token = token or os.getenv("DIRECTDATA_TOKEN") or ""
        self.habilitado = bool(self.token)
        if not self.habilitado:
            log.debug(
                "DirectData: sem DIRECTDATA_TOKEN no .env — enriquecimento desabilitado"
            )

        # v2.14.1 (ITEM 9): PIS e TSE OFF por default — sucesso < 30% no mês
        # (PIS 7%, TSE 30%). Caller (enrichment.py) passa as flags do config.
        self.pis_habilitado = bool(pis_habilitado)
        self.titulo_habilitado = bool(titulo_habilitado)

        # Cache em RAM por CPF — economiza R$ em reprocessamentos
        self._cache_cadastro: dict[str, dict] = {}
        self._cache_pis: dict[str, dict] = {}
        self._cache_titulo: dict[str, dict] = {}

        # Métricas de custo da sessão (pra log final)
        self.custo_acumulado_brl: float = 0.0
        self.chamadas_feitas: dict[str, int] = {"cadastro": 0, "pis": 0, "titulo": 0}

    def _get(self, url: str, timeout: float = 20.0) -> tuple[dict, int, int]:
        """Chamada GET defensiva. Nunca quebra fluxo.

        v2.14.1 (ITEM 9): latência real é logada SEMPRE; quando <50ms num
        endpoint HTTP, é praticamente certo que houve exceção engolida
        (auth fail, DNS, SSL) — caso real "3ms" do Cadastro Básico.

        Returns:
            (data, status_code, duration_ms)
            - data: dict do retorno (vazio em falha)
            - status_code: 0 em exceção, senão HTTP code
            - duration_ms: tempo da chamada
        """
        t0 = time.perf_counter()
        try:
            with httpx.Client(timeout=timeout) as client:
                r = client.get(url, headers={"Accept": "application/json"})
                duration_ms = int((time.perf_counter() - t0) * 1000)
                # v2.14.1: latência implausivelmente baixa = sinal de falha local
                if duration_ms < LATENCIA_MIN_REAL_MS:
                    log.warning(
                        f"DirectData latência {duration_ms}ms em HTTP "
                        f"(< {LATENCIA_MIN_REAL_MS}ms) — possível auth/conexão "
                        f"falhando silenciosa. URL={url[:80]}... status={r.status_code}"
                    )
                if r.status_code != 200:
                    log.warning(
                        f"DirectData HTTP {r.status_code} em {url[:80]}... "
                        f"body={r.text[:200]}"
                    )
                    return {}, r.status_code, duration_ms
                try:
                    data = r.json()
                except (ValueError, json.JSONDecodeError) as e:
                    log.warning(
                        f"DirectData 200 mas JSON inválido: {e} — body={r.text[:200]}"
                    )
                    return {}, r.status_code, duration_ms
                ret = (data.get("retorno") if isinstance(data, dict)
                       and "retorno" in data else data) or {}
                return ret, r.status_code, duration_ms
        except Exception as e:
            duration_ms = int((time.perf_counter() - t0) * 1000)
            # v2.14.1: log explícito com tipo da exceção (já existia, mas
            # agora dá pra correlacionar com latência baixa)
            log.warning(
                f"DirectData exception ({type(e).__name__}): {e} "
                f"[duration={duration_ms}ms, url={url[:80]}...]"
            )
            return {}, 0, duration_ms

    def cadastro_basico(self, cpf: str) -> dict:
        """API 1 — R$ 0,16 — nome, mãe, nascimento, sexo, endereço.

        Retorno (cache hit não conta como chamada paga):
            {nome, sexo, dataNascimento, nomeMae, enderecos: [...], ...}
        """
        if not self.habilitado:
            return {}
        cpf_d = _so_digitos(cpf)
        if len(cpf_d) != 11:
            return {}

        corr_id = uuid.uuid4().hex[:8]
        cpf_mask = _mascarar_cpf(cpf_d)

        # Cache hit — não conta como chamada paga
        if cpf_d in self._cache_cadastro:
            _audit_write({
                "corr_id": corr_id, "api": "cadastro_basico", "cpf": cpf_mask,
                "cache_hit": True, "custo_brl": 0.0, "duration_ms": 0,
                "success": True, "campos_uteis": list(self._cache_cadastro[cpf_d].keys())[:10],
            })
            return dict(self._cache_cadastro[cpf_d])

        # v2.14.1 (ITEM 9): cache negativo — evita retentativa paga em CPF
        # que falhou nos últimos 7 dias (caso real 12/06: 194 chamadas pagas
        # pra ~45 admissões).
        if _esta_em_neg_cache("cadastro_basico", cpf_d):
            log.info(
                f"[directdata] CadastroBasico CPF={cpf_mask} pulado "
                f"(neg_cache — falhou nos últimos {NEG_CACHE_DIAS}d)"
            )
            _audit_write({
                "corr_id": corr_id, "api": "cadastro_basico", "cpf": cpf_mask,
                "neg_cache_hit": True, "custo_brl": 0.0, "duration_ms": 0,
                "success": False,
            })
            return {}

        url = f"{BASE_URL}/CadastroPessoaFisica?CPF={cpf_d}&TOKEN={self.token}"
        log.info(f"[directdata] CadastroBasico CPF={cpf_mask} | R$ {CUSTOS['cadastro_basico']:.2f}")
        dados, status, duration_ms = self._get(url, timeout=20.0)

        self._cache_cadastro[cpf_d] = dict(dados)
        custo = CUSTOS["cadastro_basico"]
        self.custo_acumulado_brl += custo
        self.chamadas_feitas["cadastro"] += 1

        # v2.14.1: registra no cache negativo se falhou
        if not dados or status != 200:
            _marcar_neg_cache("cadastro_basico", cpf_d)

        # Efetividade: quantos campos úteis vieram
        campos_uteis = [k for k in (
            "nome", "nomeMae", "dataNascimento", "sexo", "enderecos",
            "telefones", "emails",
        ) if dados.get(k)]
        _audit_write({
            "corr_id": corr_id, "api": "cadastro_basico", "cpf": cpf_mask,
            "cache_hit": False, "custo_brl": custo, "duration_ms": duration_ms,
            "success": bool(dados) and status == 200, "status_code": status,
            "campos_uteis": campos_uteis,
        })
        return dados

    def pis(self, cpf: str) -> dict:
        """API 3 — R$ 0,36 — número do PIS.

        v2.14.1 (ITEM 9): OFF por default (sucesso 7% no mês 06/2026 — R$ 14
        gastos sem retorno). Ligar via config.directdata_pis_habilitado.

        Retorno: {pis, cpf, nome, nomeMae, dataNascimento}
        """
        if not self.habilitado:
            return {}
        if not self.pis_habilitado:
            log.debug("[directdata] PIS desabilitado por flag — pulando")
            return {}
        cpf_d = _so_digitos(cpf)
        if len(cpf_d) != 11:
            return {}

        corr_id = uuid.uuid4().hex[:8]
        cpf_mask = _mascarar_cpf(cpf_d)

        if cpf_d in self._cache_pis:
            _audit_write({
                "corr_id": corr_id, "api": "pis", "cpf": cpf_mask,
                "cache_hit": True, "custo_brl": 0.0, "duration_ms": 0,
                "success": True,
            })
            return dict(self._cache_pis[cpf_d])

        if _esta_em_neg_cache("pis", cpf_d):
            log.info(f"[directdata] PIS CPF={cpf_mask} pulado (neg_cache)")
            _audit_write({
                "corr_id": corr_id, "api": "pis", "cpf": cpf_mask,
                "neg_cache_hit": True, "custo_brl": 0.0, "duration_ms": 0,
                "success": False,
            })
            return {}

        url = f"{BASE_URL}/MinisterioTrabalhoPIS?CPF={cpf_d}&TOKEN={self.token}"
        log.info(f"[directdata] PIS CPF={cpf_mask} | R$ {CUSTOS['pis']:.2f}")
        dados, status, duration_ms = self._get(url, timeout=30.0)

        self._cache_pis[cpf_d] = dict(dados)
        custo = CUSTOS["pis"]
        self.custo_acumulado_brl += custo
        self.chamadas_feitas["pis"] += 1

        if not dados.get("pis") or status != 200:
            _marcar_neg_cache("pis", cpf_d)

        _audit_write({
            "corr_id": corr_id, "api": "pis", "cpf": cpf_mask,
            "cache_hit": False, "custo_brl": custo, "duration_ms": duration_ms,
            "success": bool(dados.get("pis")) and status == 200,
            "status_code": status,
            "pis_encontrado": bool(dados.get("pis")),
        })
        return dados

    def titulo_eleitor(
        self,
        cpf: str,
        nome_mae: str,
        data_nascimento_br: str,
    ) -> dict:
        """API 4 — R$ 0,72 — título, zona, seção. LATÊNCIA ALTA (~40s).

        v2.14.1 (ITEM 9): OFF por default (sucesso 30% no mês 06/2026, 37s
        de latência média). Ligar via config.directdata_titulo_habilitado.

        Args:
            cpf: CPF do eleitor
            nome_mae: nome completo da mãe (vem da API 1)
            data_nascimento_br: data no formato DD/MM/AAAA (vem da API 1)

        Retorno: {identificacao, domicilioEleitoral, biometriaColetada, status}
        Quando eleitor não cadastrado: status descritivo + inscricao=null.
        """
        if not self.habilitado:
            return {}
        if not self.titulo_habilitado:
            log.debug("[directdata] TSE desabilitado por flag — pulando")
            return {}
        cpf_d = _so_digitos(cpf)
        if len(cpf_d) != 11 or not nome_mae or not data_nascimento_br:
            return {}

        # Cache key: CPF (mãe e data são derivados do CPF, então CPF basta)
        corr_id = uuid.uuid4().hex[:8]
        cpf_mask = _mascarar_cpf(cpf_d)

        if cpf_d in self._cache_titulo:
            _audit_write({
                "corr_id": corr_id, "api": "titulo", "cpf": cpf_mask,
                "cache_hit": True, "custo_brl": 0.0, "duration_ms": 0,
                "success": True,
            })
            return dict(self._cache_titulo[cpf_d])

        if _esta_em_neg_cache("titulo", cpf_d):
            log.info(f"[directdata] TSE CPF={cpf_mask} pulado (neg_cache)")
            _audit_write({
                "corr_id": corr_id, "api": "titulo", "cpf": cpf_mask,
                "neg_cache_hit": True, "custo_brl": 0.0, "duration_ms": 0,
                "success": False,
            })
            return {}

        url = (
            f"{BASE_URL}/TituloLocalVotacao"
            f"?CPF={cpf_d}"
            f"&NOMEMAE={quote(nome_mae)}"
            f"&DATANASCIMENTO={quote(data_nascimento_br)}"
            f"&TOKEN={self.token}"
        )
        log.info(f"[directdata] TSE CPF={cpf_mask} | R$ {CUSTOS['titulo']:.2f} (latência ~40s)")
        dados, status, duration_ms = self._get(url, timeout=60.0)

        self._cache_titulo[cpf_d] = dict(dados)
        custo = CUSTOS["titulo"]
        self.custo_acumulado_brl += custo
        self.chamadas_feitas["titulo"] += 1

        # Eleitor cadastrado? Útil pra medir efetividade
        identificacao = (dados.get("identificacao") or {})
        eleitor_encontrado = bool(identificacao.get("inscricao"))
        if not eleitor_encontrado or status != 200:
            _marcar_neg_cache("titulo", cpf_d)
        _audit_write({
            "corr_id": corr_id, "api": "titulo", "cpf": cpf_mask,
            "cache_hit": False, "custo_brl": custo, "duration_ms": duration_ms,
            "success": status == 200,
            "status_code": status,
            "eleitor_encontrado": eleitor_encontrado,
        })
        return dados

    def resumo_custo(self) -> str:
        """Resumo formatado pra log de billing no fim da passada."""
        return (
            f"DirectData sessão: "
            f"{self.chamadas_feitas['cadastro']}x Cadastro + "
            f"{self.chamadas_feitas['pis']}x PIS + "
            f"{self.chamadas_feitas['titulo']}x TSE "
            f"= R$ {self.custo_acumulado_brl:.2f}"
        )


def sum_directdata_mes_atual() -> dict:
    """Agrega chamadas Direct Data do mês corrente lendo directdata_audit.ndjson.

    Retorna:
        {
            "n_chamadas": int,       # total real (sem cache hits)
            "n_cache_hits": int,     # cache hits (gratuitos)
            "custo_brl": float,      # custo total do mês
            "por_api": {
                "cadastro_basico": {"n": int, "custo_brl": float, "sucesso": int},
                "pis":             {...},
                "titulo":          {...},
            },
            "taxa_sucesso": float,   # 0.0 a 1.0
            "taxa_cache_hit": float, # 0.0 a 1.0
            "duracao_media_ms": dict # por api
        }
    """
    out = {
        "n_chamadas": 0, "n_cache_hits": 0, "custo_brl": 0.0,
        "por_api": {
            "cadastro_basico": {"n": 0, "custo_brl": 0.0, "sucesso": 0, "duracoes": []},
            "pis":             {"n": 0, "custo_brl": 0.0, "sucesso": 0, "duracoes": []},
            "titulo":          {"n": 0, "custo_brl": 0.0, "sucesso": 0, "duracoes": []},
        },
        "taxa_sucesso": 0.0, "taxa_cache_hit": 0.0,
        "duracao_media_ms": {},
    }
    if not AUDIT_FILE.exists():
        return out

    mes_atual = datetime.now().strftime("%Y-%m")
    n_sucesso_total = 0
    try:
        with open(AUDIT_FILE, "r", encoding="utf-8") as f:
            for linha in f:
                try:
                    e = json.loads(linha)
                except json.JSONDecodeError:
                    continue
                ts = e.get("timestamp", "")
                if not ts.startswith(mes_atual):
                    continue
                api = e.get("api", "")
                if api not in out["por_api"]:
                    continue
                if e.get("cache_hit"):
                    out["n_cache_hits"] += 1
                    continue
                out["n_chamadas"] += 1
                out["custo_brl"] += float(e.get("custo_brl") or 0)
                out["por_api"][api]["n"] += 1
                out["por_api"][api]["custo_brl"] += float(e.get("custo_brl") or 0)
                if e.get("success"):
                    out["por_api"][api]["sucesso"] += 1
                    n_sucesso_total += 1
                if e.get("duration_ms"):
                    out["por_api"][api]["duracoes"].append(int(e["duration_ms"]))
    except OSError:
        return out

    # Calcula médias e taxas
    total_eventos = out["n_chamadas"] + out["n_cache_hits"]
    if total_eventos > 0:
        out["taxa_cache_hit"] = out["n_cache_hits"] / total_eventos
    if out["n_chamadas"] > 0:
        out["taxa_sucesso"] = n_sucesso_total / out["n_chamadas"]
    for api, info in out["por_api"].items():
        duracoes = info.pop("duracoes", [])
        if duracoes:
            out["duracao_media_ms"][api] = int(sum(duracoes) / len(duracoes))
        info["custo_brl"] = round(info["custo_brl"], 2)
    out["custo_brl"] = round(out["custo_brl"], 2)
    out["taxa_sucesso"] = round(out["taxa_sucesso"], 4)
    out["taxa_cache_hit"] = round(out["taxa_cache_hit"], 4)
    return out
