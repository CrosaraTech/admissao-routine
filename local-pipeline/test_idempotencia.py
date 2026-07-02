"""Testes do módulo idempotencia (v2.14.x) — proteção contra POST duplicado.

Cobre os 3 mecanismos centrais:
  1. backfill_de_payloads — popula o registro a partir de payloads/*.json
  2. consultar_duplicata — mesma_empresa vs outra empresa
  3. fingerprint de reprocesso — só reprocessar se mudou alguma tabela
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import idempotencia as ide


class TestIdempotencia(unittest.TestCase):
    def setUp(self):
        # Isola estado em disco (registro + fingerprint + payloads/)
        self.tmpdir = Path(tempfile.mkdtemp(prefix="idem_test_"))
        self._patchers = [
            mock.patch.object(ide, "REGISTRO_FILE", self.tmpdir / "candidatos_postados.json"),
            mock.patch.object(ide, "FP_FILE", self.tmpdir / "reprocesso_fp.json"),
            mock.patch.object(ide, "PAYLOADS_DIR", self.tmpdir / "payloads"),
            mock.patch.object(ide, "_DIR", self.tmpdir),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        import shutil
        try:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        except OSError:
            pass

    # ── backfill ────────────────────────────────────────────────

    def test_backfill_vazio_quando_sem_payloads(self):
        # PAYLOADS_DIR não existe → 0 entries
        registro: dict = {}
        n = ide.backfill_de_payloads(registro)
        self.assertEqual(n, 0)
        self.assertEqual(registro, {})

    def test_backfill_de_3_payloads_com_sucesso(self):
        payloads_dir = self.tmpdir / "payloads"
        payloads_dir.mkdir()
        for i, (cpf, cid, cnpj) in enumerate([
            ("12345678901", "111", "10560396000185"),
            ("98765432100", "222", "10560396000185"),
            ("11122233344", "333", "04584726000331"),
        ]):
            (payloads_dir / f"{i}.json").write_text(json.dumps({
                "msg_id": f"msg{i}",
                "timestamp": f"2026-06-1{i}T10:00:00",
                "payload": {"data": {"attributes": {"cpf": cpf, "nome": f"FUL #{i}"}}},
                "resolucao": {"cnpj_empresa": cnpj},
                "resultado": {"status": "sucesso", "candidato_id": cid},
            }), encoding="utf-8")
        registro: dict = {}
        n = ide.backfill_de_payloads(registro)
        self.assertEqual(n, 3)
        # Cada chave é cpf|cnpj
        self.assertEqual(len(registro), 3)
        self.assertEqual(registro["12345678901|10560396000185"][0]["candidato_id"], "111")

    def test_backfill_pula_payloads_falhos(self):
        payloads_dir = self.tmpdir / "payloads"
        payloads_dir.mkdir()
        (payloads_dir / "ok.json").write_text(json.dumps({
            "payload": {"data": {"attributes": {"cpf": "12345678901"}}},
            "resolucao": {"cnpj_empresa": "10560396000185"},
            "resultado": {"status": "sucesso", "candidato_id": "111"},
        }), encoding="utf-8")
        (payloads_dir / "fail.json").write_text(json.dumps({
            "payload": {"data": {"attributes": {"cpf": "98765432100"}}},
            "resolucao": {"cnpj_empresa": "10560396000185"},
            "resultado": {"status": "falha_post", "candidato_id": None, "erro": "HTTP 422"},
        }), encoding="utf-8")
        registro: dict = {}
        n = ide.backfill_de_payloads(registro)
        self.assertEqual(n, 1)
        self.assertIn("12345678901|10560396000185", registro)
        self.assertNotIn("98765432100|10560396000185", registro)

    # ── consultar_duplicata ─────────────────────────────────────

    def test_cpf_nunca_postado_retorna_vazio(self):
        self.assertEqual(ide.consultar_duplicata("12345678901", "10560396000185"), [])

    def test_duplicata_mesma_empresa(self):
        ide.registrar_post("12345678901", "10560396000185", "111", nome="X")
        hits = ide.consultar_duplicata("12345678901", "10560396000185")
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0]["mesma_empresa"])
        self.assertEqual(hits[0]["candidato_id"], "111")

    def test_duplicata_outra_empresa_marca_mesma_empresa_false(self):
        # Caso JENIFFY: CNPJ digitado errado entre tentativas
        ide.registrar_post("12345678901", "10560396000185", "111", nome="X")
        hits = ide.consultar_duplicata("12345678901", "10560396000186")  # CNPJ típo
        self.assertEqual(len(hits), 1)
        self.assertFalse(hits[0]["mesma_empresa"])
        self.assertEqual(hits[0]["candidato_id"], "111")

    def test_cpf_normalizado_aceita_formatacao(self):
        ide.registrar_post("123.456.789-01", "10560396000185", "111")
        hits = ide.consultar_duplicata("12345678901", "10.560.396/0001-85")
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0]["mesma_empresa"])

    def test_registrar_post_idempotente_mesmo_candidato(self):
        ide.registrar_post("12345678901", "10560396000185", "111", nome="X")
        ide.registrar_post("12345678901", "10560396000185", "111", nome="X")
        hits = ide.consultar_duplicata("12345678901", "10560396000185")
        # Não duplica entrada do MESMO candidato_id
        self.assertEqual(len(hits), 1)

    # ── fingerprint ─────────────────────────────────────────────

    def test_fingerprint_estavel_quando_tabelas_nao_mudam(self):
        # Cria uma tabela de teste
        (self.tmpdir / "config.json").write_text("{}", encoding="utf-8")
        fp1 = ide.fingerprint_tabelas()
        fp2 = ide.fingerprint_tabelas()
        self.assertEqual(fp1, fp2)

    def test_fingerprint_muda_quando_tabela_muda(self):
        cfg = self.tmpdir / "config.json"
        cfg.write_text('{"v": 1}', encoding="utf-8")
        fp1 = ide.fingerprint_tabelas()
        # Sleep mínimo pra garantir mtime_ns diferente em FSes de baixa resolução
        import time
        time.sleep(0.05)
        cfg.write_text('{"v": 2}', encoding="utf-8")
        fp2 = ide.fingerprint_tabelas()
        self.assertNotEqual(fp1, fp2)

    def test_aviso_reprocesso_sem_fp_salvo_retorna_none(self):
        self.assertIsNone(ide.aviso_reprocesso("msg-novo"))

    def test_aviso_reprocesso_quando_nada_mudou_retorna_texto(self):
        ide.salvar_fingerprint_reprocesso("msg123")
        aviso = ide.aviso_reprocesso("msg123")
        self.assertIsNotNone(aviso)
        self.assertIn("Nada mudou", aviso)

    def test_aviso_reprocesso_some_se_tabela_mudou(self):
        ide.salvar_fingerprint_reprocesso("msg123")
        # Cria/modifica uma tabela monitorada pra forçar mudança de fp
        (self.tmpdir / "funcao_aliases.json").write_text('{"novo": "alias"}', encoding="utf-8")
        import time
        time.sleep(0.05)
        self.assertIsNone(ide.aviso_reprocesso("msg123"))


if __name__ == "__main__":
    unittest.main()
