"""Testes pra inferência de empresa a partir do remetente do email (v2.10.0)."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

import remetente_empresa as re_mod
from empresas_cache import EmpresasCache


def fake_cache() -> EmpresasCache:
    """Cache pequeno pra testes — 4 empresas típicas."""
    cache = EmpresasCache()
    cache.cnpjs = {
        "10560396000185",  # MODELOFARMA
        "13513736000377",  # R LOG RIO TRANSPORTE
        "04584726000331",  # MOTO BRASIL PEÇAS
        "33014889000115",  # RODOVALHO ADMINISTRACAO
    }
    cache.detalhes = {
        "10560396000185": {"empresa_id": "89", "razao_social": "MODELOFARMA LTDA"},
        "13513736000377": {"empresa_id": "47", "razao_social": "R LOG RIO TRANSPORTE E LOGISTICA LTDA ME"},
        "04584726000331": {"empresa_id": "982", "razao_social": "MOTO BRASIL PECAS E ACESSORIOS LTDA"},
        "33014889000115": {"empresa_id": "210", "razao_social": "RODOVALHO ADMINISTRACAO E SERVICOS LTDA"},
    }
    cache.carregado = True
    return cache


class TestNormalizadores(unittest.TestCase):
    def test_norm_email_simples(self):
        self.assertEqual(re_mod._norm_email("RH@Modelofarma.COM.BR"), "rh@modelofarma.com.br")

    def test_norm_email_com_nome_entre_brackets(self):
        self.assertEqual(
            re_mod._norm_email('"Maria DP" <maria@xpto.com>'),
            "maria@xpto.com",
        )

    def test_norm_email_vazio(self):
        self.assertEqual(re_mod._norm_email(""), "")
        self.assertEqual(re_mod._norm_email(None), "")

    def test_extrair_dominio(self):
        self.assertEqual(re_mod._extrair_dominio("rh@modelofarma.com.br"), "modelofarma.com.br")
        self.assertEqual(re_mod._extrair_dominio("Maria <m@xpto.com>"), "xpto.com")
        self.assertEqual(re_mod._extrair_dominio(""), "")


class TestExtracaoCandidatosEmpresa(unittest.TestCase):
    def test_uppercase_simples(self):
        c = re_mod._extrair_candidatos_empresa("Admissão da Paula na MODELOFARMA LTDA hoje")
        self.assertTrue(any("MODELOFARMA" in x for x in c))

    def test_sufixo_societario(self):
        c = re_mod._extrair_candidatos_empresa("Empresa: Padaria do Zé Ltda contratou João")
        self.assertTrue(any("Padaria" in x for x in c))

    def test_texto_sem_empresa(self):
        c = re_mod._extrair_candidatos_empresa("admissao do joao, salario 2000")
        self.assertEqual(c, [])


class TestPersistenciaAliases(unittest.TestCase):
    def setUp(self):
        # Aponta o arquivo de aliases pra um tmp
        self.tmp_file = Path(__file__).parent / "_test_remetente_aliases.json"
        if self.tmp_file.exists():
            self.tmp_file.unlink()
        self._patcher = patch.object(re_mod, "REMETENTE_ALIASES_FILE", self.tmp_file)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        if self.tmp_file.exists():
            self.tmp_file.unlink()

    def test_carregar_vazio(self):
        self.assertEqual(re_mod.carregar_aliases(), {})

    def test_salvar_e_consultar(self):
        re_mod.salvar_alias("rh@modelofarma.com.br", "10560396000185",
                            "MODELOFARMA LTDA", fonte="manual")
        entry = re_mod.consultar_alias_exato("RH@Modelofarma.COM.BR")  # case insensitive
        self.assertIsNotNone(entry)
        self.assertEqual(entry["cnpj"], "10560396000185")
        self.assertEqual(entry["fonte"], "manual")

    def test_salvar_com_nome_entre_brackets(self):
        re_mod.salvar_alias('"Maria DP" <maria@x.com>', "11111111000111", "X SA")
        entry = re_mod.consultar_alias_exato("maria@x.com")
        self.assertIsNotNone(entry)


class TestInferenciaPorDominio(unittest.TestCase):
    def test_dominio_unico_match(self):
        cache = fake_cache()
        r = re_mod.inferir_por_dominio("rh@modelofarma.com.br", cache)
        self.assertIsNotNone(r)
        self.assertEqual(r["cnpj"], "10560396000185")
        self.assertEqual(r["estrategia"], "dominio")

    def test_dominio_generico_pulado(self):
        cache = fake_cache()
        self.assertIsNone(re_mod.inferir_por_dominio("alguem@gmail.com", cache))
        self.assertIsNone(re_mod.inferir_por_dominio("alguem@hotmail.com", cache))

    def test_raiz_curta_pulada(self):
        cache = fake_cache()
        # rl.com seria perigoso (raiz 'rl' bate com tudo)
        self.assertIsNone(re_mod.inferir_por_dominio("rh@rl.com.br", cache))

    def test_email_vazio(self):
        self.assertIsNone(re_mod.inferir_por_dominio("", fake_cache()))


class TestInferenciaPorRazaoFuzzy(unittest.TestCase):
    def test_match_no_corpo(self):
        cache = fake_cache()
        texto = "Admissão da Paula na MODELOFARMA LTDA, salário R$ 1800"
        r = re_mod.inferir_por_razao_fuzzy(texto, cache)
        self.assertIsNotNone(r)
        self.assertEqual(r["cnpj"], "10560396000185")

    def test_substring_boost(self):
        # Só "MODELOFARMA" sem "LTDA" — deve casar por substring
        cache = fake_cache()
        texto = "Assunto: ADMISSAO MODELOFARMA"
        r = re_mod.inferir_por_razao_fuzzy(texto, cache)
        self.assertIsNotNone(r)
        self.assertEqual(r["cnpj"], "10560396000185")

    def test_sem_match(self):
        cache = fake_cache()
        # NOTA: dependendo do threshold, qualquer texto pode dar falso positivo.
        # Aqui usamos texto sem nenhuma palavra que pareça empresa.
        self.assertIsNone(re_mod.inferir_por_razao_fuzzy("admissao do joao", cache))


class TestResolverOrquestracao(unittest.TestCase):
    def setUp(self):
        self.tmp_file = Path(__file__).parent / "_test_remetente_aliases_resolver.json"
        if self.tmp_file.exists():
            self.tmp_file.unlink()
        self._patcher = patch.object(re_mod, "REMETENTE_ALIASES_FILE", self.tmp_file)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        if self.tmp_file.exists():
            self.tmp_file.unlink()

    def test_alias_tem_prioridade(self):
        cache = fake_cache()
        # Salva alias manual apontando pra empresa A
        re_mod.salvar_alias("rh@modelofarma.com.br", "13513736000377",
                            "R LOG RIO", fonte="manual")
        # Texto menciona MODELOFARMA — mas alias deve ganhar
        r = re_mod.resolver(
            remetente="rh@modelofarma.com.br",
            texto_email="MODELOFARMA LTDA admite Paula",
            cache=cache,
        )
        self.assertIsNotNone(r)
        self.assertEqual(r["cnpj"], "13513736000377")
        self.assertEqual(r["estrategia"], "alias_manual")

    def test_fallback_dominio_quando_sem_alias(self):
        cache = fake_cache()
        r = re_mod.resolver(
            remetente="contato@modelofarma.com.br",
            texto_email="oi",
            cache=cache,
        )
        self.assertIsNotNone(r)
        self.assertEqual(r["estrategia"], "dominio")

    def test_fallback_razao_quando_dominio_generico(self):
        cache = fake_cache()
        r = re_mod.resolver(
            remetente="rh@gmail.com",
            texto_email="Admissão na MODELOFARMA LTDA",
            cache=cache,
        )
        self.assertIsNotNone(r)
        self.assertIn("fuzzy", r["estrategia"])

    def test_nada_bate_retorna_none(self):
        cache = fake_cache()
        r = re_mod.resolver(
            remetente="x@hotmail.com",
            texto_email="admissao do joao",
            cache=cache,
        )
        self.assertIsNone(r)

    def test_cache_nao_carregado(self):
        cache = EmpresasCache()  # vazio + não carregado
        r = re_mod.resolver("rh@modelofarma.com.br", "", cache)
        self.assertIsNone(r)


if __name__ == "__main__":
    unittest.main()
