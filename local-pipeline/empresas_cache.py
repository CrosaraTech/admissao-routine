"""empresas_cache.py — cache em memória dos CNPJs cadastrados no eContador.

Usado pra auto-corrigir CNPJs com typo (caso real: ASO da clínica escreveu
"09.401.921/0001-79" em vez de "09.491.921/0001-79"). Quando GET /empresas
falha pro CNPJ extraído, geramos variações de 1-2 dígitos e checamos contra
a whitelist local — se 1 candidato bate, usa.

Design:
  - Tudo em RAM (sem arquivo) — repopula a cada startup do AdmitER (~30s pra
    paginar ~500 empresas). Portável: roda igual em qualquer máquina sem
    state local. Funciona offline depois de carregado.
  - Lookup O(1) em set. Geração de variações: até ~7k candidatos testados
    em <50ms — instantâneo.
  - Defesa em camadas:
      Camada 1: GET /empresas?cpfcnpj=<extraido>     (fluxo atual)
      Camada 2: se falhou → buscar_candidatos_no_cache  (esta nova)
      Camada 3: se ainda nada → pendência interna     (fluxo atual)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from itertools import product

from cnpj_utils import so_digitos, validar_cnpj

log = logging.getLogger("admissao.empresas_cache")


@dataclass
class EmpresasCache:
    """Cache em memória. Funciona como set otimizado pra lookup + variações.

    cnpjs: set[str] de CNPJs válidos (14 dígitos puros, sem formatação)
    detalhes: dict[cnpj → {empresa_id, razao_social}] pra logs amigáveis
    """
    cnpjs: set[str] = field(default_factory=set)
    detalhes: dict[str, dict] = field(default_factory=dict)
    carregado: bool = False

    def __len__(self) -> int:
        return len(self.cnpjs)

    def contem(self, cnpj: str) -> bool:
        """Check O(1) — aceita CNPJ com ou sem formatação."""
        return so_digitos(cnpj) in self.cnpjs

    def info(self, cnpj: str) -> dict | None:
        """Retorna {empresa_id, razao_social} se cacheado."""
        return self.detalhes.get(so_digitos(cnpj))


def carregar_empresas_do_econtador(api, log_progresso: bool = True) -> EmpresasCache:
    """Pagina GET /empresas e popula o cache. Robust: erro em uma página NÃO
    perde as anteriores — degrada graciosamente.

    Args:
        api: instância de EContadorAPI já autenticada
        log_progresso: loga "carregadas X empresas" a cada página

    Returns:
        EmpresasCache populado. cache.carregado=True só se a paginação completou.
    """
    cache = EmpresasCache()
    try:
        for empresa in api.iterar_todas_empresas(log_progresso=log_progresso):
            cnpj_d = so_digitos(empresa.get("cpfcnpj") or empresa.get("cnpj"))
            # v2.16.10: CNPJ com 13 dígitos = zero à esquerda perdido
            # (API eContador serializa cpfcnpj como int em algum lugar e
            # perde o zero inicial). 79 das primeiras 200 empresas têm
            # esse problema. Preende zero pra restaurar os 14 dígitos.
            if len(cnpj_d) == 13:
                cnpj_d = "0" + cnpj_d
            # 11 = CPF (empresa pessoa física — produtor rural, etc.);
            # 14 = CNPJ (pessoa jurídica). Outros tamanhos = lixo.
            if len(cnpj_d) not in (11, 14):
                continue
            cache.cnpjs.add(cnpj_d)
            cache.detalhes[cnpj_d] = {
                "empresa_id": str(empresa.get("id") or ""),
                "razao_social": empresa.get("nome") or empresa.get("razao_social") or "",
            }
        cache.carregado = True
        log.info(f"✓ Cache de empresas carregado: {len(cache)} CNPJs no whitelist")
    except Exception as e:
        log.warning(
            f"⚠ Falha carregando cache de empresas: {type(e).__name__}: {e}. "
            f"Pipeline continua, mas auto-correção de CNPJ fica desabilitada."
        )
    return cache


def buscar_candidatos_no_cache(
    cnpj_lido: str,
    cache: EmpresasCache,
    max_digitos_modificados: int = 2,
) -> list[str]:
    """Gera variações do CNPJ lido (1 ou 2 dígitos modificados) e retorna
    a lista dos que estão no cache E passam validação DV.

    Args:
        cnpj_lido: CNPJ extraído (com ou sem formatação)
        cache: EmpresasCache populado
        max_digitos_modificados: 1 (rápido, 126 candidatos) ou 2 (até 7.371)

    Returns:
        Lista de CNPJs válidos (14 dígitos, sem formatação) que estão no cache.
        Vazia se nada bateu. Pode ter >1 elemento se houver ambiguidade.
    """
    cnpj_d = so_digitos(cnpj_lido)
    if len(cnpj_d) != 14:
        return []

    # Match exato primeiro — atalho gratuito
    if cnpj_d in cache.cnpjs:
        return [cnpj_d]

    encontrados: list[str] = []

    # 1 dígito modificado: 14 posições × 9 substituições = 126 candidatos
    for pos in range(14):
        for novo_digito in "0123456789":
            if novo_digito == cnpj_d[pos]:
                continue
            candidato = cnpj_d[:pos] + novo_digito + cnpj_d[pos + 1:]
            if candidato in cache.cnpjs and validar_cnpj(candidato):
                encontrados.append(candidato)

    if encontrados or max_digitos_modificados < 2:
        return _unicos(encontrados)

    # 2 dígitos modificados: até 7.371 candidatos — ainda <50ms num set
    for pos1 in range(14):
        for pos2 in range(pos1 + 1, 14):
            for d1, d2 in product("0123456789", repeat=2):
                if d1 == cnpj_d[pos1] and d2 == cnpj_d[pos2]:
                    continue
                candidato = (
                    cnpj_d[:pos1] + d1 + cnpj_d[pos1 + 1:pos2] + d2 + cnpj_d[pos2 + 1:]
                )
                if candidato in cache.cnpjs and validar_cnpj(candidato):
                    encontrados.append(candidato)

    return _unicos(encontrados)


def _unicos(lst: list[str]) -> list[str]:
    """Preserva ordem mas remove duplicatas."""
    visto: set[str] = set()
    out: list[str] = []
    for x in lst:
        if x not in visto:
            visto.add(x)
            out.append(x)
    return out


def corrigir_cnpj_via_cache(
    cnpj_lido: str,
    cache: EmpresasCache,
) -> tuple[str | None, str]:
    """Wrapper de alto nível: retorna (cnpj_corrigido, motivo).

    Returns:
        (cnpj_correto, motivo) — usado direto pelo pipeline
        (None, motivo)         — não conseguiu resolver

    Motivos possíveis:
        - "exato"          — CNPJ extraído já está no cache
        - "corrigido_1d"   — 1 candidato com 1 dígito modificado
        - "corrigido_2d"   — 1 candidato com 2 dígitos modificados
        - "ambiguo"        — múltiplos candidatos batem (lista no log)
        - "nao_encontrado" — nenhuma variação no cache
        - "cache_vazio"    — cache ainda não foi populado
    """
    if not cache.carregado or len(cache) == 0:
        return None, "cache_vazio"

    cnpj_d = so_digitos(cnpj_lido)
    if cnpj_d in cache.cnpjs:
        return cnpj_d, "exato"

    # Tenta 1 dígito primeiro (mais provável)
    candidatos_1d = buscar_candidatos_no_cache(cnpj_lido, cache, max_digitos_modificados=1)
    if len(candidatos_1d) == 1:
        return candidatos_1d[0], "corrigido_1d"
    if len(candidatos_1d) > 1:
        log.warning(f"   ⚠ CNPJ '{cnpj_lido}' tem {len(candidatos_1d)} candidatos no cache (1d): {candidatos_1d}")
        return None, "ambiguo"

    # Tenta 2 dígitos (mais caro mas ainda rápido)
    candidatos_2d = buscar_candidatos_no_cache(cnpj_lido, cache, max_digitos_modificados=2)
    if len(candidatos_2d) == 1:
        return candidatos_2d[0], "corrigido_2d"
    if len(candidatos_2d) > 1:
        log.warning(f"   ⚠ CNPJ '{cnpj_lido}' tem {len(candidatos_2d)} candidatos no cache (2d): {candidatos_2d}")
        return None, "ambiguo"

    return None, "nao_encontrado"
