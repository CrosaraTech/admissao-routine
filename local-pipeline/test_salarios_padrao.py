"""Testes pra módulo salarios_padrao (v2.12.0)."""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import salarios_padrao as sp


class TestSalariosPadrao(unittest.TestCase):
    def setUp(self):
        self.tmp_file = Path(__file__).parent / "_test_salarios_padrao.json"
        if self.tmp_file.exists():
            self.tmp_file.unlink()
        self._patcher = patch.object(sp, "SALARIOS_PADRAO_FILE", self.tmp_file)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        if self.tmp_file.exists():
            self.tmp_file.unlink()

    def test_carregar_vazio(self):
        self.assertEqual(sp.carregar(), {})

    def test_salvar_e_consultar(self):
        sp.salvar("10560396000185", "Auxiliar de Loja", 1518.00,
                  razao_social="MODELOFARMA LTDA", fonte="manual")
        entry = sp.consultar("10560396000185", "AUXILIAR DE LOJA")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["salario"], 1518.00)
        self.assertEqual(entry["fonte"], "manual")
        # Razão social fica na empresa, não na entry do cargo
        data = sp.carregar()
        self.assertEqual(
            data["10560396000185"]["_razao_social"],
            "MODELOFARMA LTDA",
        )

    def test_consultar_valor_atalho(self):
        sp.salvar("10560396000185", "estagio em supermercado", 600.0,
                  fonte="manual")
        valor = sp.consultar_valor("10560396000185", "ESTAGIO EM SUPERMERCADO")
        self.assertEqual(valor, 600.0)

    def test_cnpj_normalizado(self):
        # CNPJ com formatação deve achar o mesmo cadastrado só com dígitos
        sp.salvar("10560396000185", "auxiliar", 1500.0)
        self.assertIsNotNone(sp.consultar("10.560.396/0001-85", "AUXILIAR"))

    def test_cargo_normalizado(self):
        # Acentos e caixa não importam
        sp.salvar("10560396000185", "Auxiliar de Administração", 2000.0)
        self.assertIsNotNone(
            sp.consultar("10560396000185", "AUXILIAR DE ADMINISTRACAO")
        )
        self.assertIsNotNone(
            sp.consultar("10560396000185", "auxiliar de administração")
        )

    def test_cnpjs_diferentes_sao_independentes(self):
        sp.salvar("10560396000185", "auxiliar de loja", 1518.0)
        sp.salvar("04584726000331", "auxiliar de loja", 1700.0)
        a = sp.consultar_valor("10560396000185", "AUXILIAR DE LOJA")
        b = sp.consultar_valor("04584726000331", "AUXILIAR DE LOJA")
        self.assertEqual(a, 1518.0)
        self.assertEqual(b, 1700.0)

    def test_consulta_inexistente_retorna_none(self):
        sp.salvar("10560396000185", "auxiliar de loja", 1518.0)
        self.assertIsNone(sp.consultar("99999999999999", "AUXILIAR DE LOJA"))
        self.assertIsNone(sp.consultar("10560396000185", "OUTRO CARGO"))

    def test_update_mantem_criado_em_e_seta_ultima_atualizacao(self):
        sp.salvar("10560396000185", "auxiliar", 1500.0, fonte="auto")
        primeira = sp.consultar("10560396000185", "auxiliar")
        sp.salvar("10560396000185", "auxiliar", 1700.0, fonte="manual")
        segunda = sp.consultar("10560396000185", "auxiliar")

        self.assertEqual(segunda["criado_em"], primeira["criado_em"])
        self.assertEqual(segunda["salario"], 1700.0)
        self.assertEqual(segunda["fonte"], "manual")
        self.assertEqual(segunda["valor_anterior"], 1500.0)

    def test_valor_zero_ou_negativo_rejeitado(self):
        sp.salvar("10560396000185", "x", 0)
        sp.salvar("10560396000185", "y", -100)
        self.assertIsNone(sp.consultar("10560396000185", "x"))
        self.assertIsNone(sp.consultar("10560396000185", "y"))

    def test_cnpj_invalido_rejeitado(self):
        sp.salvar("123", "cargo", 1500.0)  # CNPJ curto
        sp.salvar("", "cargo", 1500.0)  # vazio
        self.assertEqual(sp.carregar(), {})

    def test_cargo_vazio_rejeitado(self):
        sp.salvar("10560396000185", "", 1500.0)
        self.assertEqual(sp.carregar(), {})


if __name__ == "__main__":
    unittest.main()
