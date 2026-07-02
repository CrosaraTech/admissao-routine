"""Testes pra sistema de aliases globais de cargo em funcao.py.

Rodar com:
    .venv\\Scripts\\python.exe -m unittest test_funcao_aliases.py -v

Estratégia: tempfile + mock.patch isola FUNCAO_ALIASES_FILE no módulo
funcao.py pra não tocar no arquivo real do projeto.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import funcao
from funcao import (
    _norm,
    carregar_funcao_aliases,
    consultar_funcao_alias,
    resolver_funcao,
    salvar_funcao_alias,
)


class TestCarregarFuncaoAliases(unittest.TestCase):
    """Caso 1: arquivo inexistente retorna {}."""

    def test_arquivo_inexistente_retorna_dict_vazio(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "nao_existe.json"
            self.assertFalse(fake_path.exists())
            with mock.patch.object(funcao, "FUNCAO_ALIASES_FILE", fake_path):
                out = carregar_funcao_aliases()
            self.assertEqual(out, {})

    def test_arquivo_corrompido_retorna_dict_vazio(self):
        """Bonus: JSON inválido também devolve {} (não levanta exceção)."""
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "lixo.json"
            fake_path.write_text("isso não é json {{{", encoding="utf-8")
            with mock.patch.object(funcao, "FUNCAO_ALIASES_FILE", fake_path):
                out = carregar_funcao_aliases()
            self.assertEqual(out, {})


class TestSalvarFuncaoAlias(unittest.TestCase):
    """Caso 2: persistência — escreve e read-back confere."""

    def test_salvar_persiste_e_read_back_confere(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "aliases.json"
            with mock.patch.object(funcao, "FUNCAO_ALIASES_FILE", fake_path):
                salvar_funcao_alias(
                    cargo="AUXILIAR DE SERVIÇOS GERAIS",
                    funcao_id="12345",
                    nome_cargo="AUXILIAR DE LIMPEZA",
                    observacoes="Cliente X manda sempre assim",
                )
                # Read-back via API
                aliases = carregar_funcao_aliases()

            chave = _norm("AUXILIAR DE SERVIÇOS GERAIS")
            self.assertIn(chave, aliases)
            entry = aliases[chave]
            self.assertEqual(entry["funcao_id"], "12345")
            self.assertEqual(entry["nome_cargo"], "AUXILIAR DE LIMPEZA")
            self.assertEqual(entry["observacoes"], "Cliente X manda sempre assim")
            self.assertIn("criado_em", entry)

            # Confere também via arquivo cru (não dependendo só da API)
            raw = json.loads(fake_path.read_text(encoding="utf-8"))
            self.assertEqual(raw[chave]["funcao_id"], "12345")

    def test_salvar_atualiza_alias_existente(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "aliases.json"
            with mock.patch.object(funcao, "FUNCAO_ALIASES_FILE", fake_path):
                salvar_funcao_alias("MOTORISTA", "111", "MOTORISTA CATEGORIA D")
                salvar_funcao_alias("MOTORISTA", "222", "MOTORISTA CATEGORIA E")
                aliases = carregar_funcao_aliases()

            chave = _norm("MOTORISTA")
            self.assertEqual(len(aliases), 1)
            self.assertEqual(aliases[chave]["funcao_id"], "222")
            self.assertEqual(aliases[chave]["nome_cargo"], "MOTORISTA CATEGORIA E")

    def test_salvar_cargo_vazio_levanta(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "aliases.json"
            with mock.patch.object(funcao, "FUNCAO_ALIASES_FILE", fake_path):
                with self.assertRaises(ValueError):
                    salvar_funcao_alias("", "123", "FOO")
                with self.assertRaises(ValueError):
                    salvar_funcao_alias("   ", "123", "FOO")

    def test_salvar_funcao_id_vazio_levanta(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "aliases.json"
            with mock.patch.object(funcao, "FUNCAO_ALIASES_FILE", fake_path):
                with self.assertRaises(ValueError):
                    salvar_funcao_alias("MOTORISTA", "", "FOO")


class TestConsultarFuncaoAlias(unittest.TestCase):
    """Caso 3: match case-insensitive via _norm; Caso 4: None quando não mapeado."""

    def test_match_case_insensitive_e_sem_acento(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "aliases.json"
            with mock.patch.object(funcao, "FUNCAO_ALIASES_FILE", fake_path):
                salvar_funcao_alias(
                    cargo="AUXILIAR DE SERVIÇOS GERAIS",
                    funcao_id="12345",
                    nome_cargo="AUXILIAR DE LIMPEZA",
                )

                # Variantes que devem bater (case + acento + espaços)
                variantes = [
                    "auxiliar de serviços gerais",
                    "Auxiliar De Servicos Gerais",
                    "AUXILIAR DE SERVICOS GERAIS",
                    "  auxiliar  de   servicos  gerais  ",
                    "AUXILIAR DE SERVIÇOS GERAIS",
                ]
                for v in variantes:
                    with self.subTest(variante=v):
                        entry = consultar_funcao_alias(v)
                        self.assertIsNotNone(entry, f"Falhou pra: {v!r}")
                        self.assertEqual(entry["funcao_id"], "12345")
                        self.assertEqual(entry["nome_cargo"], "AUXILIAR DE LIMPEZA")

    def test_cargo_nao_mapeado_retorna_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "aliases.json"
            with mock.patch.object(funcao, "FUNCAO_ALIASES_FILE", fake_path):
                salvar_funcao_alias("MOTORISTA", "111", "MOTORISTA D")
                # Cargo diferente — não tem alias
                self.assertIsNone(consultar_funcao_alias("VENDEDOR"))
                self.assertIsNone(consultar_funcao_alias("AUXILIAR ADMINISTRATIVO"))

    def test_cargo_vazio_retorna_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "aliases.json"
            with mock.patch.object(funcao, "FUNCAO_ALIASES_FILE", fake_path):
                self.assertIsNone(consultar_funcao_alias(""))
                self.assertIsNone(consultar_funcao_alias("   "))


class TestResolverFuncaoComAlias(unittest.TestCase):
    """Caso 5: alias usado mesmo sem X-marcados; Caso 6: ignora se cargo vazio."""

    def test_alias_resolve_mesmo_sem_x_marcados(self):
        """Planilha SEM nenhum X-marcado normalmente vira pendência, mas
        se houver alias o resolver deve devolver o id do alias."""
        planilha = [
            # Nenhum item com usar=True
            {"nome_cargo": "VENDEDOR", "cbo": "521110", "funcao_id": "1", "usar": False},
            {"nome_cargo": "MOTORISTA", "cbo": "782310", "funcao_id": "2", "usar": False},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "aliases.json"
            with mock.patch.object(funcao, "FUNCAO_ALIASES_FILE", fake_path):
                salvar_funcao_alias(
                    cargo="AUXILIAR DE SERVIÇOS GERAIS",
                    funcao_id="9999",
                    nome_cargo="AUXILIAR DE LIMPEZA",
                )
                fid, conf, amb, msg = resolver_funcao(
                    planilha, "AUXILIAR DE SERVIÇOS GERAIS", None
                )

        self.assertEqual(fid, "9999")
        self.assertEqual(conf, 1.0)
        self.assertEqual(amb, [])
        self.assertIn("alias", msg)

    def test_alias_resolve_independente_de_acento_e_caixa(self):
        """O resolver deve achar o alias mesmo que o cargo extraído venha
        em caixa baixa ou sem acento."""
        planilha = [
            {"nome_cargo": "OUTRO CARGO", "cbo": "999999", "funcao_id": "1", "usar": True},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "aliases.json"
            with mock.patch.object(funcao, "FUNCAO_ALIASES_FILE", fake_path):
                salvar_funcao_alias(
                    cargo="AUXILIAR DE SERVIÇOS GERAIS",
                    funcao_id="9999",
                    nome_cargo="AUXILIAR DE LIMPEZA",
                )
                fid, conf, amb, msg = resolver_funcao(
                    planilha, "auxiliar de servicos gerais", None
                )

        self.assertEqual(fid, "9999")
        self.assertEqual(conf, 1.0)

    def test_alias_ignorado_quando_cargo_vazio(self):
        """Cargo extraído vazio + sem CBO → não tenta alias, devolve motivo
        'Cargo e CBO não informados'."""
        planilha = [
            {"nome_cargo": "VENDEDOR", "cbo": "521110", "funcao_id": "1", "usar": True},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "aliases.json"
            with mock.patch.object(funcao, "FUNCAO_ALIASES_FILE", fake_path):
                # Salva alias mas com cargo vazio o resolver não deve usar
                salvar_funcao_alias("QUALQUER COISA", "9999", "ALGUM CARGO")

                # cargo vazio + sem CBO
                fid, conf, amb, msg = resolver_funcao(planilha, "", None)
                self.assertIsNone(fid)
                self.assertIn("não informados", msg)

                # cargo None + sem CBO
                fid2, _, _, msg2 = resolver_funcao(planilha, None, None)
                self.assertIsNone(fid2)
                self.assertIn("não informados", msg2)

    def test_sem_alias_cai_no_fluxo_normal_x_marcados(self):
        """Sanity check: se não tem alias, segue o fluxo de X-marcados normalmente."""
        planilha = [
            {"nome_cargo": "VENDEDOR", "cbo": "521110", "funcao_id": "1", "usar": True},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / "aliases.json"
            # arquivo nem existe — nenhum alias salvo
            with mock.patch.object(funcao, "FUNCAO_ALIASES_FILE", fake_path):
                fid, conf, amb, msg = resolver_funcao(planilha, "VENDEDOR", "521110")

        # Match exato pelo nome + CBO entre X-marcados
        self.assertEqual(fid, "1")
        self.assertEqual(msg, "ok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
