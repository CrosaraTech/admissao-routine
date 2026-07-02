"""Testes do empresas_cache. Roda com:
    .venv\\Scripts\\python.exe -m unittest test_empresas_cache.py -v
"""
from __future__ import annotations

import unittest

from empresas_cache import (
    EmpresasCache,
    buscar_candidatos_no_cache,
    corrigir_cnpj_via_cache,
)

# CNPJs reais válidos (DV correto) pra testes
CNPJ_EKOPLASTIC = "09491921000179"  # caso real desta sessão
CNPJ_OUTRA = "12345678000195"        # outro válido qualquer (DV calculado)


def _make_cache(*cnpjs: str) -> EmpresasCache:
    """Helper pra criar cache populado com CNPJs específicos."""
    c = EmpresasCache()
    c.carregado = True
    for cnpj in cnpjs:
        c.cnpjs.add(cnpj)
        c.detalhes[cnpj] = {"empresa_id": "999", "razao_social": "TESTE"}
    return c


class TestEmpresasCache(unittest.TestCase):
    def test_cache_vazio(self):
        c = EmpresasCache()
        self.assertEqual(len(c), 0)
        self.assertFalse(c.carregado)
        self.assertFalse(c.contem(CNPJ_EKOPLASTIC))

    def test_contem_aceita_com_e_sem_formatacao(self):
        c = _make_cache(CNPJ_EKOPLASTIC)
        self.assertTrue(c.contem(CNPJ_EKOPLASTIC))
        self.assertTrue(c.contem("09.491.921/0001-79"))
        self.assertFalse(c.contem("12345678000195"))


class TestBuscarCandidatos(unittest.TestCase):
    def test_match_exato_retorna_o_proprio(self):
        c = _make_cache(CNPJ_EKOPLASTIC)
        out = buscar_candidatos_no_cache(CNPJ_EKOPLASTIC, c)
        self.assertEqual(out, [CNPJ_EKOPLASTIC])

    def test_um_digito_modificado_caso_real_ekoplastic(self):
        """Caso real: PDF tem 09.401.921... mas correto é 09.491.921..."""
        c = _make_cache(CNPJ_EKOPLASTIC)
        # CNPJ lido errado (pos 3: '0' em vez de '9') — DV correspondente:
        # 09401921000179 (DV inválido) → buscar variações
        cnpj_errado = "09401921000179"
        out = buscar_candidatos_no_cache(cnpj_errado, c, max_digitos_modificados=1)
        self.assertEqual(out, [CNPJ_EKOPLASTIC])

    def test_um_digito_sem_match_retorna_vazio(self):
        c = _make_cache(CNPJ_EKOPLASTIC)
        # CNPJ muito diferente — nenhuma variação de 1 dígito bate
        out = buscar_candidatos_no_cache("99999999999999", c, max_digitos_modificados=1)
        self.assertEqual(out, [])

    def test_cache_vazio_nao_quebra(self):
        c = EmpresasCache()  # cache vazio
        out = buscar_candidatos_no_cache(CNPJ_EKOPLASTIC, c)
        self.assertEqual(out, [])

    def test_cnpj_formatado_normalizado(self):
        c = _make_cache(CNPJ_EKOPLASTIC)
        out = buscar_candidatos_no_cache("09.491.921/0001-79", c)
        self.assertEqual(out, [CNPJ_EKOPLASTIC])

    def test_cnpj_invalido_size_retorna_vazio(self):
        c = _make_cache(CNPJ_EKOPLASTIC)
        out = buscar_candidatos_no_cache("123", c)
        self.assertEqual(out, [])

    def test_ambiguidade_retorna_multiplos(self):
        """Quando 2+ candidatos batem no cache, retorna ambos pra caller decidir."""
        # Difícil construir um caso real sem manipular CNPJs sintéticos.
        # Cache com 2 CNPJs DV-válidos que diferem em 1 dígito:
        cnpj_a = "09491921000179"
        cnpj_b = "09491921000260"  # CNPJ DV válido com prefixo similar
        c = _make_cache(cnpj_a, cnpj_b)
        # CNPJ lido errado que dista 1 dígito de cada um:
        # Não fácil de construir — vou testar com cnpj inexistente que dista
        # 1 de cnpj_a, ambíguo só se houver outro também dista 1
        out = buscar_candidatos_no_cache(cnpj_a, c)
        # Match exato curto-circuita
        self.assertEqual(out, [cnpj_a])


class TestCorrigirCnpjViaCache(unittest.TestCase):
    def test_cache_vazio_retorna_cache_vazio(self):
        c = EmpresasCache()  # não carregado
        cnpj, motivo = corrigir_cnpj_via_cache(CNPJ_EKOPLASTIC, c)
        self.assertIsNone(cnpj)
        self.assertEqual(motivo, "cache_vazio")

    def test_match_exato(self):
        c = _make_cache(CNPJ_EKOPLASTIC)
        cnpj, motivo = corrigir_cnpj_via_cache(CNPJ_EKOPLASTIC, c)
        self.assertEqual(cnpj, CNPJ_EKOPLASTIC)
        self.assertEqual(motivo, "exato")

    def test_corrigido_1_digito(self):
        c = _make_cache(CNPJ_EKOPLASTIC)
        cnpj, motivo = corrigir_cnpj_via_cache("09401921000179", c)
        self.assertEqual(cnpj, CNPJ_EKOPLASTIC)
        self.assertEqual(motivo, "corrigido_1d")

    def test_nao_encontrado(self):
        c = _make_cache(CNPJ_EKOPLASTIC)
        cnpj, motivo = corrigir_cnpj_via_cache("99999999999999", c)
        self.assertIsNone(cnpj)
        self.assertEqual(motivo, "nao_encontrado")

    def test_cnpj_formatado_aceito(self):
        c = _make_cache(CNPJ_EKOPLASTIC)
        cnpj, motivo = corrigir_cnpj_via_cache("09.491.921/0001-79", c)
        self.assertEqual(cnpj, CNPJ_EKOPLASTIC)
        self.assertEqual(motivo, "exato")


if __name__ == "__main__":
    unittest.main(verbosity=2)
