"""Testes pro módulo perfis_remetente (v2.16.0).

Cobertura mínima: 15 testes — consolidação, normalização, agregação,
persistência de observações, resumo pra prompt e aplicação de defaults.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import perfis_remetente as pr


def _payload(remetente, cnpj, cargo, salario, ok=True, attrs_extra=None,
             ts="2026-06-15T10:00:00", msg_id="abc", funcao_id="100",
             razao_social="EMPRESA X"):
    """Helper que monta um payload JSON simulado no formato salvo em payloads/."""
    attrs = {"nome": "X", "cpf": 12345678901, "nomecargo": cargo}
    if salario:
        attrs["salario"] = salario
    if attrs_extra:
        attrs.update(attrs_extra)
    return {
        "timestamp": ts,
        "msg_id": msg_id,
        "remetente": remetente,
        "payload": {"data": {"attributes": attrs}},
        "resolucao": {
            "cnpj_empresa": cnpj,
            "razao_social": razao_social,
            "cargo_extraido": cargo,
            "funcao_id": funcao_id,
        },
        "resultado": {
            "status": "sucesso" if ok else "pendente_claude",
            "candidato_id": "9999" if ok else None,
        },
    }


class PerfisTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="perfis_test_"))
        self.payloads_dir = self.tmp / "payloads"
        self.payloads_dir.mkdir()
        self.perfis_file = self.tmp / "perfis_remetente.json"
        self._patchers = [
            patch.object(pr, "PERFIS_FILE", self.perfis_file),
            patch.object(pr, "PAYLOADS_DIR", self.payloads_dir),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _add_payload(self, idx, **kwargs):
        path = self.payloads_dir / f"{idx:03d}.json"
        path.write_text(json.dumps(_payload(**kwargs)), encoding="utf-8")
        return path


class TestCarregarConsolidarBasico(PerfisTestBase):
    def test_carregar_vazio(self):
        """Sem arquivo, carregar retorna {}."""
        self.assertFalse(self.perfis_file.exists())
        self.assertEqual(pr.carregar(), {})

    def test_consolidar_sem_payloads(self):
        """Pasta de payloads vazia retorna {}."""
        result = pr.consolidar_todos()
        self.assertEqual(result, {})

    def test_consolidar_um_remetente_simples(self):
        """1 payload → 1 perfil com stats corretas."""
        self._add_payload(1, remetente="rh@x.com", cnpj="12345678000190",
                          cargo="OPERADOR", salario=1500)
        result = pr.consolidar_todos()
        self.assertEqual(len(result), 1)
        self.assertIn("rh@x.com", result)
        perf = result["rh@x.com"]
        stats = perf["estatisticas"]
        self.assertEqual(stats["n_total"], 1)
        self.assertEqual(stats["n_processadas"], 1)
        self.assertEqual(stats["n_pendencias"], 0)
        self.assertEqual(len(perf["cnpjs"]), 1)
        self.assertEqual(perf["cnpjs"][0]["cnpj"], "12345678000190")

    def test_consolidar_agrupa_por_remetente(self):
        """3 payloads de 2 remetentes diferentes → 2 perfis."""
        self._add_payload(1, remetente="rh@a.com", cnpj="11111111000111",
                          cargo="OP A", salario=1500)
        self._add_payload(2, remetente="rh@a.com", cnpj="11111111000111",
                          cargo="OP A", salario=1600)
        self._add_payload(3, remetente="rh@b.com", cnpj="22222222000122",
                          cargo="OP B", salario=2000)
        result = pr.consolidar_todos()
        self.assertEqual(len(result), 2)
        self.assertEqual(result["rh@a.com"]["estatisticas"]["n_total"], 2)
        self.assertEqual(result["rh@b.com"]["estatisticas"]["n_total"], 1)


class TestNormalizacao(PerfisTestBase):
    def test_normaliza_email_com_nome_no_formato_From(self):
        """'Robert <rh@x.com>' vira 'rh@x.com'."""
        self._add_payload(1, remetente="Robert <rh@x.com>",
                          cnpj="12345678000190", cargo="OP", salario=1500)
        result = pr.consolidar_todos()
        self.assertIn("rh@x.com", result)
        self.assertNotIn("Robert <rh@x.com>", result)


class TestAgregacao(PerfisTestBase):
    def test_cnpjs_multiplos_do_mesmo_remetente(self):
        """2 CNPJs do mesmo email aparecem na lista de cnpjs."""
        self._add_payload(1, remetente="rh@grupo.com", cnpj="11111111000111",
                          cargo="OP", salario=1500, razao_social="FILIAL A")
        self._add_payload(2, remetente="rh@grupo.com", cnpj="22222222000122",
                          cargo="OP", salario=1600, razao_social="FILIAL B")
        self._add_payload(3, remetente="rh@grupo.com", cnpj="22222222000122",
                          cargo="OP", salario=1700, razao_social="FILIAL B")
        result = pr.consolidar_todos()
        perf = result["rh@grupo.com"]
        cnpjs = perf["cnpjs"]
        self.assertEqual(len(cnpjs), 2)
        cnpjs_set = {c["cnpj"] for c in cnpjs}
        self.assertEqual(cnpjs_set, {"11111111000111", "22222222000122"})
        # O mais frequente fica primeiro (most_common)
        self.assertEqual(cnpjs[0]["cnpj"], "22222222000122")
        self.assertEqual(cnpjs[0]["n_admissoes"], 2)

    def test_cargos_frequentes_com_salario_medio(self):
        """Vários payloads do mesmo cargo → salario_padrao = média."""
        self._add_payload(1, remetente="rh@x.com", cnpj="11111111000111",
                          cargo="OPERADOR DE CAIXA", salario=1500)
        self._add_payload(2, remetente="rh@x.com", cnpj="11111111000111",
                          cargo="OPERADOR DE CAIXA", salario=1600)
        self._add_payload(3, remetente="rh@x.com", cnpj="11111111000111",
                          cargo="OPERADOR DE CAIXA", salario=1700)
        result = pr.consolidar_todos()
        cargos = result["rh@x.com"]["cargos_frequentes"]
        self.assertIn("OPERADOR DE CAIXA", cargos)
        rec = cargos["OPERADOR DE CAIXA"]
        self.assertEqual(rec["n_vezes"], 3)
        self.assertEqual(rec["salario_padrao"], 1600.0)


class TestOmissoesHabituais(PerfisTestBase):
    def test_omissoes_habituais_detecta_quando_todos_omitem(self):
        """5 payloads sem 'salario' → 'salario' em omissoes_habituais."""
        for i in range(5):
            self._add_payload(
                i, remetente="rh@x.com", cnpj="11111111000111",
                cargo="OP", salario=None,
                ts=f"2026-06-{10 + i:02d}T10:00:00",
            )
        result = pr.consolidar_todos()
        omissoes = result["rh@x.com"]["padroes_aprendidos"]["omissoes_habituais"]
        self.assertIn("salario", omissoes)

    def test_omissoes_habituais_ignora_quando_um_tem(self):
        """4 sem salário + 1 com salário → 'salario' NÃO está em omissoes."""
        for i in range(4):
            self._add_payload(
                i, remetente="rh@x.com", cnpj="11111111000111",
                cargo="OP", salario=None,
                ts=f"2026-06-{10 + i:02d}T10:00:00",
            )
        # Mais recente tem salário
        self._add_payload(99, remetente="rh@x.com", cnpj="11111111000111",
                          cargo="OP", salario=1500,
                          ts="2026-06-20T10:00:00")
        result = pr.consolidar_todos()
        omissoes = result["rh@x.com"]["padroes_aprendidos"]["omissoes_habituais"]
        self.assertNotIn("salario", omissoes)


class TestObservacoesPersistencia(PerfisTestBase):
    def test_atualizar_observacoes_persiste(self):
        """Atualiza, recarrega, observação está lá."""
        self._add_payload(1, remetente="rh@x.com", cnpj="11111111000111",
                          cargo="OP", salario=1500)
        pr.consolidar_todos()
        ok = pr.atualizar_observacoes("rh@x.com",
                                      "Robert é responsivo",
                                      nome_apresentacao="Drogaria X")
        self.assertTrue(ok)
        # Recarrega do arquivo
        perfis = pr.carregar()
        self.assertEqual(perfis["rh@x.com"]["observacoes_operador"],
                         "Robert é responsivo")
        self.assertEqual(perfis["rh@x.com"]["nome_apresentacao"],
                         "Drogaria X")

    def test_atualizar_observacoes_preserva_no_recalcular(self):
        """recalcular_todos não apaga observações."""
        self._add_payload(1, remetente="rh@x.com", cnpj="11111111000111",
                          cargo="OP", salario=1500)
        pr.consolidar_todos()
        pr.atualizar_observacoes("rh@x.com", "Anotação importante",
                                 nome_apresentacao="Empresa Cool")
        # Recalcula
        result = pr.consolidar_todos()
        self.assertEqual(result["rh@x.com"]["observacoes_operador"],
                         "Anotação importante")
        self.assertEqual(result["rh@x.com"]["nome_apresentacao"],
                         "Empresa Cool")


class TestResumoPraPrompt(PerfisTestBase):
    def test_resumo_pra_prompt_vazio_pra_remetente_novo(self):
        """Remetente sem perfil retorna ""."""
        # Sem nenhum payload — perfil não existe
        resumo = pr.resumo_pra_prompt("desconhecido@x.com")
        self.assertEqual(resumo, "")

    def test_resumo_pra_prompt_contem_cnpj_e_cargo(self):
        """Com perfil, retorno tem CNPJ (formatado v2.16.4) e cargo."""
        self._add_payload(1, remetente="rh@x.com", cnpj="12345678000190",
                          cargo="OPERADOR DE CAIXA", salario=1500,
                          razao_social="DROGARIA TESTE")
        pr.consolidar_todos()
        resumo = pr.resumo_pra_prompt("rh@x.com")
        # CNPJ formatado: 12.345.678/0001-90 (v2.16.4)
        self.assertIn("12.345.678/0001-90", resumo)
        self.assertIn("OPERADOR DE CAIXA", resumo)
        # E o cabeçalho passou a dizer "Contratantes" (não mais "CNPJs")
        self.assertIn("Contratantes", resumo)


class TestAplicarDefaults(PerfisTestBase):
    def test_aplicar_defaults_preenche_salario_quando_omissao(self):
        """Payload sem salário + perfil tem omissão E salário cadastrado → preenche."""
        # 5 payloads do mesmo cargo SEM salário → vira omissão habitual,
        # mas precisamos do salário cadastrado pra cargo. Truque:
        # uns têm salário (pra alimentar cargos_frequentes), mas no aggregate
        # da janela todos sem (impossível). Vamos por outro caminho:
        # ter o cargo cadastrado COM salário em payloads antigos, e a janela
        # recente ser sem.
        # Janela = últimas 5. Vou criar 5 recentes sem salário + 1 antigo com.
        # Mas detectar_omissoes_habituais olha o campo "salario" em attrs.
        # Estratégia: criar 1 evento antigo com salário (alimenta cargo)
        # e 5 recentes sem (alimenta omissão).
        self._add_payload(0, remetente="rh@x.com", cnpj="11111111000111",
                          cargo="OPERADOR", salario=1600,
                          ts="2026-01-01T10:00:00")
        for i in range(5):
            self._add_payload(
                i + 1, remetente="rh@x.com", cnpj="11111111000111",
                cargo="OPERADOR", salario=None,
                ts=f"2026-06-{10 + i:02d}T10:00:00",
            )
        pr.consolidar_todos()
        # Confirma estado do perfil
        perfis = pr.carregar()
        perf = perfis["rh@x.com"]
        self.assertIn("salario", perf["padroes_aprendidos"]["omissoes_habituais"])
        self.assertEqual(perf["cargos_frequentes"]["OPERADOR"]["salario_padrao"],
                         1600.0)

        # Payload novo sem salário
        payload = {
            "data": {
                "attributes": {
                    "nome": "TESTE",
                    "nomecargo": "OPERADOR",
                },
            },
        }
        preenchidos = pr.aplicar_defaults_do_perfil(payload, "rh@x.com")
        self.assertEqual(payload["data"]["attributes"]["salario"], 1600.0)
        self.assertTrue(any("salario" in s for s in preenchidos))

    def test_aplicar_defaults_NAO_sobrescreve_existente(self):
        """Se payload já tem salário, NÃO substitui."""
        self._add_payload(0, remetente="rh@x.com", cnpj="11111111000111",
                          cargo="OPERADOR", salario=1600,
                          ts="2026-01-01T10:00:00")
        for i in range(5):
            self._add_payload(
                i + 1, remetente="rh@x.com", cnpj="11111111000111",
                cargo="OPERADOR", salario=None,
                ts=f"2026-06-{10 + i:02d}T10:00:00",
            )
        pr.consolidar_todos()

        payload = {
            "data": {
                "attributes": {
                    "nome": "TESTE",
                    "nomecargo": "OPERADOR",
                    "salario": 2500,  # já tem!
                },
            },
        }
        preenchidos = pr.aplicar_defaults_do_perfil(payload, "rh@x.com")
        # Salário original preservado
        self.assertEqual(payload["data"]["attributes"]["salario"], 2500)
        # Nada preenchido
        self.assertEqual(preenchidos, [])


if __name__ == "__main__":
    unittest.main()
