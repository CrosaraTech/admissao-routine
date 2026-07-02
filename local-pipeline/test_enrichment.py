"""Testes pra enrichment.py — rodar com:
    .venv\\Scripts\\python.exe -m unittest test_enrichment.py -v

Testes do ViaCEP usam unittest.mock pra não depender da rede.
"""
from __future__ import annotations

import unittest
from unittest import mock

from enrichment import (
    _carregar_estados,
    _formatar_cep,
    _so_digitos,
    _upper,
    _upper_sem_pontuacao,
    apply_ctps_from_cpf,
    apply_fixed_defaults,
    enrich_candidato,
    enrich_from_cep,
    enrich_from_cpf,
)


class TestHelpers(unittest.TestCase):
    def test_formatar_cep_valido(self):
        self.assertEqual(_formatar_cep("75250000"), "75250-000")
        self.assertEqual(_formatar_cep("75250-000"), "75250-000")
        self.assertEqual(_formatar_cep("75.250-000"), "75250-000")

    def test_formatar_cep_invalido(self):
        self.assertIsNone(_formatar_cep("123"))
        self.assertIsNone(_formatar_cep(""))
        self.assertIsNone(_formatar_cep("abcdefgh"))

    def test_upper(self):
        self.assertEqual(_upper("rua xpto"), "RUA XPTO")
        self.assertEqual(_upper("  rua  "), "RUA")
        self.assertIsNone(_upper(""))
        self.assertIsNone(_upper(None))

    def test_so_digitos(self):
        self.assertEqual(_so_digitos("857.887.745-43"), "85788774543")
        self.assertEqual(_so_digitos("abc"), "")

    def test_carregar_estados(self):
        estados = _carregar_estados()
        self.assertEqual(estados.get("GO"), "9")
        self.assertEqual(estados.get("SP"), "25")
        self.assertEqual(estados.get("PE"), "17")


class TestViaCEP(unittest.TestCase):
    """ViaCEP mockado — não bate na rede real."""

    def setUp(self):
        # Limpa cache entre testes
        import enrichment
        enrichment._VIACEP_CACHE.clear()

    @mock.patch("enrichment.httpx.Client")
    def test_cep_valido(self, mock_client):
        # Simula resposta real do ViaCEP pro CEP 74000-000 (Goiânia/GO)
        mock_response = mock.MagicMock()
        mock_response.json.return_value = {
            "cep": "74000-000",
            "logradouro": "Rua Fagundes Varela",
            "bairro": "Setor Bueno",
            "localidade": "Goiânia",
            "uf": "GO",
        }
        mock_response.raise_for_status.return_value = None
        mock_client.return_value.__enter__.return_value.get.return_value = mock_response

        out = enrich_from_cep("74000-000")

        self.assertEqual(out.get("rua"), "RUA FAGUNDES VARELA")
        self.assertEqual(out.get("bairro"), "SETOR BUENO")
        self.assertEqual(out.get("cidade"), "GOIÂNIA")
        self.assertEqual(out.get("cep"), "74000-000")
        self.assertEqual(out.get("_estado_id"), "9")  # GO
        self.assertEqual(out.get("_uf"), "GO")

    @mock.patch("enrichment.httpx.Client")
    def test_cep_inexistente_retorna_erro_true(self, mock_client):
        # ViaCEP retorna {"erro": true} pra CEPs que não existem
        mock_response = mock.MagicMock()
        mock_response.json.return_value = {"erro": True}
        mock_response.raise_for_status.return_value = None
        mock_client.return_value.__enter__.return_value.get.return_value = mock_response

        out = enrich_from_cep("99999-999")
        self.assertEqual(out, {})

    def test_cep_formato_invalido(self):
        # Não deve nem chamar a API se o CEP for inválido
        self.assertEqual(enrich_from_cep("123"), {})
        self.assertEqual(enrich_from_cep(""), {})
        self.assertEqual(enrich_from_cep("abc-def"), {})

    @mock.patch("enrichment.httpx.Client")
    def test_cep_timeout_retorna_vazio_sem_quebrar(self, mock_client):
        # Simula timeout — não pode propagar exceção
        mock_client.return_value.__enter__.return_value.get.side_effect = (
            Exception("simulated timeout")
        )
        out = enrich_from_cep("74000-000")
        self.assertEqual(out, {})  # Falhou silenciosamente

    @mock.patch("enrichment.httpx.Client")
    def test_cache_evita_chamada_dupla(self, mock_client):
        mock_response = mock.MagicMock()
        mock_response.json.return_value = {
            "logradouro": "Rua X", "bairro": "Bairro Y",
            "localidade": "Goiânia", "uf": "GO",
        }
        mock_response.raise_for_status.return_value = None
        get = mock_client.return_value.__enter__.return_value.get
        get.return_value = mock_response

        enrich_from_cep("74000-000")
        enrich_from_cep("74000-000")  # segunda chamada usa cache
        self.assertEqual(get.call_count, 1)


class TestEnrichFromCpf(unittest.TestCase):
    def test_sem_provider_configurado_retorna_vazio(self):
        with mock.patch.dict("os.environ", {"CPF_LOOKUP_PROVIDER": ""}, clear=False):
            out = enrich_from_cpf("857.887.745-43")
            self.assertEqual(out, {})

    def test_cpf_invalido(self):
        self.assertEqual(enrich_from_cpf("123"), {})
        self.assertEqual(enrich_from_cpf(""), {})

    def test_provider_sem_token_avisa(self):
        # Quando provider configurado mas token ausente, retorna vazio
        env = {"CPF_LOOKUP_PROVIDER": "konsi", "CPF_LOOKUP_TOKEN": ""}
        with mock.patch.dict("os.environ", env, clear=False):
            out = enrich_from_cpf("85788774543")
            self.assertEqual(out, {})


class TestApplyFixedDefaults(unittest.TestCase):
    def test_aplica_em_payload_vazio(self):
        payload = {}
        apply_fixed_defaults(payload)
        attrs = payload["data"]["attributes"]
        self.assertEqual(attrs["primeiroemprego"], False)
        self.assertEqual(attrs["usuariocriacao"], "PIPELINE-V3")

        rels = payload["data"]["relationships"]
        self.assertEqual(rels["statusadmissao"]["data"]["id"], "1")
        self.assertEqual(rels["nacionalidade"]["data"]["id"], "105")
        self.assertEqual(rels["raca"]["data"]["id"], "2")

    def test_nao_sobrescreve_relationships_existentes(self):
        # Cliente já populou estadocivil — não pode ser tocado
        payload = {
            "data": {
                "attributes": {"primeiroemprego": True},  # já preenchido (true)
                "relationships": {
                    "statusadmissao": {"data": {"type": "tipos-status-admissao", "id": "9"}}
                },
            }
        }
        apply_fixed_defaults(payload)

        # primeiroemprego permanece true (não vira False)
        self.assertEqual(payload["data"]["attributes"]["primeiroemprego"], True)
        # statusadmissao permanece "9" (não vira "1")
        self.assertEqual(
            payload["data"]["relationships"]["statusadmissao"]["data"]["id"], "9"
        )
        # Mas raca foi preenchido porque estava faltando (default=2 Branca)
        self.assertEqual(
            payload["data"]["relationships"]["raca"]["data"]["id"], "2"
        )


class TestLimpezaPontuacaoEndereco(unittest.TestCase):
    """eContador rejeita vírgula/ponto/barra em rua/bairro/cidade/complemento."""

    def test_upper_sem_pontuacao_remove_virgula_ponto_barra(self):
        self.assertEqual(
            _upper_sem_pontuacao("Rua Ouro Preto, Q. 113, L. 02"),
            "RUA OURO PRETO Q 113 L 02"
        )
        self.assertEqual(
            _upper_sem_pontuacao("Av. José/Maria"),
            "AV JOSÉ MARIA"
        )

    def test_upper_sem_pontuacao_mantem_hifen_e_apostrofo(self):
        self.assertEqual(
            _upper_sem_pontuacao("Rua D'Avila"),
            "RUA D'AVILA"
        )
        self.assertEqual(
            _upper_sem_pontuacao("Bairro Vila-Real"),
            "BAIRRO VILA-REAL"
        )

    def test_upper_sem_pontuacao_colapsa_espacos(self):
        self.assertEqual(
            _upper_sem_pontuacao("Rua  X,  Q.  5"),
            "RUA X Q 5"
        )

    def test_upper_sem_pontuacao_vazio(self):
        self.assertIsNone(_upper_sem_pontuacao(""))
        self.assertIsNone(_upper_sem_pontuacao(None))

    def test_sanitizar_attributes_limpa_endereco(self):
        from payload_builder import sanitizar_attributes
        out = sanitizar_attributes({
            "rua": "RUA OURO PRETO, Q. 113, L. 02",
            "bairro": "SETOR BUENO/SUL",
            "cidade": "APARECIDA DE GOIÂNIA, GO",
            "complemento": "APTO. 101, BL. A",
        })
        self.assertEqual(out["rua"], "RUA OURO PRETO Q 113 L 02")
        self.assertEqual(out["bairro"], "SETOR BUENO SUL")
        self.assertEqual(out["cidade"], "APARECIDA DE GOIÂNIA GO")
        self.assertEqual(out["complemento"], "APTO 101 BL A")

    def test_sanitizar_nao_mexe_em_outros_campos(self):
        from payload_builder import sanitizar_attributes
        # nome, email não são endereço — não mexer mesmo se tiver pontuação
        out = sanitizar_attributes({
            "nome": "JOAO, DA SILVA",  # mantém — é nome, não endereço
            "email": "joao.silva@example.com",  # mantém (lowercase só)
        })
        self.assertEqual(out["nome"], "JOAO, DA SILVA")
        self.assertEqual(out["email"], "joao.silva@example.com")


class TestApplyCtpsFromCpf(unittest.TestCase):
    """Regra do escritório: CTPS sempre derivada do CPF.
    CPF 857.887.745-43 (dígitos: 85788774543):
      ctps      = int("8578877") = 8578877
      seriectps = "4543"          (posições [7:11])
    """

    def test_cpf_sem_ctps_gera_ctps(self):
        payload = {"data": {"attributes": {"cpf": 85788774543}}}
        _, mudou = apply_ctps_from_cpf(payload)
        attrs = payload["data"]["attributes"]
        self.assertTrue(mudou)
        self.assertEqual(attrs["ctps"], 8578877)
        self.assertEqual(attrs["seriectps"], "4543")

    def test_cpf_com_ctps_igual_ao_derivado_nao_muda(self):
        payload = {"data": {"attributes": {
            "cpf": 85788774543, "ctps": 8578877, "seriectps": "4543",
        }}}
        _, mudou = apply_ctps_from_cpf(payload)
        self.assertFalse(mudou)

    def test_cpf_com_ctps_diferente_substitui(self):
        """Regra do escritório: CPF GANHA contra Claude."""
        payload = {"data": {"attributes": {
            "cpf": 85788774543,
            "ctps": 9999999,   # Claude extraiu da carteira física, mas...
            "seriectps": "0001",
        }}}
        _, mudou = apply_ctps_from_cpf(payload)
        attrs = payload["data"]["attributes"]
        self.assertTrue(mudou)
        # ...regra do CPF ganha sempre
        self.assertEqual(attrs["ctps"], 8578877)
        self.assertEqual(attrs["seriectps"], "4543")

    def test_cpf_com_zeros_a_esquerda(self):
        """CPF 01234567890 (10 dígitos como int — perdeu o zero da frente).
        zfill restaura o zero: '01234567890'.
          ctps = int('0123456') = 123456
          seriectps = '7890'
        """
        payload = {"data": {"attributes": {"cpf": 1234567890}}}
        _, mudou = apply_ctps_from_cpf(payload)
        attrs = payload["data"]["attributes"]
        self.assertTrue(mudou)
        self.assertEqual(attrs["ctps"], 123456)
        self.assertEqual(attrs["seriectps"], "7890")

    def test_sem_cpf_nao_mexe(self):
        payload = {"data": {"attributes": {"nome": "X"}}}
        _, mudou = apply_ctps_from_cpf(payload)
        self.assertFalse(mudou)
        self.assertNotIn("ctps", payload["data"]["attributes"])

    def test_cpf_invalido_nao_mexe(self):
        payload = {"data": {"attributes": {"cpf": "abc"}}}
        _, mudou = apply_ctps_from_cpf(payload)
        self.assertFalse(mudou)

    def test_ufctps_copia_de_ufidentidade(self):
        payload = {
            "data": {
                "attributes": {"cpf": 85788774543},
                "relationships": {
                    "ufidentidade": {"data": {"type": "estados", "id": "5"}}  # BA
                }
            }
        }
        apply_ctps_from_cpf(payload)
        rels = payload["data"]["relationships"]
        self.assertEqual(rels["ufctps"]["data"]["id"], "5")

    def test_ufctps_existente_nao_sobrescreve(self):
        payload = {
            "data": {
                "attributes": {"cpf": 85788774543},
                "relationships": {
                    "ufidentidade": {"data": {"type": "estados", "id": "5"}},
                    "ufctps":       {"data": {"type": "estados", "id": "9"}},  # já tinha GO
                }
            }
        }
        apply_ctps_from_cpf(payload)
        # ufctps mantém GO (9), não troca pra BA (5)
        self.assertEqual(
            payload["data"]["relationships"]["ufctps"]["data"]["id"], "9"
        )


class TestEnrichCandidato(unittest.TestCase):
    """Teste do orquestrador end-to-end (com ViaCEP mockado)."""

    def setUp(self):
        import enrichment
        enrichment._VIACEP_CACHE.clear()

    @mock.patch("enrichment.httpx.Client")
    def test_payload_parcial_com_cep_enriquece_via_viacep(self, mock_client):
        mock_response = mock.MagicMock()
        mock_response.json.return_value = {
            "logradouro": "Rua Fagundes Varela",
            "bairro": "Setor Bueno",
            "localidade": "Goiânia",
            "uf": "GO",
        }
        mock_response.raise_for_status.return_value = None
        mock_client.return_value.__enter__.return_value.get.return_value = mock_response

        payload_in = {
            "data": {
                "type": "candidatos",
                "attributes": {
                    "nome": "JOAO TESTE",
                    "cpf": 12345678901,
                    "cep": "74000-000",
                    "admissao": "2026-06-01",
                    "salario": 1500.0,
                },
            }
        }
        out = enrich_candidato(payload_in)

        # Endereço veio do ViaCEP
        self.assertEqual(out["data"]["attributes"]["rua"], "RUA FAGUNDES VARELA")
        self.assertEqual(out["data"]["attributes"]["cidade"], "GOIÂNIA")
        # Defaults aplicados
        self.assertEqual(
            out["data"]["relationships"]["statusadmissao"]["data"]["id"], "1"
        )
        # Meta de auditoria presente
        meta = out["_enrich_meta"]
        self.assertIn("rua", meta["fields_filled_by_cep"])
        self.assertIn("estado", meta["fields_filled_by_cep"])
        self.assertGreater(len(meta["fields_still_missing"]), 0)
        self.assertTrue(meta["ocr_required"])

    def test_sem_cep_nem_cpf_so_aplica_defaults(self):
        payload_in = {
            "data": {
                "type": "candidatos",
                "attributes": {"nome": "JOAO TESTE"},
            }
        }
        out = enrich_candidato(payload_in)
        # Defaults presentes (raca=2 Branca, briefing §8.1)
        self.assertEqual(
            out["data"]["relationships"]["raca"]["data"]["id"], "2"
        )
        # Meta vazia pra CEP/CPF
        self.assertEqual(out["_enrich_meta"]["fields_filled_by_cep"], [])
        self.assertEqual(out["_enrich_meta"]["fields_filled_by_cpf"], [])

    def test_nao_quebra_com_payload_invalido(self):
        # Não pode levantar exceção
        out = enrich_candidato({})
        self.assertIn("_enrich_meta", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
