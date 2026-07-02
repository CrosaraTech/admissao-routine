"""Teste de integração: wrapper único de POST + idempotência.

Cenário real (cobra v2.14.1): reprocessar email multi-pessoa com 2 já
cadastrados (JENIFFY/EDIMAURA no caso 12/06) NÃO deve gerar POST novo.

Antes da v2.14.1, o orquestrador chamava api.post_candidato direto e
duplicava. Agora todo POST passa por post_admissao.postar_candidato_registrado
que consulta idempotência ANTES.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import idempotencia as ide
from post_admissao import postar_candidato_registrado


class FakeAPI:
    """Stand-in pra EContadorAPI — conta quantos POSTs foram feitos."""
    def __init__(self, sucesso_para_cpfs: set[str] | None = None):
        # cpfs que devem retornar sucesso no POST (todos por default)
        self.sucesso_para_cpfs = sucesso_para_cpfs
        self.posts_feitos: list[dict] = []
        self.proximo_id = 9000

    def post_candidato(self, payload: dict):
        cpf = str((payload.get("data") or {}).get("attributes", {}).get("cpf", ""))
        self.posts_feitos.append(payload)
        if self.sucesso_para_cpfs is not None and cpf not in self.sucesso_para_cpfs:
            return False, "HTTP 422", "{}"
        self.proximo_id += 1
        return True, str(self.proximo_id), ""


class FakeGmail:
    def __init__(self):
        self.labels_aplicados: list[tuple[str, str]] = []
        self.labels_removidos: list[tuple[str, str]] = []
    def aplicar_label(self, msg_id, label):
        self.labels_aplicados.append((msg_id, label))
    def remover_label(self, msg_id, label):
        self.labels_removidos.append((msg_id, label))


class TestReprocessoMultiPessoa(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="post_int_"))
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
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _payload_de(self, cpf: str, nome: str) -> dict:
        return {
            "data": {
                "type": "candidatos",
                "attributes": {
                    "nome": nome, "cpf": int(cpf), "admissao": "2026-06-15",
                    "salario": 1500.0,
                },
                "relationships": {
                    "empresa": {"data": {"type": "empresas", "id": "89"}},
                },
            }
        }

    def test_reprocesso_3_pessoas_2_ja_cadastradas_so_posta_1(self):
        """Email tinha 3 pessoas; reprocessamento depois que 2 já passaram.

        Sem idempotência: 3 POSTs (2 viram duplicata). Com idempotência: 1 POST.
        """
        cnpj = "10560396000185"
        api = FakeAPI()

        # Pré-condição: JENIFFY e EDIMAURA já foram cadastradas em passada anterior
        ide.registrar_post("11111111111", cnpj, "5001", nome="JENIFFY", origem="orquestrador")
        ide.registrar_post("22222222222", cnpj, "5002", nome="EDIMAURA", origem="orquestrador")
        # Sanity: registro tem 2 entradas
        self.assertEqual(
            len(ide.consultar_duplicata("11111111111", cnpj)), 1
        )

        pessoas = [
            ("11111111111", "JENIFFY"),
            ("22222222222", "EDIMAURA"),
            ("33333333333", "NOVA_PESSOA"),  # essa SIM deve postar
        ]

        resultados = []
        for cpf, nome in pessoas:
            res = postar_candidato_registrado(
                api, self._payload_de(cpf, nome),
                cpf=cpf, cnpj=cnpj, nome=nome,
                origem="orquestrador", msg_id="thread123",
            )
            resultados.append(res)

        # 2 PULOU + 1 POST real
        self.assertEqual(sum(1 for r in resultados if r.pulou), 2)
        self.assertEqual(sum(1 for r in resultados if r.ok and not r.pulou), 1)
        # FakeAPI só recebeu 1 POST (NOVA_PESSOA)
        self.assertEqual(len(api.posts_feitos), 1)
        # As 2 puladas devolveram o candidato_id antigo
        self.assertEqual(resultados[0].candidato_id, "5001")
        self.assertEqual(resultados[1].candidato_id, "5002")
        # A nova ganhou id novo do FakeAPI
        self.assertNotIn(resultados[2].candidato_id, ("5001", "5002"))

    def test_permitir_duplicata_forca_post(self):
        """UI 'POSTar mesmo assim' precisa funcionar — passa permitir_duplicata=True."""
        cnpj = "10560396000185"
        ide.registrar_post("11111111111", cnpj, "5001", nome="X", origem="orquestrador")
        api = FakeAPI()

        res = postar_candidato_registrado(
            api, self._payload_de("11111111111", "X"),
            cpf="11111111111", cnpj=cnpj, nome="X",
            origem="ui_envio_forcado", msg_id="mid",
            permitir_duplicata=True,
        )
        # POST foi feito (não pulou)
        self.assertFalse(res.pulou)
        self.assertTrue(res.ok)
        self.assertEqual(len(api.posts_feitos), 1)
        # E o registro ganhou nova entrada (id diferente)
        hits = ide.consultar_duplicata("11111111111", cnpj)
        ids = sorted(h["candidato_id"] for h in hits)
        self.assertEqual(len(ids), 2)
        self.assertIn("5001", ids)

    def test_outra_empresa_avisa_mas_posta(self):
        """CPF em outra empresa (típo de CNPJ entre tentativas) loga warning
        mas POSTa. UI pode passar permitir_duplicata=True após confirmação."""
        ide.registrar_post("11111111111", "10560396000185", "5001", nome="X")
        api = FakeAPI()

        res = postar_candidato_registrado(
            api, self._payload_de("11111111111", "X"),
            cpf="11111111111", cnpj="10560396000186",  # CNPJ diferente
            nome="X", origem="orquestrador", msg_id="mid",
            permitir_duplicata=True,
        )
        self.assertTrue(res.ok)
        self.assertFalse(res.pulou)
        self.assertEqual(len(api.posts_feitos), 1)
        self.assertEqual(len(res.hits_anteriores), 1)  # tem hit pra mostrar
        self.assertFalse(res.hits_anteriores[0]["mesma_empresa"])

    def test_falha_http_422_marcada_corretamente(self):
        api = FakeAPI(sucesso_para_cpfs=set())  # tudo falha
        res = postar_candidato_registrado(
            api, self._payload_de("11111111111", "X"),
            cpf="11111111111", cnpj="10560396000185", nome="X",
            origem="orquestrador", msg_id="mid",
        )
        self.assertFalse(res.ok)
        self.assertEqual(res.status_http, 422)
        self.assertEqual(res.erro_ref, "HTTP 422")

    def test_pulado_aplica_label_processado_se_gmail_passado(self):
        """Mesmo PULANDO, a thread precisa ser fechada (label processado).
        Senão polling reprocessa e Claude é chamado de novo à toa.
        """
        cnpj = "10560396000185"
        ide.registrar_post("11111111111", cnpj, "5001", nome="X")
        api = FakeAPI()
        gmail = FakeGmail()
        res = postar_candidato_registrado(
            api, self._payload_de("11111111111", "X"),
            cpf="11111111111", cnpj=cnpj, nome="X",
            origem="orquestrador", msg_id="mid_x",
            gmail=gmail, label_processado="ADMISSÃO/processado",
            label_pendente_remover=["mid_x"],
        )
        self.assertTrue(res.pulou)
        # Label processado aplicada na msg
        self.assertIn(("mid_x", "ADMISSÃO/processado"), gmail.labels_aplicados)


if __name__ == "__main__":
    unittest.main()
