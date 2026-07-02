"""Testes dos clamps de payload (PATCHES.md §4.2 — v2.14.1).

Cada caso aqui é um HTTP 422 REAL observado na produção em 12/06/2026:
  1. complemento >40 chars (Size: 0-40)
  2. tituloeleitor >12 dígitos (Max: 999999999999)
  3. celular com 11 dígitos sem DDI (Size: 12-13) — Claude extraiu "62999990000"

Todos viram regressão: se a tabela de clamps for relaxada sem postmortem
novo, esses 3 testes quebram e a release é bloqueada.
"""
from __future__ import annotations

import unittest

from payload_builder import sanitizar_attributes, finalizar_payload
from departamento import SEM_DEPARTAMENTO


class TestClampsHTTP422(unittest.TestCase):
    """Cada teste reproduz o input que gerou 422 e confere que sai certinho."""

    def test_complemento_truncado_em_40(self):
        # Caso real 12/06: ViaCEP devolveu "AO LADO DO POSTO X NA ENTRADA DO BAIRRO"
        # (41 chars) + Claude adicionou "Q. 12 L. 03" → 52 chars.
        longo = "AO LADO DO POSTO X NA ENTRADA DO BAIRRO Q12 L03"
        self.assertEqual(len(longo), 47)
        attrs_in = {"complemento": longo}
        attrs_out = sanitizar_attributes(attrs_in)
        # ≤ 40 (truncamento + strip pode comer espaço terminal → 39).
        # O importante é não estourar 40 (constraint Size do eContador).
        self.assertLessEqual(len(attrs_out["complemento"]), 40)
        # Mantém o início (mais informativo)
        self.assertTrue(attrs_out["complemento"].startswith("AO LADO DO POSTO X"))

    def test_complemento_curto_intacto(self):
        attrs_out = sanitizar_attributes({"complemento": "APTO 101"})
        self.assertEqual(attrs_out["complemento"], "APTO 101")

    def test_complemento_exatamente_40_intacto(self):
        s = "X" * 40
        attrs_out = sanitizar_attributes({"complemento": s})
        self.assertEqual(attrs_out["complemento"], s)

    def test_tituloeleitor_int_quando_12_digitos(self):
        # Caso real: "012345678901" (12 dígitos como string com zero à esquerda).
        # eContador exige Max(999999999999) — STRING quebra a validação Max.
        attrs_out = sanitizar_attributes({"tituloeleitor": "012345678901"})
        self.assertEqual(attrs_out["tituloeleitor"], 12345678901)
        self.assertIsInstance(attrs_out["tituloeleitor"], int)

    def test_tituloeleitor_omitido_quando_excede_12(self):
        # Caso real 12/06: Claude concatenou inscrição+zona+seção (>12 dígitos)
        attrs_out = sanitizar_attributes({"tituloeleitor": "01234567890123"})
        self.assertNotIn("tituloeleitor", attrs_out)

    def test_tituloeleitor_aceita_com_pontuacao(self):
        attrs_out = sanitizar_attributes({"tituloeleitor": "1234.5678.0123"})
        self.assertEqual(attrs_out["tituloeleitor"], 123456780123)

    def test_celular_11_digitos_recebe_prefixo_55(self):
        # Caso real RAIMUNDO 09/06: Claude mandou "62999990000" (11 chars,
        # DDD+celular sem DDI). eContador valida Size(min=12, max=13) — 422.
        attrs_out = sanitizar_attributes({"celular": "62999990000"})
        self.assertEqual(attrs_out["celular"], "5562999990000")
        self.assertEqual(len(attrs_out["celular"]), 13)

    def test_celular_10_digitos_recebe_prefixo_55(self):
        attrs_out = sanitizar_attributes({"celular": "6232310000"})
        self.assertEqual(attrs_out["celular"], "556232310000")
        self.assertEqual(len(attrs_out["celular"]), 12)

    def test_celular_13_digitos_com_ddi_intacto(self):
        attrs_out = sanitizar_attributes({"celular": "5562999990000"})
        self.assertEqual(attrs_out["celular"], "5562999990000")

    def test_celular_formatado_com_parens_e_hifens(self):
        # "(62) 99999-0000" tem 11 dígitos puros → vira 5562999990000
        attrs_out = sanitizar_attributes({"celular": "(62) 99999-0000"})
        self.assertEqual(attrs_out["celular"], "5562999990000")

    def test_celular_invalido_omitido(self):
        # 8 dígitos só (sem DDD) — não dá pra inferir, omite (é opcional).
        attrs_out = sanitizar_attributes({"celular": "99990000"})
        self.assertNotIn("celular", attrs_out)

    def test_telefone_aplica_mesma_regra(self):
        attrs_out = sanitizar_attributes({"telefone": "6232310000"})
        self.assertEqual(attrs_out["telefone"], "556232310000")


class TestRelationshipDepartamentoSentinela(unittest.TestCase):
    """PATCHES.md §4.1 — quando resolver_departamento devolve SEM_DEPARTAMENTO,
    o payload final NÃO deve ter a relationship 'departamento' inteira.
    Resolve 6 das 11 pendências internas abertas em 12/06."""

    def _payload_base(self) -> dict:
        return {
            "data": {
                "type": "candidatos",
                "attributes": {
                    "nome": "TESTE COBAIA",
                    "cpf": 12345678901,
                    "admissao": "2026-06-13",
                    "salario": 1500.0,
                },
                "relationships": {},
            }
        }

    def test_sem_departamento_omite_relationship(self):
        out = finalizar_payload(
            self._payload_base(),
            empresa_id="89", departamento_id=SEM_DEPARTAMENTO, funcao_id="123",
        )
        rels = out["data"]["relationships"]
        self.assertNotIn("departamento", rels)
        self.assertEqual(rels["empresa"]["data"]["id"], "89")
        self.assertEqual(rels["funcao"]["data"]["id"], "123")

    def test_departamento_id_normal_cria_relationship(self):
        out = finalizar_payload(
            self._payload_base(),
            empresa_id="89", departamento_id="245", funcao_id="123",
        )
        self.assertEqual(
            out["data"]["relationships"]["departamento"]["data"]["id"],
            "245",
        )

    def test_departamento_id_none_tambem_omite(self):
        out = finalizar_payload(
            self._payload_base(),
            empresa_id="89", departamento_id=None, funcao_id="123",
        )
        self.assertNotIn("departamento", out["data"]["relationships"])


if __name__ == "__main__":
    unittest.main()
