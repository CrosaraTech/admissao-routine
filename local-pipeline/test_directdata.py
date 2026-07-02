"""Testes do directdata_client + directdata_mapper.
Mocks httpx pra não bater na API real (que custa dinheiro).

Roda com:
    .venv\\Scripts\\python.exe -m unittest test_directdata -v
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from directdata_client import DirectDataClient
from directdata_mapper import (
    SEXO_TO_ID,
    limpar_texto,
    map_cadastro_basico,
    map_pis,
    map_titulo,
    parse_data_nascimento,
    upper,
)


# ─── Retornos reais (capturados pelo user no spec) ──────────────────

RETORNO_CADASTRO_REAL = {
    "cpf": "050.754.871-06",
    "nome": "JOAO MARCOS ALVES ALEIXO MOREIRA",
    "sexo": "Masculino",
    "dataNascimento": "13/03/2002 00:00:00",
    "nomeMae": "HADRIANE ALVES TEIXEIRA MOREIRA",
    "idade": 24,
    "telefones": [{
        "telefoneComDDD": "(62) 992454104",
        "operadora": "CLARO",
        "tipoTelefone": "TELEFONE MÓVEL",
        "whatsApp": True,
    }],
    "enderecos": [{
        "logradouro": "VILA DE ACESSO",
        "numero": "8",
        "complemento": "0 CD CONDOMINIO",
        "bairro": "CONJUNTO RESIDENCIAL COSTA VER",
        "cidade": "GOIANIA",
        "uf": "GO",
        "cep": "74482-514",
    }],
    "emails": [{"enderecoEmail": "exemplo@gmail.com"}],
    "rendaEstimada": "2282.60",
}

RETORNO_PIS_REAL = {
    "pis": "139.13012.98-1",
    "cpf": "050.754.871-06",
    "nome": "JOAO MARCOS ALVES ALEIXO MOREIRA",
    "nomeMae": "HADRIANE ALVES TEIXEIRA MOREIRA",
    "dataNascimento": "13/03/2002 00:00:00",
}

RETORNO_TSE_OK = {
    "identificacao": {"inscricao": "140569130582", "eleitor": "050.754.871-06"},
    "domicilioEleitoral": {"zona": "119", "secao": "0159",
                            "local": "...", "municipio": "...", "uf": "GO"},
    "biometriaColetada": True,
    "status": "Regular",
}

RETORNO_TSE_NAO_ELEITOR = {
    "identificacao": {"inscricao": None},
    "domicilioEleitoral": {"zona": None, "secao": None},
    "status": "Não foi possível identificá-lo no Cadastro Eleitoral.",
}


# ═══════════════════════════════════════════════════════════════════
# MAPPER — testes puros (sem rede)
# ═══════════════════════════════════════════════════════════════════

class TestParseDataNascimento(unittest.TestCase):
    def test_formato_com_hora(self):
        self.assertEqual(parse_data_nascimento("13/03/2002 00:00:00"), "2002-03-13")

    def test_formato_sem_hora(self):
        self.assertEqual(parse_data_nascimento("13/03/2002"), "2002-03-13")

    def test_ja_iso(self):
        self.assertEqual(parse_data_nascimento("2002-03-13"), "2002-03-13")

    def test_invalido(self):
        self.assertIsNone(parse_data_nascimento(""))
        self.assertIsNone(parse_data_nascimento(None))
        self.assertIsNone(parse_data_nascimento("abc"))


class TestLimparTexto(unittest.TestCase):
    def test_remove_pontuacao(self):
        self.assertEqual(limpar_texto("RUA BRASIL, 123"), "RUA BRASIL 123")
        self.assertEqual(limpar_texto("AV. PAULISTA"), "AV PAULISTA")
        self.assertEqual(limpar_texto("SETOR/SUL"), "SETOR SUL")

    def test_upper_e_colapsa_espacos(self):
        self.assertEqual(limpar_texto("  rua  x,  q  5  "), "RUA X Q 5")

    def test_vazio(self):
        self.assertIsNone(limpar_texto(""))
        self.assertIsNone(limpar_texto(None))


class TestMapCadastroBasico(unittest.TestCase):
    def test_mapeamento_completo_real(self):
        out = map_cadastro_basico(RETORNO_CADASTRO_REAL)
        self.assertEqual(out["nome"], "JOAO MARCOS ALVES ALEIXO MOREIRA")
        self.assertEqual(out["nomedamae"], "HADRIANE ALVES TEIXEIRA MOREIRA")
        self.assertEqual(out["nascimento"], "2002-03-13")
        self.assertEqual(out["_sexo_id"], "1")  # Masculino
        self.assertEqual(out["cep"], "74482-514")
        self.assertEqual(out["rua"], "VILA DE ACESSO")
        self.assertEqual(out["numero"], 8)  # INT, não string
        self.assertEqual(out["bairro"], "CONJUNTO RESIDENCIAL COSTA VER")
        self.assertEqual(out["cidade"], "GOIANIA")
        self.assertEqual(out["_estado_id"], "9")  # GO
        self.assertEqual(out["celular"], "62992454104")
        self.assertEqual(out["email"], "exemplo@gmail.com")

    def test_sexo_feminino(self):
        out = map_cadastro_basico({"sexo": "Feminino"})
        self.assertEqual(out["_sexo_id"], "2")

    def test_numero_zero_quando_ausente(self):
        out = map_cadastro_basico({"enderecos": [{"logradouro": "RUA X"}]})
        # Sem campo numero → 0
        self.assertEqual(out["numero"], 0)

    def test_numero_string_nao_numerica(self):
        out = map_cadastro_basico({"enderecos": [{"numero": "SN"}]})
        self.assertEqual(out["numero"], 0)

    def test_entrada_vazia(self):
        self.assertEqual(map_cadastro_basico({}), {})
        self.assertEqual(map_cadastro_basico(None), {})

    def test_endereco_com_pontuacao_eh_limpo(self):
        out = map_cadastro_basico({
            "enderecos": [{"logradouro": "RUA X, Q 5", "bairro": "SETOR/SUL", "uf": "GO"}]
        })
        self.assertEqual(out["rua"], "RUA X Q 5")
        self.assertEqual(out["bairro"], "SETOR SUL")


class TestMapPis(unittest.TestCase):
    def test_pis_com_formatacao(self):
        out = map_pis({"pis": "139.13012.98-1"})
        self.assertEqual(out["pis"], "13913012981")

    def test_pis_preserva_zeros_a_esquerda(self):
        out = map_pis({"pis": "000.12345.67-8"})
        self.assertEqual(out["pis"], "00012345678")

    def test_pis_ausente(self):
        self.assertEqual(map_pis({}), {})
        self.assertEqual(map_pis({"pis": None}), {})
        self.assertEqual(map_pis({"pis": ""}), {})


class TestMapTitulo(unittest.TestCase):
    def test_eleitor_cadastrado(self):
        out = map_titulo(RETORNO_TSE_OK)
        self.assertEqual(out["tituloeleitor"], "140569130582")
        self.assertEqual(out["zonatituloeleitor"], "119")
        self.assertEqual(out["secaotituloeleitor"], "0159")

    def test_eleitor_nao_cadastrado_retorna_vazio(self):
        # Caso null: NÃO adiciona campos vazios no payload
        out = map_titulo(RETORNO_TSE_NAO_ELEITOR)
        self.assertEqual(out, {})

    def test_dados_invalidos(self):
        self.assertEqual(map_titulo({}), {})
        self.assertEqual(map_titulo(None), {})


# ═══════════════════════════════════════════════════════════════════
# CLIENT — testes com httpx mockado
# ═══════════════════════════════════════════════════════════════════

class TestDirectDataClient(unittest.TestCase):
    def setUp(self):
        # v2.14.1: isola o cache negativo persistente entre testes.
        # Patcheia NEG_CACHE_FILE pra um path temp único por teste (sem abrir
        # o arquivo — só pega path, pra Windows não travar com PermissionError).
        import directdata_client as ddc
        import os
        import tempfile
        fd, tmp_path = tempfile.mkstemp(suffix="_neg.json")
        os.close(fd)  # fecha o file descriptor pra Windows liberar
        self._tmp_neg = Path(tmp_path)
        if self._tmp_neg.exists():
            self._tmp_neg.unlink()
        self._patcher = mock.patch.object(ddc, "NEG_CACHE_FILE", self._tmp_neg)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        try:
            if self._tmp_neg.exists():
                self._tmp_neg.unlink()
        except (PermissionError, OSError):
            pass

    def test_sem_token_fica_desabilitado(self):
        with mock.patch.dict("os.environ", {"DIRECTDATA_TOKEN": ""}, clear=False):
            c = DirectDataClient()
            self.assertFalse(c.habilitado)
            # Chamadas viram no-op
            self.assertEqual(c.cadastro_basico("85788774543"), {})
            self.assertEqual(c.pis("85788774543"), {})
            self.assertEqual(c.titulo_eleitor("85788774543", "MAE", "13/03/2002"), {})

    def test_com_token_explicito_fica_habilitado(self):
        c = DirectDataClient(token="TESTE-TOKEN")
        self.assertTrue(c.habilitado)

    def test_cpf_invalido_retorna_vazio_sem_chamada(self):
        c = DirectDataClient(token="TESTE")
        with mock.patch("directdata_client.httpx.Client") as mh:
            self.assertEqual(c.cadastro_basico("123"), {})
            # Não chegou a chamar httpx
            mh.assert_not_called()

    @mock.patch("directdata_client.httpx.Client")
    def test_cadastro_basico_retorna_dados(self, mock_client):
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"retorno": RETORNO_CADASTRO_REAL}
        mock_client.return_value.__enter__.return_value.get.return_value = resp

        c = DirectDataClient(token="TESTE")
        out = c.cadastro_basico("05075487106")
        self.assertEqual(out["nome"], "JOAO MARCOS ALVES ALEIXO MOREIRA")

    @mock.patch("directdata_client.httpx.Client")
    def test_cache_evita_chamada_dupla(self, mock_client):
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"retorno": RETORNO_CADASTRO_REAL}
        get_mock = mock_client.return_value.__enter__.return_value.get
        get_mock.return_value = resp

        c = DirectDataClient(token="TESTE")
        c.cadastro_basico("05075487106")
        c.cadastro_basico("05075487106")  # mesma sessão
        # Só 1 chamada HTTP
        self.assertEqual(get_mock.call_count, 1)

    @mock.patch("directdata_client.httpx.Client")
    def test_http_erro_retorna_vazio(self, mock_client):
        resp = mock.MagicMock()
        resp.status_code = 500
        resp.text = "Internal Server Error"
        mock_client.return_value.__enter__.return_value.get.return_value = resp

        c = DirectDataClient(token="TESTE")
        out = c.cadastro_basico("05075487106")
        self.assertEqual(out, {})  # Não quebra pipeline

    @mock.patch("directdata_client.httpx.Client")
    def test_timeout_ou_excecao_retorna_vazio(self, mock_client):
        mock_client.return_value.__enter__.return_value.get.side_effect = (
            Exception("Connection reset")
        )
        c = DirectDataClient(token="TESTE")
        self.assertEqual(c.cadastro_basico("05075487106"), {})

    @mock.patch("directdata_client.httpx.Client")
    def test_custo_acumulado_e_contadores(self, mock_client):
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"retorno": RETORNO_CADASTRO_REAL}
        mock_client.return_value.__enter__.return_value.get.return_value = resp

        # v2.14.1: PIS é OFF por default — habilita explicitamente pra este
        # teste cobrir o cenário antigo (cadastro + PIS).
        c = DirectDataClient(token="TESTE", pis_habilitado=True)
        c.cadastro_basico("05075487106")
        c.pis("05075487106")  # mas vai fazer outra request → mockmos com PIS
        # Custo: 0,16 + 0,36 = 0,52
        # Mas mock retorna o de cadastro pra ambas, ok pra teste de custo
        self.assertAlmostEqual(c.custo_acumulado_brl, 0.52, places=2)
        self.assertEqual(c.chamadas_feitas["cadastro"], 1)
        self.assertEqual(c.chamadas_feitas["pis"], 1)


# ═══════════════════════════════════════════════════════════════════
# REGRA DE OURO — não sobrescrever campo já preenchido
# ═══════════════════════════════════════════════════════════════════

class TestNaoSobrescreveCampoPreenchido(unittest.TestCase):
    """O orquestrador NÃO deve sobrescrever campos que já vêm do Claude."""

    @mock.patch("directdata_client.httpx.Client")
    def test_payload_completo_pula_chamadas(self, mock_client):
        """Se payload já tem TUDO, não chama API (skip por economia)."""
        from enrichment import _enrich_from_directdata
        import enrichment

        # Força um cliente novo pra teste (resetando o singleton)
        from directdata_client import DirectDataClient
        enrichment._DD_CLIENT = DirectDataClient(token="TESTE")

        payload_completo = {
            "data": {
                "type": "candidatos",
                "attributes": {
                    "cpf": 5075487106,
                    "nome": "JA TINHA NOME",
                    "nomedamae": "JA TINHA MAE",
                    "nascimento": "2002-03-13",
                    "cep": "74000-000", "rua": "JA TINHA", "bairro": "JA TINHA",
                    "cidade": "GOIANIA", "pis": "12345678901",
                    "tituloeleitor": "140569130582",
                },
                "relationships": {
                    "sexo": {"data": {"type": "tipos-sexo", "id": "1"}},
                    "estado": {"data": {"type": "estados", "id": "9"}},
                },
            }
        }
        get_mock = mock_client.return_value.__enter__.return_value.get

        novos = _enrich_from_directdata(payload_completo, "05075487106")
        # Nenhuma API chamada porque tudo já estava preenchido
        get_mock.assert_not_called()
        self.assertEqual(novos, [])

        # Limpa singleton pra outros testes
        enrichment._DD_CLIENT = None


if __name__ == "__main__":
    unittest.main(verbosity=2)
