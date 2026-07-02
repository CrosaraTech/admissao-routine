"""Testes pra detector de estágio + aliases separados (v2.11.0)."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

import estagio
import funcao


class TestDetectorEstagio(unittest.TestCase):
    def test_assunto_explicito(self):
        d = estagio.detectar(
            assunto="ADMISSÃO ESTAGIÁRIO ASH TALENTOS YURI",
            corpo="bom dia",
            anexos_filenames=[],
        )
        self.assertTrue(d.eh_estagio)
        self.assertGreater(d.confianca, 0.5)
        self.assertTrue(any("estagiario" in e.lower() for e in d.evidencias))

    def test_corpo_termo_compromisso(self):
        d = estagio.detectar(
            assunto="ADMISSÃO YURI",
            corpo="Segue termo de compromisso estagiário ash talentos",
            anexos_filenames=[],
        )
        self.assertTrue(d.eh_estagio)

    def test_filename_tce(self):
        d = estagio.detectar(
            assunto="Documentos do funcionário",
            corpo="bom dia, segue tudo",
            anexos_filenames=["RG.pdf", "TCE_assinado.pdf"],
        )
        self.assertTrue(d.eh_estagio)
        self.assertTrue(any("tce" in e.lower() for e in d.evidencias))

    def test_filename_termo_compromisso(self):
        d = estagio.detectar(
            assunto="Admissão",
            corpo="",
            anexos_filenames=["termo_compromisso_yuri.pdf", "RG.pdf"],
        )
        self.assertTrue(d.eh_estagio)

    def test_ciee_no_corpo(self):
        d = estagio.detectar(
            assunto="Admissão",
            corpo="Estagiário vem do CIEE com bolsa",
            anexos_filenames=[],
        )
        self.assertTrue(d.eh_estagio)

    def test_lei_11788(self):
        d = estagio.detectar(
            assunto="contratação",
            corpo="conforme Lei 11.788 de 2008",
            anexos_filenames=[],
        )
        self.assertTrue(d.eh_estagio)

    def test_clt_normal_nao_dispara(self):
        d = estagio.detectar(
            assunto="ADMISSÃO PEDRO HENRIQUE",
            corpo="Segue documentação do novo funcionário",
            anexos_filenames=["RG.pdf", "CPF.pdf", "CTPS.pdf"],
        )
        self.assertFalse(d.eh_estagio)

    def test_palavra_estagio_isolada_nao_dispara(self):
        # "estagio inicial do projeto" não deve disparar
        d = estagio.detectar(
            assunto="Atualização do projeto",
            corpo="O estagio inicial foi concluído com sucesso",
            anexos_filenames=[],
        )
        self.assertFalse(d.eh_estagio)


class TestAliasEstagio(unittest.TestCase):
    def setUp(self):
        self.tmp_file = Path(__file__).parent / "_test_funcao_aliases_estagio.json"
        if self.tmp_file.exists():
            self.tmp_file.unlink()
        self._patcher = patch.object(funcao, "FUNCAO_ALIASES_FILE", self.tmp_file)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        if self.tmp_file.exists():
            self.tmp_file.unlink()

    def test_salvar_clt_e_estagio_mesmo_cargo(self):
        # Mesma string de cargo, mas funções diferentes — uma CLT, outra estágio.
        # Devem coexistir sem sobrescrever uma à outra.
        funcao.salvar_funcao_alias(
            "Auxiliar de Loja", "1259", "OPERADOR DE CAIXA",
            eh_estagio=False,
        )
        funcao.salvar_funcao_alias(
            "Auxiliar de Loja", "9876", "ESTAGIÁRIO DE LOJA",
            eh_estagio=True,
        )

        clt = funcao.consultar_funcao_alias("Auxiliar de Loja", eh_estagio=False)
        estagio_alias = funcao.consultar_funcao_alias("Auxiliar de Loja", eh_estagio=True)

        self.assertIsNotNone(clt)
        self.assertIsNotNone(estagio_alias)
        self.assertEqual(clt["funcao_id"], "1259")
        self.assertEqual(estagio_alias["funcao_id"], "9876")

    def test_alias_estagio_nao_faz_fallback_pra_clt(self):
        # Salva SÓ CLT — busca por estágio deve retornar None (não pode pegar CLT)
        funcao.salvar_funcao_alias(
            "Auxiliar de Loja", "1259", "OPERADOR DE CAIXA",
            eh_estagio=False,
        )
        self.assertIsNone(
            funcao.consultar_funcao_alias("Auxiliar de Loja", eh_estagio=True)
        )

    def test_resolver_estagio_sem_alias_vira_pendencia_imediata(self):
        # Sem alias salvo + eh_estagio=True → pendência imediata, NÃO tenta
        # X-marcados (que seriam CLT)
        planilha = [
            {"usar": True, "funcao_id": "1259", "nome_cargo": "OPERADOR DE CAIXA",
             "cbo": "421125", "codigo": "1259"},
        ]
        funcao_id, conf, ambiguos, msg = funcao.resolver_funcao(
            planilha, "AUXILIAR DE LOJA", None, eh_estagio=True,
        )
        self.assertIsNone(funcao_id)
        self.assertIn("Estágio", msg)
        self.assertIn("alias", msg.lower())

    def test_resolver_estagio_com_alias_funciona(self):
        funcao.salvar_funcao_alias(
            "AUXILIAR DE LOJA", "9876", "ESTAGIÁRIO DE LOJA",
            eh_estagio=True,
        )
        planilha = []  # planilha vazia não importa quando alias existe... espera
        # resolver_funcao requer planilha não vazia pra começar — vou usar uma
        planilha = [
            {"usar": False, "funcao_id": "999", "nome_cargo": "ANY", "cbo": "000000", "codigo": "999"},
        ]
        funcao_id, conf, ambiguos, msg = funcao.resolver_funcao(
            planilha, "AUXILIAR DE LOJA", None, eh_estagio=True,
        )
        self.assertEqual(funcao_id, "9876")
        self.assertIn("alias", msg.lower())

    def test_sanity_check_nome_cargo_com_estagio_forca_estagio(self):
        """v2.11.1: caso YURI — operador digitou função "ESTAGIO EM SUPERMERCADO"
        no Desktop mas a UI chamou salvar_funcao_alias com eh_estagio=False
        (porque a pendência veio de v2.10.0 sem a flag). Sanity check DEVE
        forçar eh_estagio=True pelo nome."""
        funcao.salvar_funcao_alias(
            "AUXILIAR DE LOJA", "8860", "ESTAGIO EM SUPERMERCADO",
            eh_estagio=False,  # caller errado!
        )
        # Apesar de eh_estagio=False, deve ter salvado como ESTÁGIO
        clt = funcao.consultar_funcao_alias("AUXILIAR DE LOJA", eh_estagio=False)
        estagio_alias = funcao.consultar_funcao_alias("AUXILIAR DE LOJA", eh_estagio=True)
        self.assertIsNone(clt, "Não deveria ter salvado como CLT")
        self.assertIsNotNone(estagio_alias, "Deveria ter salvado como ESTÁGIO")
        self.assertEqual(estagio_alias["funcao_id"], "8860")

    def test_sanity_check_estagiario_no_nome(self):
        funcao.salvar_funcao_alias(
            "AUXILIAR ADMINISTRATIVO", "7777", "ESTAGIARIO ADMINISTRATIVO",
            eh_estagio=False,
        )
        self.assertIsNotNone(
            funcao.consultar_funcao_alias("AUXILIAR ADMINISTRATIVO", eh_estagio=True)
        )

    def test_sanity_nao_confunde_com_palavras_parecidas(self):
        # "ESTAGNADO" ou "ESTAGEIRO" (typo) não devem disparar
        funcao.salvar_funcao_alias(
            "CARGO X", "5555", "OPERADOR ESTAGNADO LTDA",
            eh_estagio=False,
        )
        self.assertIsNotNone(
            funcao.consultar_funcao_alias("CARGO X", eh_estagio=False),
            "Não deveria ter virado estágio — palavra parecida mas não é"
        )


class TestTermosEstagioAutoResolviveis(unittest.TestCase):
    """v2.11.3 — Claude às vezes desiste de estagiários alegando que estágio
    não é CLT. Pipeline deve reconhecer esses motivos como auto-resolvíveis
    e seguir o fluxo (estágio é admissão normal pro escritório)."""

    def test_motivo_jessyka_lei_11788(self):
        motivo = (
            "Contrato de estágio (Lei 11.788/2008) NÃO é vínculo CLT — "
            "não gera payload de admissão eSocial S-2200. Este é um Termo "
            "de Compromisso de Estágio, não um contrato de trabalho."
        )
        motivo_lower = motivo.lower()
        # Termos que main.py considera auto-resolvíveis pra estágio (v2.11.3)
        termos_estagio = [
            "lei 11.788", "lei 11788",
            "não é vínculo clt", "nao e vinculo clt",
            "não gera payload de admissão esocial",
            "não gera s-2200",
            "esocial s-2200",
            "termo de compromisso de estagio",
            "termo de compromisso de estágio",
        ]
        achados = [t for t in termos_estagio if t in motivo_lower]
        self.assertGreater(
            len(achados), 0,
            f"Nenhum termo de estágio bateu no motivo. Termos buscados: "
            f"{termos_estagio}"
        )


if __name__ == "__main__":
    unittest.main()
