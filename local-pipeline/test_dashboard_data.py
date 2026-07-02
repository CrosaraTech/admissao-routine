"""Testes do módulo dashboard_data (v2.15.0).

Cobre:
  - leitura crua da planilha XLSX
  - dedup por (msg_id, nome, cnpj)
  - categorização por procedência
  - regra "velha" (≥ 3 dias)
  - contadores agregados
  - agrupamento por dia (ordem decrescente)
  - cargo_ia lendo payload JSON
  - status do pipeline sem ndjson

Estratégia: cria arquivos temporários (planilha + payloads/) e aponta
PLANILHA_ADMISSOES / PAYLOADS_DIR / ADMISSAO_LOG pra esses paths via
patch.object.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

import dashboard_data as dd


HEADERS = ["Data/Hora", "Nome", "Empresa", "CNPJ", "Procedência", "msg_id"]


def _hoje() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _dias_atras(n: int) -> str:
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d %H:%M:%S")


def _criar_planilha(path: Path, linhas: list[list]) -> None:
    """Cria uma planilha XLSX com cabeçalho + linhas controladas."""
    wb = Workbook()
    ws = wb.active
    ws.append(HEADERS)
    for linha in linhas:
        ws.append(linha)
    wb.save(path)


class DashboardDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="admiter_test_"))
        self.planilha = self.tmp / "admissoes.xlsx"
        self.payloads = self.tmp / "payloads"
        self.payloads.mkdir()
        self.ndjson = self.tmp / "admissao_log.ndjson"

        # Patches que valem por todo o teste
        self._patches = [
            patch.object(dd, "PLANILHA_ADMISSOES", self.planilha),
            patch.object(dd, "PAYLOADS_DIR", self.payloads),
            patch.object(dd, "ADMISSAO_LOG", self.ndjson),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── Casos de leitura ─────────────────────────────────────────

    def test_planilha_vazia_retorna_lista_vazia(self):
        # nenhum arquivo no disco
        self.assertEqual(dd.listar_entidades(), [])
        # contadores também devem ficar zerados
        c = dd.resumo_contadores()
        self.assertEqual(c["processadas"], 0)
        self.assertEqual(c["total_pendentes"], 0)

    def test_dedup_por_entidade(self):
        """3 tentativas do mesmo (msg_id, nome, cnpj) devem virar 1 entidade
        com o ts do ÚLTIMO evento."""
        msg_id = "abc123def4567890"
        nome = "ELIANE TESTE"
        cnpj = "12345678000190"
        linhas = [
            [_dias_atras(5), nome, "EMPRESA X", cnpj, "Pendente cliente — faltam: cpf", msg_id],
            [_dias_atras(2), nome, "EMPRESA X", cnpj, "Pendente cliente — faltam: cpf, salario", msg_id],
            [_dias_atras(0), nome, "EMPRESA X", cnpj, "Cadastrado — candidato 999", msg_id],
        ]
        _criar_planilha(self.planilha, linhas)

        entidades = dd.listar_entidades()
        self.assertEqual(len(entidades), 1)
        e = entidades[0]
        self.assertEqual(e["nome"], nome)
        self.assertEqual(e["cnpj"], cnpj)
        # último evento venceu — categoria = processada
        self.assertEqual(e["categoria"], "processada")

    def test_categorizar_por_procedencia(self):
        """Cada string da coluna procedência deve mapear pra categoria correta."""
        # "Cadastrado..." → processada
        self.assertEqual(dd._categorizar("Cadastrado — candidato 999"), "processada")
        self.assertEqual(dd._categorizar("Dry-run — payload pronto"), "processada")
        # "Pendente cliente..." → pendente_cliente
        self.assertEqual(dd._categorizar("Pendente cliente — faltam: cpf"), "pendente_cliente")
        # "Pendente interno..." → pendente_interna
        self.assertEqual(dd._categorizar("Pendente interno — depto não casa"), "pendente_interna")
        # "Falha técnica" / "Falha tecnica" → falha_tecnica
        self.assertEqual(dd._categorizar("Falha técnica — HTTP 500"), "falha_tecnica")
        self.assertEqual(dd._categorizar("Falha tecnica — HTTP 422"), "falha_tecnica")
        # default → outro
        self.assertEqual(dd._categorizar(""), "outro")
        self.assertEqual(dd._categorizar("Outro"), "outro")

    def test_eh_velha_3_dias(self):
        """ts ≥ 3 dias atrás → velha; hoje → não velha."""
        agora = datetime.now()
        ts_4_dias = (agora - timedelta(days=4)).strftime("%Y-%m-%d %H:%M:%S")
        ts_hoje = agora.strftime("%Y-%m-%d %H:%M:%S")
        ts_2_dias = (agora - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")

        self.assertTrue(dd._eh_velha(ts_4_dias, agora=agora))
        self.assertFalse(dd._eh_velha(ts_hoje, agora=agora))
        self.assertFalse(dd._eh_velha(ts_2_dias, agora=agora))
        # ts vazio nunca é velha
        self.assertFalse(dd._eh_velha("", agora=agora))
        # ts inválido → não quebra, retorna False
        self.assertFalse(dd._eh_velha("não é data", agora=agora))

    def test_resumo_contadores_bate(self):
        """5 linhas: 3 cadastrados + 2 pendentes → contadores corretos."""
        linhas = [
            [_hoje(), "PESSOA A", "EMP", "111", "Cadastrado — candidato 1", "m1aaaaaaaaaaaaaa"],
            [_hoje(), "PESSOA B", "EMP", "111", "Cadastrado — candidato 2", "m2aaaaaaaaaaaaaa"],
            [_hoje(), "PESSOA C", "EMP", "111", "Cadastrado — candidato 3", "m3aaaaaaaaaaaaaa"],
            [_hoje(), "PESSOA D", "EMP", "111", "Pendente cliente — faltam: cpf", "m4aaaaaaaaaaaaaa"],
            [_hoje(), "PESSOA E", "EMP", "111", "Pendente interno — depto", "m5aaaaaaaaaaaaaa"],
        ]
        _criar_planilha(self.planilha, linhas)

        c = dd.resumo_contadores()
        self.assertEqual(c["processadas"], 3)
        self.assertEqual(c["pendentes_cliente"], 1)
        self.assertEqual(c["pendentes_internas"], 1)
        self.assertEqual(c["falhas_tecnicas"], 0)
        self.assertEqual(c["total_pendentes"], 2)

    def test_agrupar_por_dia_ordem_decrescente(self):
        """agrupar_por_dia deve retornar dias do MAIS RECENTE pro mais antigo."""
        ontem = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        anteontem = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        hoje = _hoje()
        linhas = [
            [ontem, "PESSOA ONTEM", "E", "1", "Cadastrado — c1", "mid1aaaaaaaaaaaa"],
            [hoje, "PESSOA HOJE", "E", "1", "Cadastrado — c2", "mid2aaaaaaaaaaaa"],
            [anteontem, "PESSOA ANTEONTEM", "E", "1", "Cadastrado — c3", "mid3aaaaaaaaaaaa"],
        ]
        _criar_planilha(self.planilha, linhas)

        ents = dd.listar_entidades()
        por_dia = dd.agrupar_por_dia(ents)
        chaves = list(por_dia.keys())
        # ordem decrescente: hoje > ontem > anteontem
        self.assertEqual(len(chaves), 3)
        self.assertGreater(chaves[0], chaves[1])
        self.assertGreater(chaves[1], chaves[2])

    def test_cargo_ia_de_le_payload(self):
        """cargo_ia_de deve ler o JSON do payload e devolver o cargo."""
        msg_id = "feedfacecafebabe1234567890"
        # nome no glob é "*_<msg_id[:16]>*.json"
        arq = self.payloads / f"2026-06-13T10-00-00_{msg_id[:16]}_ELIANE.json"
        doc = {
            "payload": {
                "data": {
                    "attributes": {"nomecargo": "OPERADOR DE CAIXA"}
                }
            },
            "resolucao": {}
        }
        arq.write_text(json.dumps(doc), encoding="utf-8")

        cargo = dd.cargo_ia_de(msg_id)
        self.assertEqual(cargo, "OPERADOR DE CAIXA")

        # sem msg_id → string vazia
        self.assertEqual(dd.cargo_ia_de(""), "")
        # msg_id que não tem payload → string vazia
        self.assertEqual(dd.cargo_ia_de("0000000000000000xxxxxxxxxxxxxxxx"), "")

    def test_status_pipeline_sem_ndjson(self):
        """Sem o admissao_log.ndjson, status retorna defaults + contadores zerados."""
        s = dd.status_pipeline()
        self.assertEqual(s["ultima_passada"], "")
        self.assertEqual(s["processadas"], 0)
        self.assertEqual(s["total_pendentes"], 0)
        self.assertEqual(s["falhas_tecnicas"], 0)


if __name__ == "__main__":
    unittest.main()
