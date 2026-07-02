"""Testes do resolver_departamento — REGRA 0 (v2.14.0) + 1/2/3 (regressão).

Bug real 12/06: 6 das 11 pendências eram empresas SEM departamentos no eContador
e o resolver matava no caminho `if not deptos_api: return None, ...`. REGRA 0
permite POSTar sem departamento atrás de flag.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from departamento import (
    SEM_DEPARTAMENTO,
    resolver_departamento,
)


class TestRegra0SemDepartamento(unittest.TestCase):
    """Empresa com 0 deptos no eContador (REGRA 0 — v2.14.0)."""

    def test_sem_deptos_e_flag_off_vira_pendencia(self):
        depto_id, msg = resolver_departamento(
            empresa_id="89", cnpj_empresa="10560396000185",
            razao_social="MODELOFARMA LTDA",
            deptos_api=[],  # ZERO deptos
            departamento_sugerido=None,
            departamentos_json_paths=[],
            permitir_sem_departamento=False,  # FLAG OFF
        )
        self.assertIsNone(depto_id)
        self.assertIn("sem departamentos cadastrados", msg.lower())

    def test_sem_deptos_e_flag_on_devolve_sentinela(self):
        depto_id, msg = resolver_departamento(
            empresa_id="89", cnpj_empresa="10560396000185",
            razao_social="MODELOFARMA LTDA",
            deptos_api=[],
            departamento_sugerido=None,
            departamentos_json_paths=[],
            permitir_sem_departamento=True,  # FLAG ON
        )
        self.assertEqual(depto_id, SEM_DEPARTAMENTO)
        self.assertTrue(msg.startswith("ok"))


class TestRegra1UmDeptoSo(unittest.TestCase):
    def test_um_depto_usa_ele(self):
        depto_id, msg = resolver_departamento(
            empresa_id="89", cnpj_empresa="10560396000185",
            razao_social="X", deptos_api=[{"id": "245", "nome": "GERAL"}],
            departamento_sugerido=None, departamentos_json_paths=[],
        )
        self.assertEqual(depto_id, "245")
        self.assertEqual(msg, "ok")


class TestRegra2GeralMaisNome(unittest.TestCase):
    def test_dois_deptos_geral_mais_empresa_usa_o_nao_geral(self):
        depto_id, msg = resolver_departamento(
            empresa_id="89", cnpj_empresa="10560396000185",
            razao_social="MODELOFARMA",
            deptos_api=[
                {"id": "245", "nome": "GERAL"},
                {"id": "246", "nome": "MODELOFARMA"},
            ],
            departamento_sugerido=None, departamentos_json_paths=[],
        )
        self.assertEqual(depto_id, "246")
        self.assertEqual(msg, "ok")

    def test_dois_deptos_sem_geral_pega_mais_proximo_da_razao(self):
        depto_id, _ = resolver_departamento(
            empresa_id="89", cnpj_empresa="10560396000185",
            razao_social="ALPHA SOLUTIONS",
            deptos_api=[
                {"id": "100", "nome": "BETA UNIT"},
                {"id": "101", "nome": "ALPHA UNIT"},
            ],
            departamento_sugerido=None, departamentos_json_paths=[],
        )
        # ALPHA UNIT bate mais com ALPHA SOLUTIONS que BETA UNIT
        self.assertEqual(depto_id, "101")


class TestRegra3MultiploEspecial(unittest.TestCase):
    """CNPJ na lista CNPJS_ESPECIAIS deve usar departamentos.json."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="depto_test_"))
        self.dep_json = self.tmpdir / "departamentos.json"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_cnpj_especial_resolve_via_variantes(self):
        # 08867336000168 = SOL NASCENTE — já está em CNPJS_ESPECIAIS
        self.dep_json.write_text(json.dumps({
            "empresas": {
                "08867336000168": {
                    "razao_social": "SOL NASCENTE",
                    "departamentos": [
                        {"id": "500", "nome_variantes": ["LOGÍSTICA", "TRANSPORTE"]},
                        {"id": "501", "nome_variantes": ["ADMINISTRATIVO"]},
                    ],
                }
            }
        }), encoding="utf-8")
        depto_id, msg = resolver_departamento(
            empresa_id="89", cnpj_empresa="08867336000168",
            razao_social="SOL NASCENTE",
            deptos_api=[{"id": "500", "nome": "LOGISTICA"},
                        {"id": "501", "nome": "ADM"}],
            departamento_sugerido="LOGÍSTICA",
            departamentos_json_paths=[self.dep_json],
        )
        self.assertEqual(depto_id, "500")
        self.assertEqual(msg, "ok")

    def test_cnpj_especial_sem_sugestao_usa_default_id(self):
        self.dep_json.write_text(json.dumps({
            "empresas": {
                "08867336000168": {
                    "razao_social": "SOL NASCENTE",
                    "departamento_default_id": "500",
                    "departamentos": [
                        {"id": "500", "nome_variantes": ["LOGÍSTICA"]},
                    ],
                }
            }
        }), encoding="utf-8")
        depto_id, _ = resolver_departamento(
            empresa_id="89", cnpj_empresa="08867336000168",
            razao_social="SOL NASCENTE",
            deptos_api=[{"id": "500", "nome": "LOG"}],
            departamento_sugerido=None,
            departamentos_json_paths=[self.dep_json],
        )
        self.assertEqual(depto_id, "500")


if __name__ == "__main__":
    unittest.main()
