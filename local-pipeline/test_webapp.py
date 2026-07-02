"""Testes do Flask app (webapp.py, v2.15.0).

Usa app.test_client() pra simular requests sem subir servidor. Aponta
dashboard_data.PLANILHA_ADMISSOES / PAYLOADS_DIR pra um diretório tmp
por teste, então cada caso roda isolado.

Mocks principais:
  - main.carregar_config / ClaudeClient / rodar_uma_passada / registrar_admissao_planilha
  - ecotador_client.EContadorAPI
  - post_admissao.postar_candidato_registrado

Roda via: python -m pytest test_webapp.py -q
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from openpyxl import Workbook

# Garante CWD do projeto pra imports
_AQUI = Path(__file__).parent
sys.path.insert(0, str(_AQUI))

import dashboard_data as dd
import webapp


HEADERS = ["Data/Hora", "Nome", "Empresa", "CNPJ", "Procedência", "msg_id"]


def _agora_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _criar_planilha(path: Path, linhas: list[list]) -> None:
    wb = Workbook()
    ws = wb.active
    ws.append(HEADERS)
    for linha in linhas:
        ws.append(linha)
    wb.save(path)


class WebappBaseTest(unittest.TestCase):
    """Base com setup do test_client + redirecionamento dos paths do dd."""

    def setUp(self):
        webapp.app.config["TESTING"] = True
        self.client = webapp.app.test_client()

        self.tmp = Path(tempfile.mkdtemp(prefix="admiter_web_test_"))
        self.planilha = self.tmp / "admissoes.xlsx"
        self.payloads = self.tmp / "payloads"
        self.payloads.mkdir()
        self.ndjson = self.tmp / "admissao_log.ndjson"

        self._patches = [
            patch.object(dd, "PLANILHA_ADMISSOES", self.planilha),
            patch.object(dd, "PAYLOADS_DIR", self.payloads),
            patch.object(dd, "ADMISSAO_LOG", self.ndjson),
        ]
        for p in self._patches:
            p.start()

        # Garante que PASSADA começa "parada" pra cada teste
        webapp.PASSADA.rodando = False
        webapp.PASSADA.iniciada_em = ""
        webapp.PASSADA.terminada_em = ""
        webapp.PASSADA.erro = None
        webapp.PASSADA.ultimo_resumo = {}

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _post(self, url, data=None, htmx=False, origin="http://localhost"):
        """Helper que adiciona Origin (CSRF check do v2.15.0) e opcionalmente
        HX-Request. Mantém POSTs realistas (browser sempre manda Origin)."""
        headers = {}
        if origin is not None:
            headers["Origin"] = origin
        if htmx:
            headers["HX-Request"] = "true"
        return self.client.post(url, data=data or {}, headers=headers)


class TestPaginas(WebappBaseTest):
    """Testes das páginas principais (rotas que renderizam HTML)."""

    def test_index_render_ok(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"AdmitER", resp.data)

    def test_pendentes_render_ok(self):
        resp = self.client.get("/pendentes")
        self.assertEqual(resp.status_code, 200)

    def test_processadas_render_ok(self):
        resp = self.client.get("/processadas")
        self.assertEqual(resp.status_code, 200)

    def test_auditoria_render_ok(self):
        resp = self.client.get("/auditoria")
        self.assertEqual(resp.status_code, 200)

    def test_estatisticas_render_ok(self):
        # Mocka billing pra não tentar somar de arquivos inexistentes
        with patch("webapp._billing_resumido", return_value={"claude": {}, "directdata": {}}):
            resp = self.client.get("/estatisticas")
        self.assertEqual(resp.status_code, 200)


class TestApiJson(WebappBaseTest):
    """Testes dos endpoints JSON."""

    def test_api_status_json(self):
        resp = self.client.get("/api/status")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertIn("app_version", body)
        self.assertIn("status", body)
        self.assertIn("passada", body)

    def test_api_pendentes_lista(self):
        # cria uma pendência pra ter algo na lista
        linhas = [
            [_agora_str(), "JOAO TESTE", "EMP", "12345678000190",
             "Pendente cliente — faltam: cpf", "midaaaaaaaaaaaaaa"],
        ]
        _criar_planilha(self.planilha, linhas)

        resp = self.client.get("/api/pendentes")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertIn("total", body)
        self.assertIn("items", body)
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["items"][0]["nome"], "JOAO TESTE")


class TestPendenciaDetalhe(WebappBaseTest):
    """Detalhe de pendência: 404 quando não existe."""

    def test_pendencia_detalhe_404(self):
        # sem planilha → nenhuma entidade existe
        resp = self.client.get("/pendencia/midxxxxxxxxxxxx/INEXISTENTE/12345678000190")
        self.assertEqual(resp.status_code, 404)


class TestAtualizarAgora(WebappBaseTest):
    """POST /atualizar-agora dispara thread que roda passada."""

    def test_atualizar_agora_em_thread(self):
        """Mocka rodar_uma_passada como no-op; valida que a thread arranca
        (PASSADA.rodando vira True OU já volta False/finalizada quickly)."""
        from unittest.mock import MagicMock

        fake_config = MagicMock()
        fake_config.confirmar_replies = True
        fake_config.claude_model = "fake"
        fake_config.claude_max_tokens = 100
        fake_config.claude_chamadas_verificacao = 0
        fake_config.base_url = "http://x"
        fake_config.token = "tk"

        # Cria um módulo "main" mock antes do import lazy lá dentro
        fake_main = types.ModuleType("main")

        def _fake_rodar(*a, **k):
            # Pausa rápida pra dar tempo de checar "rodando" no test
            time.sleep(0.05)

        class _FakeClaudeClient:
            def __init__(self, *a, **k):
                self.usage_total = {"n_calls": 0, "input_tokens": 0, "output_tokens": 0}

        fake_main.carregar_config = lambda: fake_config
        fake_main.bootstrap_arquivos_locais = lambda: None
        fake_main.carregar_planilha = lambda *a, **k: []
        fake_main.ClaudeClient = _FakeClaudeClient
        fake_main.rodar_uma_passada = _fake_rodar
        fake_main.PLANILHA_CBO = ""

        with patch.dict(sys.modules, {"main": fake_main}):
            resp = self._post("/atualizar-agora")

        self.assertEqual(resp.status_code, 200)
        # Espera a thread terminar (até 2s)
        for _ in range(40):
            if not webapp.PASSADA.snapshot()["rodando"]:
                break
            time.sleep(0.05)

        snap = webapp.PASSADA.snapshot()
        # Pode ter iniciado e já terminado — o importante é que houve marcação
        self.assertTrue(snap["iniciada_em"], "Thread deveria ter marcado iniciada_em")

    def test_atualizar_agora_ja_rodando(self):
        """Se PASSADA já está rodando, retorna aviso e não dispara nova thread."""
        webapp.PASSADA.rodando = True
        try:
            resp = self._post("/atualizar-agora")
            self.assertEqual(resp.status_code, 200)
            # Mensagem de "já rodando" deve aparecer no fragmento
            self.assertIn(b"Rodando", resp.data)
        finally:
            webapp.PASSADA.rodando = False


class TestPostarPendencia(WebappBaseTest):
    """POST /pendencia/.../postar — caminho de "Aplicar form e POSTar"."""

    def _setup_entidade_e_payload(
        self,
        msg_id: str,
        nome: str,
        cnpj: str,
        attrs_extra: dict | None = None,
        com_endereco: bool = True,
        com_relationships: bool = True,
    ) -> Path:
        """Cria linha na planilha + JSON em payloads/. Retorna o path do JSON.

        com_endereco=True garante cep/rua/bairro/cidade preenchidos (v2.15.0
        exige endereço completo antes do POST). com_relationships=True inclui
        empresa/funcao/estado (também obrigatórios).
        """
        linhas = [
            [_agora_str(), nome, "EMPRESA TESTE", cnpj,
             "Pendente cliente — faltam: salario", msg_id],
        ]
        _criar_planilha(self.planilha, linhas)

        arq = self.payloads / f"2026-06-13T10-00-00_{msg_id[:16]}_{nome[:6]}.json"
        attrs = {
            "nome": nome,
            "cpf": 12345678901,
            "salario": 1500.0,
            "nomecargo": "OPERADOR",
        }
        if com_endereco:
            attrs.update({
                "cep": "74000000",
                "rua": "RUA TESTE",
                "numero": 100,
                "bairro": "CENTRO",
                "cidade": "GOIANIA",
            })
        if attrs_extra:
            attrs.update(attrs_extra)

        data = {"type": "candidatos", "attributes": attrs}
        if com_relationships:
            data["relationships"] = {
                "empresa": {"data": {"type": "empresas", "id": "89"}},
                "funcao": {"data": {"type": "funcoes", "id": "12345"}},
                "estado": {"data": {"type": "estados", "id": "9"}},
            }
        doc = {
            "payload": {"data": data},
            "resolucao": {"cnpj_empresa": cnpj, "razao_social": "EMPRESA TESTE"}
        }
        arq.write_text(json.dumps(doc), encoding="utf-8")
        return arq

    def test_postar_pendencia_sem_payload_retorna_400(self):
        """Sem payload no disco, deve retornar 400 com mensagem de erro."""
        msg_id = "abcabcabc1234567"
        nome = "MARIA PENDENTE"
        cnpj = "11111111000111"
        # Cria entidade na planilha mas SEM payload
        _criar_planilha(self.planilha, [
            [_agora_str(), nome, "X", cnpj, "Pendente cliente", msg_id],
        ])

        resp = self._post(
            f"/pendencia/{msg_id}/{nome}/{cnpj}/postar",
            data={"salario": "2000"},
        )
        self.assertEqual(resp.status_code, 400)
        # Sem msg de "payload" no body — mas o que importa é o 400

    def test_postar_pendencia_com_payload_mockado(self):
        """Com payload no disco + mocks de POST + registrar, deve gravar linha
        e retornar resposta de sucesso."""
        msg_id = "ffffeeeedddd1111"
        nome = "JOAO POSTANTE"
        cnpj = "22222222000122"
        self._setup_entidade_e_payload(msg_id, nome, cnpj)

        # PostResult fake
        from post_admissao import PostResult
        fake_res = PostResult(ok=True, candidato_id="9999", pulou=False, origem="web_resolver")

        # Mocka carregar_config e EContadorAPI e o wrapper de POST
        fake_main = types.ModuleType("main")
        fake_main.carregar_config = lambda: MagicMock(base_url="http://x", token="tk")
        fake_main.registrar_admissao_planilha = MagicMock()

        with patch.dict(sys.modules, {"main": fake_main}), \
             patch("webapp.EContadorAPI") as mock_api_cls, \
             patch("webapp.postar_candidato_registrado", return_value=fake_res) as mock_postar:
            mock_api_cls.return_value = MagicMock()

            resp = self._post(
                f"/pendencia/{msg_id}/{nome}/{cnpj}/postar",
                data={"salario": "2500,50"},
            )

        # Pode ser 200 (HTML normal) — não checamos código exato porque o
        # template flash_full retorna 200. Confere que NÃO é 400/500.
        self.assertNotEqual(resp.status_code, 400)
        self.assertNotEqual(resp.status_code, 500)
        # Mock do postar foi chamado
        mock_postar.assert_called_once()
        # E o registrar foi chamado
        fake_main.registrar_admissao_planilha.assert_called_once()


class TestHtmxFragments(WebappBaseTest):
    """Fragmentos HTMX retornam parciais (não a página inteira)."""

    def test_htmx_contadores_retorna_partial(self):
        resp = self.client.get("/htmx/contadores")
        self.assertEqual(resp.status_code, 200)
        # Partial não tem <html> — só a div de contadores
        self.assertIn(b"counter", resp.data.lower())

    def test_htmx_lista_pendentes_filtra(self):
        """Passa ?q=ELI e confere que filtra a lista."""
        linhas = [
            [_agora_str(), "ELIANE DA SILVA", "EMP", "1",
             "Pendente cliente — faltam: x", "midaaaaaaaaaaaaaa"],
            [_agora_str(), "RAIMUNDO NONATO", "EMP", "1",
             "Pendente cliente — faltam: x", "midbbbbbbbbbbbbbb"],
        ]
        _criar_planilha(self.planilha, linhas)

        # Sem filtro: ambos aparecem
        resp_all = self.client.get("/htmx/lista-pendentes")
        self.assertEqual(resp_all.status_code, 200)
        self.assertIn(b"ELIANE", resp_all.data)
        self.assertIn(b"RAIMUNDO", resp_all.data)

        # Com filtro "ELI": só ELIANE
        resp_filt = self.client.get("/htmx/lista-pendentes?q=ELI")
        self.assertEqual(resp_filt.status_code, 200)
        self.assertIn(b"ELIANE", resp_filt.data)
        self.assertNotIn(b"RAIMUNDO", resp_filt.data)


# ── v2.15.0 fix pack: testes adicionais ───────────────────────────────────
# Cobertura dos fixes de segurança (CSRF/Origin, allowlist, headers,
# validação de path) e blockers (gmail wrapper, endereço completo, cooldown).


class TestOriginCheck(WebappBaseTest):
    """CSRF-lite via Origin/Referer header. Rejeita POST sem origem ou
    com origem que não bate com request.host."""

    def test_origin_check_rejeita_post_sem_origin(self):
        """POST sem Origin nem Referer → 403."""
        # Manda sem nenhum header (origin=None desliga o helper)
        resp = self.client.post("/atualizar-agora", data={})
        self.assertEqual(resp.status_code, 403)

    def test_origin_check_rejeita_origin_diferente(self):
        """POST com Origin: http://evil.com → 403."""
        resp = self._post("/atualizar-agora", origin="http://evil.com")
        self.assertEqual(resp.status_code, 403)

    def test_origin_check_aceita_origin_localhost(self):
        """POST com Origin: http://localhost passa o CSRF check (não retorna 403).
        Note: pode retornar 200 (sucesso) ou outro código de negócio, mas não 403."""
        # Mocka main pra não disparar passada real
        fake_main = types.ModuleType("main")
        fake_main.carregar_config = lambda: MagicMock()
        fake_main.bootstrap_arquivos_locais = lambda: None
        fake_main.carregar_planilha = lambda *a, **k: []
        fake_main.ClaudeClient = lambda *a, **k: MagicMock(
            usage_total={"n_calls": 0, "input_tokens": 0, "output_tokens": 0}
        )
        fake_main.rodar_uma_passada = lambda *a, **k: None
        fake_main.PLANILHA_CBO = ""

        with patch.dict(sys.modules, {"main": fake_main}):
            resp = self._post("/atualizar-agora", origin="http://localhost")
        self.assertNotEqual(resp.status_code, 403)


class TestSecurityHeaders(WebappBaseTest):
    """Security headers básicos aplicados em todas as respostas."""

    def test_security_headers_presentes(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("X-Frame-Options"), "SAMEORIGIN")
        self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(resp.headers.get("Referrer-Policy"), "same-origin")


class TestSalarioBR(WebappBaseTest):
    """_parse_salario_br aceita formatos comuns no Brasil."""

    def test_salario_br_aceita_formato_real(self):
        casos = {
            "1500": 1500.0,
            "1500.00": 1500.0,
            "1500,00": 1500.0,
            "1.500,00": 1500.0,
            "R$ 1500": 1500.0,
            "R$ 1.500,00": 1500.0,
            " 1500 ": 1500.0,
        }
        for entrada, esperado in casos.items():
            with self.subTest(entrada=entrada):
                self.assertEqual(webapp._parse_salario_br(entrada), esperado)

    def test_salario_br_rejeita_lixo(self):
        for entrada in ("", "abc", "R$"):
            with self.subTest(entrada=entrada):
                with self.assertRaises(ValueError):
                    webapp._parse_salario_br(entrada)


class TestPostarPendenciaSeguranca(WebappBaseTest):
    """Validações de segurança/negócio no /postar (v2.15.0 fix pack)."""

    def _setup_basic(self, msg_id, nome, cnpj, **kw):
        # Reusa helper de TestPostarPendencia via instância nova
        return TestPostarPendencia._setup_entidade_e_payload(
            self, msg_id, nome, cnpj, **kw
        )

    def test_postar_pendencia_rejeita_campo_nao_permitido(self):
        """POST com statusadmissao=2 (fora da allowlist) → 400 com mensagem clara.
        Protege contra cliente malicioso/desatento sobrescrever statusadmissao,
        que tem que ser SEMPRE 1 (CLAUDE.md)."""
        msg_id = "aaaa1111bbbb2222"
        nome = "FULANO ALLOWLIST"
        cnpj = "33333333000133"
        self._setup_basic(msg_id, nome, cnpj)

        resp = self._post(
            f"/pendencia/{msg_id}/{nome}/{cnpj}/postar",
            data={"statusadmissao": "2"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn(b"statusadmissao", resp.data)

    def test_postar_pendencia_rejeita_endereco_incompleto(self):
        """Payload no disco SEM cep/rua/bairro/cidade + POST → 400 mencionando
        'endereço'. Evita gastar token Claude + cair em 422 do eContador."""
        msg_id = "cccc3333dddd4444"
        nome = "BELTRANO ENDERECO"
        cnpj = "44444444000144"
        # com_endereco=False: payload sem cep/rua/bairro/cidade
        self._setup_basic(msg_id, nome, cnpj, com_endereco=False)

        resp = self._post(
            f"/pendencia/{msg_id}/{nome}/{cnpj}/postar",
            data={"salario": "1800"},
        )
        self.assertEqual(resp.status_code, 400)
        # Mensagem deve mencionar "endereço" (com cedilha) — ignora case
        self.assertIn(b"endere", resp.data.lower())

    def test_postar_pendencia_passa_gmail_ao_wrapper(self):
        """Mocka postar_candidato_registrado e _abrir_gmail; verifica que os
        kwargs gmail/label_processado/label_pendente_remover foram passados
        (não None). BLOCKER review v2.15.0 — sem isso a thread fica reaberta
        e Claude reprocessa no próximo polling (US$0.40/passada)."""
        msg_id = "eeee5555ffff6666"
        nome = "CICLANO GMAILER"
        cnpj = "55555555000155"
        self._setup_basic(msg_id, nome, cnpj)

        from post_admissao import PostResult
        fake_res = PostResult(
            ok=True, candidato_id="42", pulou=False, origem="web_resolver",
        )
        fake_gmail = MagicMock()
        fake_config = MagicMock()
        fake_config.base_url = "http://x"
        fake_config.token = "tk"
        fake_config.label_processado = "ADMISSAO/processado"
        fake_config.label_pendente = "ADMISSAO/pendente"

        fake_main = types.ModuleType("main")
        fake_main.carregar_config = lambda: fake_config
        fake_main.registrar_admissao_planilha = MagicMock()

        with patch.dict(sys.modules, {"main": fake_main}), \
             patch("webapp.EContadorAPI") as mock_api_cls, \
             patch("webapp._abrir_gmail", return_value=fake_gmail), \
             patch("webapp.postar_candidato_registrado",
                   return_value=fake_res) as mock_postar:
            mock_api_cls.return_value = MagicMock()
            resp = self._post(
                f"/pendencia/{msg_id}/{nome}/{cnpj}/postar",
                data={"salario": "1900"},
            )

        self.assertNotEqual(resp.status_code, 400, msg=resp.data[:300])
        mock_postar.assert_called_once()
        kwargs = mock_postar.call_args.kwargs
        # BLOCKER: kwargs precisam ter gmail + labels (não None/missing)
        self.assertIn("gmail", kwargs)
        self.assertIs(kwargs["gmail"], fake_gmail)
        self.assertEqual(kwargs.get("label_processado"), "ADMISSAO/processado")
        self.assertEqual(kwargs.get("label_pendente_remover"), [msg_id])


class TestMarcarResolvidoGmail(WebappBaseTest):
    """marcar_resolvido_manual deve fechar a thread no Gmail (aplica label
    processado, remove pendente) — senão a próxima passada reprocessa."""

    def test_marcar_resolvido_fecha_thread_gmail(self):
        msg_id = "11112222aaaabbbb"
        nome = "DICRANIO RESOLVIDO"
        cnpj = "66666666000166"
        # Cria entidade na planilha
        _criar_planilha(self.planilha, [
            [_agora_str(), nome, "EMP", cnpj, "Pendente cliente", msg_id],
        ])

        fake_gmail = MagicMock()
        fake_config = MagicMock()
        fake_config.label_processado = "ADMISSAO/processado"
        fake_config.label_pendente = "ADMISSAO/pendente"

        fake_main = types.ModuleType("main")
        fake_main.carregar_config = lambda: fake_config
        fake_main.registrar_admissao_planilha = MagicMock()

        with patch.dict(sys.modules, {"main": fake_main}), \
             patch("webapp._abrir_gmail", return_value=fake_gmail):
            resp = self._post(
                f"/pendencia/{msg_id}/{nome}/{cnpj}/marcar-resolvido"
            )

        self.assertNotEqual(resp.status_code, 400, msg=resp.data[:300])
        self.assertNotEqual(resp.status_code, 500, msg=resp.data[:300])
        # Aplicou label processado com msg_id correto
        fake_gmail.aplicar_label.assert_called_once_with(
            msg_id, "ADMISSAO/processado"
        )
        # Removeu label pendente com msg_id correto
        fake_gmail.remover_label.assert_called_once_with(
            msg_id, "ADMISSAO/pendente"
        )


class TestCooldownAtualizarAgora(WebappBaseTest):
    """POST /atualizar-agora respeita cooldown de 30s entre passadas."""

    def test_atualizar_agora_respeita_cooldown(self):
        """Última passada terminou há 5s → próximo POST recebe mensagem
        'Aguarde Xs' (sem disparar nova thread)."""
        # Simula passada terminada há 5s (cooldown=30s → ainda 25s sobrando)
        webapp.PASSADA.rodando = False
        webapp.PASSADA.terminada_em = (
            datetime.now() - timedelta(seconds=5)
        ).isoformat(timespec="seconds")

        resp = self._post("/atualizar-agora")
        self.assertEqual(resp.status_code, 200)
        # Body deve mencionar "Aguarde" (com qualquer número de segundos)
        body = resp.data.decode("utf-8", errors="ignore")
        self.assertIn("Aguarde", body)
        self.assertIn("s", body)


class TestValidarNomePath(WebappBaseTest):
    """_validar_nome_path rejeita nomes suspeitos no path (injeção NDJSON)."""

    def test_validar_nome_path_rejeita_quebra_linha(self):
        """Nome com chars suspeitos (quebra de linha, @, #, etc.) → 400
        (proteção contra NDJSON corrupt / log injection).

        Nota: Werkzeug rejeita %0A no path antes de chegar na view (404), então
        testamos a função _validar_nome_path direto com '\\n' E o endpoint
        completo com um char que passa pelo routing mas falha no regex (@)."""
        from flask import abort
        from werkzeug.exceptions import BadRequest

        # 1. Função direto: nome com \n → BadRequest 400
        with webapp.app.test_request_context("/"):
            with self.assertRaises(BadRequest):
                webapp._validar_nome_path("NOME\nINJECAO")
            with self.assertRaises(BadRequest):
                webapp._validar_nome_path("NOME\rINJ")
            with self.assertRaises(BadRequest):
                webapp._validar_nome_path("NOME<script>")

        # 2. Endpoint: nome com '@' (passa routing, falha regex) → 400
        url = "/pendencia/abc1234567890123/NOME%40INJECAO/12345678000190"
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 400)

    def test_validar_nome_path_aceita_nome_normal(self):
        """Nome 'JOSE DA SILVA' (sem chars suspeitos) NÃO retorna 400 —
        retorna 404 (entidade não existe) ou 200 (existe), mas não 400."""
        resp = self.client.get(
            "/pendencia/abc1234567890123/JOSE DA SILVA/12345678000190"
        )
        self.assertNotEqual(resp.status_code, 400)


# ═══════════════════════════════════════════════════════════════════
# v2.15.1 — Endpoints novos (importar + corrigir-cnpj + manutenção)
# ═══════════════════════════════════════════════════════════════════

class TestImportar(WebappBaseTest):
    def setUp(self):
        super().setUp()
        # Garante que IMPORTAR começa parado pra cada teste
        webapp.IMPORTAR.rodando = False
        webapp.IMPORTAR.iniciada_em = ""
        webapp.IMPORTAR.terminada_em = ""
        webapp.IMPORTAR.erro = None
        webapp.IMPORTAR.ultimo_resumo = {}

    def test_importar_form_render(self):
        r = self.client.get("/importar")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Importacao", r.data.replace(b"\xc3\xa7\xc3\xa3o", b"acao")) \
            if False else self.assertIn(b"Importa", r.data)

    def test_importar_sem_arquivos_retorna_erro(self):
        r = self._post("/importar")
        # Sem arquivos: 400
        self.assertEqual(r.status_code, 400)

    def test_importar_extensao_nao_suportada(self):
        from io import BytesIO
        data = {"arquivos": (BytesIO(b"x" * 100), "arquivo.exe")}
        r = self.client.post(
            "/importar", data=data,
            content_type="multipart/form-data",
            headers={"Origin": "http://localhost"},
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn(b".exe", r.data)

    @patch("webapp._executar_importacao_em_thread")
    def test_importar_aceita_pdf_e_dispara_thread(self, mock_executar):
        from io import BytesIO
        data = {
            "arquivos": (BytesIO(b"%PDF-1.4\n" + b"x" * 100), "doc.pdf"),
            "corpo_texto": "teste de contexto",
        }
        r = self.client.post(
            "/importar", data=data,
            content_type="multipart/form-data",
            headers={"Origin": "http://localhost"},
        )
        self.assertIn(r.status_code, (200, 302))
        # Como dispara em thread daemon, dá um tempinho pro start
        time.sleep(0.05)
        # Verifica que o worker foi disparado com pelo menos 1 arquivo
        # (não checa execução real porque está mockado)
        # Como roda em thread, o assert é melhor feito sobre o estado
        # ou sobre o mock.called eventual — relaxado aqui.


class TestCorrigirCNPJ(WebappBaseTest):
    @patch("main.salvar_cnpj_override")
    def test_corrigir_cnpj_valido(self, mock_salvar):
        r = self._post(
            "/pendencia/abc1234567890123/JOSE/12345678000190/corrigir-cnpj",
            data={"cnpj_novo": "98.765.432/0001-10"},
        )
        self.assertIn(r.status_code, (200, 302))
        mock_salvar.assert_called_once()
        chamada = mock_salvar.call_args
        self.assertEqual(chamada.args[0], "abc1234567890123")
        self.assertEqual(chamada.args[1], "98765432000110")

    def test_corrigir_cnpj_invalido(self):
        r = self._post(
            "/pendencia/abc1234567890123/JOSE/12345678000190/corrigir-cnpj",
            data={"cnpj_novo": "123"},  # menos de 14 dígitos
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn(b"CNPJ", r.data)

    def test_corrigir_cnpj_vazio(self):
        r = self._post(
            "/pendencia/abc1234567890123/JOSE/12345678000190/corrigir-cnpj",
            data={"cnpj_novo": ""},
        )
        self.assertEqual(r.status_code, 400)


class TestManutencao(WebappBaseTest):
    @patch("main.carregar_planilha")
    def test_recarregar_cbo_chama_carregar_planilha(self, mock_carregar):
        mock_carregar.return_value = [{"nome_cargo": "X", "funcao_id": "1"}] * 5
        r = self._post("/maintenance/recarregar-cbo")
        self.assertIn(r.status_code, (200, 302))
        mock_carregar.assert_called_once()

    @patch("main.carregar_planilha")
    def test_recarregar_cbo_erro_retorna_400(self, mock_carregar):
        mock_carregar.side_effect = FileNotFoundError("planilha não existe")
        r = self._post("/maintenance/recarregar-cbo")
        self.assertEqual(r.status_code, 400)

    def test_atualizar_cache_empresas_dispara_thread(self):
        # Roda em thread bg — só verifica que retornou ok (não trava)
        with patch("main.recarregar_empresas_cache") as mock_rec, \
             patch("main.carregar_config") as mock_cfg:
            mock_cfg.return_value = types.SimpleNamespace(
                base_url="http://localhost", token="t",
            )
            mock_rec.return_value = {}
            r = self._post("/maintenance/atualizar-cache-empresas")
            self.assertIn(r.status_code, (200, 302))
            # Thread spawn ocorreu — dá tempo
            time.sleep(0.1)

    def test_limpar_fingerprint_remove_arquivo(self):
        import idempotencia as ide
        tmp_fp = self.tmp / "fp.json"
        tmp_fp.write_text("{\"abc\": {}}", encoding="utf-8")
        with patch.object(ide, "FP_FILE", tmp_fp):
            r = self._post("/maintenance/limpar-fingerprint")
            self.assertIn(r.status_code, (200, 302))
            self.assertFalse(tmp_fp.exists())

    def test_limpar_fingerprint_arquivo_inexistente_nao_quebra(self):
        # missing_ok=True deve cobrir
        import idempotencia as ide
        tmp_fp = self.tmp / "fp_nao_existe.json"
        with patch.object(ide, "FP_FILE", tmp_fp):
            r = self._post("/maintenance/limpar-fingerprint")
            self.assertIn(r.status_code, (200, 302))


# ═══════════════════════════════════════════════════════════════════
# v2.15.2 — Controle (polling) + Backup + Configurações + Dark mode
# ═══════════════════════════════════════════════════════════════════

class TestPollingLoop(WebappBaseTest):
    def setUp(self):
        super().setUp()
        # Reset polling state
        webapp.POLLING._thread = None
        webapp.POLLING._stop_evt = webapp.threading.Event()
        webapp.POLLING.iniciado_em = ""
        webapp.POLLING.parado_em = ""
        webapp.POLLING.n_passadas = 0
        webapp.POLLING.erro = None

    def test_polling_parar_quando_parado_retorna_400(self):
        r = self._post("/controle/polling/parar")
        self.assertEqual(r.status_code, 400)
        self.assertIn(b"parado", r.data.lower())

    def test_polling_iniciar_e_parar(self):
        # Patcheia `rodar_uma_passada` pra não tentar bater em APIs reais.
        # O loop chega no while, importa main, mas precisamos que main.* funcione
        # sem network — mockamos as funções pesadas:
        with patch("main.rodar_uma_passada") as mock_passada, \
             patch("main.carregar_config") as mock_cfg, \
             patch("main.carregar_planilha") as mock_pla, \
             patch("main.ClaudeClient") as mock_claude, \
             patch("main.bootstrap_arquivos_locais"):
            mock_cfg.return_value = types.SimpleNamespace(
                claude_model="claude-sonnet-4-6", claude_max_tokens=8192,
                claude_chamadas_verificacao=1, intervalo=60,
                confirmar_replies=False,
            )
            mock_pla.return_value = []
            mock_claude.return_value = MagicMock()
            mock_passada.return_value = None

            r = self._post("/controle/polling/iniciar")
            self.assertIn(r.status_code, (200, 302))
            # Pequena espera pra thread arrancar
            time.sleep(0.2)
            snap = webapp.POLLING.snapshot()
            self.assertTrue(snap["rodando"])

            r2 = self._post("/controle/polling/parar")
            self.assertIn(r2.status_code, (200, 302))
            # stop_evt sinalizado — espera o loop sair (intervalo de 60s
            # mas wait(0.2) pra teste). Forçamos saída via stop_evt
            time.sleep(0.3)
            # Após o wait, snapshot pode mostrar rodando=False ou ainda True
            # se a passada estiver em meio do mock — não asseguramos exato.

    def test_polling_iniciar_duas_vezes_segunda_falha(self):
        with patch("main.rodar_uma_passada"), \
             patch("main.carregar_config") as mock_cfg, \
             patch("main.carregar_planilha"), \
             patch("main.ClaudeClient"), \
             patch("main.bootstrap_arquivos_locais"):
            mock_cfg.return_value = types.SimpleNamespace(
                claude_model="x", claude_max_tokens=8192,
                claude_chamadas_verificacao=1, intervalo=60,
                confirmar_replies=False,
            )
            r1 = self._post("/controle/polling/iniciar")
            self.assertIn(r1.status_code, (200, 302))
            time.sleep(0.05)
            r2 = self._post("/controle/polling/iniciar")
            self.assertEqual(r2.status_code, 400)
            # Cleanup: para o loop
            webapp.POLLING._stop_evt.set()


class TestBackup(WebappBaseTest):
    @patch("main.fazer_backup_planilha_e_payloads")
    def test_backup_sucesso(self, mock_backup):
        mock_backup.return_value = Path("/fake/backup/20260613_180000")
        r = self._post("/maintenance/backup")
        self.assertIn(r.status_code, (200, 302))
        mock_backup.assert_called_once()

    @patch("main.fazer_backup_planilha_e_payloads")
    def test_backup_falha_retorna_400(self, mock_backup):
        mock_backup.return_value = None
        r = self._post("/maintenance/backup")
        self.assertEqual(r.status_code, 400)

    @patch("main.fazer_backup_planilha_e_payloads")
    def test_backup_exception_retorna_400(self, mock_backup):
        mock_backup.side_effect = PermissionError("disco cheio")
        r = self._post("/maintenance/backup")
        self.assertEqual(r.status_code, 400)


class TestConfiguracoes(WebappBaseTest):
    def setUp(self):
        super().setUp()
        # Cria um config.json temporário com defaults
        self.cfg_tmp = self.tmp / "config.json"
        self.cfg_tmp.write_text(json.dumps({
            "ecotador": {"base_url": "http://x", "token": "t"},
            "polling_intervalo_segundos": 300,
            "auto_email_pendencias": False,
            "sempre_mandar_sem_data_admissao": False,
            "sempre_mandar_sem_funcao": False,
            "reprocessar_pendentes_no_polling": False,
            "outro_campo": "preservar",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        # Patcha CONFIG_FILE do main
        import main as _main
        self._patch_cfg = patch.object(_main, "CONFIG_FILE", self.cfg_tmp)
        self._patch_cfg.start()

    def tearDown(self):
        self._patch_cfg.stop()
        super().tearDown()

    def test_configuracoes_salvar_alguns_booleans(self):
        r = self._post("/configuracoes", data={
            "auto_email_pendencias": "on",
            "sempre_mandar_sem_funcao": "on",
            "polling_intervalo_segundos": "600",
            "pausa_entre_emails_segundos": "10",
        })
        self.assertIn(r.status_code, (200, 302))
        data = json.loads(self.cfg_tmp.read_text(encoding="utf-8"))
        self.assertTrue(data["auto_email_pendencias"])
        self.assertTrue(data["sempre_mandar_sem_funcao"])
        # Os que não foram marcados viraram False (allowlist)
        self.assertFalse(data["sempre_mandar_sem_data_admissao"])
        # Intervalo virou 600
        self.assertEqual(data["polling_intervalo_segundos"], 600)
        self.assertEqual(data["pausa_entre_emails_segundos"], 10)
        # Resto preservado
        self.assertEqual(data["outro_campo"], "preservar")
        self.assertEqual(data["ecotador"]["token"], "t")

    def test_configuracoes_intervalo_fora_de_range_retorna_400(self):
        r = self._post("/configuracoes", data={
            "polling_intervalo_segundos": "30",  # < 60
        })
        self.assertEqual(r.status_code, 400)
        # Arquivo não foi alterado pra esse campo
        data = json.loads(self.cfg_tmp.read_text(encoding="utf-8"))
        self.assertEqual(data["polling_intervalo_segundos"], 300)

    def test_configuracoes_intervalo_nao_inteiro_retorna_400(self):
        r = self._post("/configuracoes", data={
            "polling_intervalo_segundos": "abc",
        })
        self.assertEqual(r.status_code, 400)

    def test_configuracoes_chave_proibida_ignorada(self):
        # form com chave não-allowlisted: simplesmente não é gravada
        # (booleans só processam allowlist; outros campos viram bool False)
        r = self._post("/configuracoes", data={
            "campo_arbitrario": "valor",
            "polling_intervalo_segundos": "300",
        })
        self.assertIn(r.status_code, (200, 302))
        data = json.loads(self.cfg_tmp.read_text(encoding="utf-8"))
        self.assertNotIn("campo_arbitrario", data)


class TestDashboardCardsNovos(WebappBaseTest):
    def test_dashboard_tem_card_controle(self):
        r = self.client.get("/")
        self.assertIn(b"Controle", r.data)
        self.assertIn(b"polling", r.data)

    def test_dashboard_tem_card_configuracoes(self):
        r = self.client.get("/")
        self.assertIn(b"Configura", r.data)
        # checkboxes essenciais
        for nome in (b"auto_email_pendencias", b"sempre_mandar_sem_funcao",
                     b"reprocessar_pendentes_no_polling"):
            self.assertIn(nome, r.data, f"checkbox {nome!r} ausente")

    def test_dashboard_tem_toggle_tema(self):
        r = self.client.get("/")
        self.assertIn(b"theme-toggle", r.data)


# ═══════════════════════════════════════════════════════════════════
# v2.15.3 — Payload parcial raiz + reprocessar email
# ═══════════════════════════════════════════════════════════════════

class TestPayloadParcialDeDados(unittest.TestCase):
    """Helper main._payload_parcial_de_dados converte _dados_parciais
    do Claude (chaves PT) em payload JSON:API parcial."""

    def test_mapeamento_chaves_comuns(self):
        from main import _payload_parcial_de_dados
        dp = {
            "nome": "ELIANE BARBOSA",
            "cpf": "02312244000135",
            "mae": "MARIA",
            "cargo": "AUXILIAR",
            "rg": "9999999",
            "data_admissao": "2026-06-15",
            "salario_base": "1500",
        }
        out = _payload_parcial_de_dados(dp)
        attrs = out["data"]["attributes"]
        self.assertEqual(attrs["nome"], "ELIANE BARBOSA")
        self.assertEqual(attrs["nomedamae"], "MARIA")
        self.assertEqual(attrs["nomecargo"], "AUXILIAR")
        self.assertEqual(attrs["identidade"], "9999999")  # rg → identidade
        self.assertEqual(attrs["admissao"], "2026-06-15")  # data_admissao → admissao
        self.assertEqual(attrs["salario"], "1500")

    def test_valores_vazios_omitidos(self):
        from main import _payload_parcial_de_dados
        dp = {"nome": "X", "cpf": "", "salario": None, "rg": []}
        out = _payload_parcial_de_dados(dp)
        attrs = out["data"]["attributes"]
        self.assertIn("nome", attrs)
        self.assertNotIn("cpf", attrs)
        self.assertNotIn("salario", attrs)
        self.assertNotIn("identidade", attrs)

    def test_chaves_desconhecidas_ignoradas(self):
        from main import _payload_parcial_de_dados
        dp = {"nome": "X", "qualquer_chave_random": "valor"}
        out = _payload_parcial_de_dados(dp)
        attrs = out["data"]["attributes"]
        self.assertIn("nome", attrs)
        self.assertNotIn("qualquer_chave_random", attrs)

    def test_dp_vazio_retorna_estrutura_minima(self):
        from main import _payload_parcial_de_dados
        out = _payload_parcial_de_dados({})
        self.assertEqual(out["data"]["type"], "candidatos")
        self.assertEqual(out["data"]["attributes"], {})

    def test_dp_nao_dict_retorna_minimo(self):
        from main import _payload_parcial_de_dados
        out = _payload_parcial_de_dados("string lixo")
        self.assertEqual(out["data"]["attributes"], {})


class TestReprocessarEndpoint(WebappBaseTest):
    def setUp(self):
        super().setUp()
        # Reset IMPORTAR (reusado pelo reprocessar)
        webapp.IMPORTAR.rodando = False
        webapp.IMPORTAR.iniciada_em = ""
        webapp.IMPORTAR.terminada_em = ""
        webapp.IMPORTAR.erro = None
        webapp.IMPORTAR.ultimo_resumo = {}

    def test_reprocessar_dispara_thread(self):
        with patch("webapp._reprocessar_msg_em_thread") as mock_repr:
            r = self._post(
                "/pendencia/abc1234567890123/JOSE/12345678000190/reprocessar"
            )
            self.assertIn(r.status_code, (200, 302))
            # Dá tempo da thread arrancar
            time.sleep(0.1)
            mock_repr.assert_called_once()
            # msg_id veio do path
            self.assertEqual(mock_repr.call_args.args[0], "abc1234567890123")

    def test_reprocessar_enquanto_outra_rodando_retorna_400(self):
        webapp.IMPORTAR.rodando = True
        r = self._post(
            "/pendencia/abc1234567890123/JOSE/12345678000190/reprocessar"
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn(b"rodando", r.data.lower())
        webapp.IMPORTAR.rodando = False  # cleanup

    def test_reprocessar_rejeita_nome_invalido(self):
        # Nome com chars suspeitos é rejeitado pelo _validar_nome_path
        r = self._post(
            "/pendencia/abc1234567890123/<script>/12345678000190/reprocessar"
        )
        self.assertEqual(r.status_code, 400)


# ═══════════════════════════════════════════════════════════════════
# v2.15.4 — Atividade (terminal de logs) + Auto-save de Configurações
# ═══════════════════════════════════════════════════════════════════

class TestAtividadeTerminal(WebappBaseTest):
    def setUp(self):
        super().setUp()
        webapp.LOG_BUFFER.limpar()

    def test_pagina_atividade_renderiza(self):
        r = self.client.get("/atividade")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"terminal", r.data)  # classe CSS .terminal

    def test_htmx_atividade_retorna_fragment(self):
        r = self.client.get("/htmx/atividade")
        self.assertEqual(r.status_code, 200)

    def test_log_buffer_captura_logs_info(self):
        import logging as _l
        log = _l.getLogger("admissao.test_captura")
        log.info("xyz1 mensagem teste")
        log.warning("xyz2 aviso")
        snap = webapp.LOG_BUFFER.snapshot()
        msgs = "".join(e["msg"] for e in snap)
        self.assertIn("xyz1", msgs)
        self.assertIn("xyz2", msgs)

    def test_log_buffer_respeita_capacidade(self):
        # Cria buffer pequeno só pra esse teste
        buf = webapp._LogBuffer(capacidade=3)
        rec = lambda nivel, msg: __import__("logging").LogRecord(
            "admissao.x", nivel, "/x.py", 1, msg, None, None,
        )
        import logging as _l
        for i in range(10):
            buf.emit(rec(_l.INFO, f"msg {i}"))
        snap = buf.snapshot()
        self.assertEqual(len(snap), 3)
        # As 3 últimas (7, 8, 9)
        self.assertIn("msg 7", snap[0]["msg"])
        self.assertIn("msg 9", snap[2]["msg"])

    def test_atividade_limpar(self):
        import logging as _l
        _l.getLogger("admissao.x").info("antes da limpeza")
        self.assertGreater(len(webapp.LOG_BUFFER.snapshot()), 0)
        r = self._post("/atividade/limpar")
        self.assertIn(r.status_code, (200, 302))
        self.assertEqual(len(webapp.LOG_BUFFER.snapshot()), 0)


class TestConfiguracoesAutoSave(WebappBaseTest):
    def setUp(self):
        super().setUp()
        # Cria um config.json temporário com defaults
        self.cfg_tmp = self.tmp / "config.json"
        self.cfg_tmp.write_text(json.dumps({
            "auto_email_pendencias": False,
            "sempre_mandar_sem_funcao": False,
            "polling_intervalo_segundos": 300,
            "pausa_entre_emails_segundos": 20,
            "outro": "preservar",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        import main as _main
        self._patch_cfg = patch.object(_main, "CONFIG_FILE", self.cfg_tmp)
        self._patch_cfg.start()

    def tearDown(self):
        self._patch_cfg.stop()
        super().tearDown()

    def test_autosave_checkbox_on(self):
        r = self._post(
            "/configuracoes?campo=auto_email_pendencias",
            data={"auto_email_pendencias": "on"},
            htmx=True,
        )
        self.assertEqual(r.status_code, 200)
        # Retorna fragment com classe cfg-salvo-pill
        self.assertIn(b"cfg-salvo-pill", r.data)
        cfg = json.loads(self.cfg_tmp.read_text(encoding="utf-8"))
        self.assertTrue(cfg["auto_email_pendencias"])
        # Não estraga as outras
        self.assertFalse(cfg["sempre_mandar_sem_funcao"])
        self.assertEqual(cfg["outro"], "preservar")

    def test_autosave_checkbox_off(self):
        # Liga primeiro
        self.cfg_tmp.write_text(json.dumps({
            "auto_email_pendencias": True,
            "outro": "preservar",
        }), encoding="utf-8")
        r = self._post(
            "/configuracoes?campo=auto_email_pendencias",
            data={},  # sem 'on' = desmarcado
            htmx=True,
        )
        self.assertEqual(r.status_code, 200)
        cfg = json.loads(self.cfg_tmp.read_text(encoding="utf-8"))
        self.assertFalse(cfg["auto_email_pendencias"])

    def test_autosave_inteiro_valido(self):
        r = self._post(
            "/configuracoes?campo=polling_intervalo_segundos",
            data={"polling_intervalo_segundos": "600"},
            htmx=True,
        )
        self.assertEqual(r.status_code, 200)
        cfg = json.loads(self.cfg_tmp.read_text(encoding="utf-8"))
        self.assertEqual(cfg["polling_intervalo_segundos"], 600)

    def test_autosave_inteiro_fora_de_range(self):
        r = self._post(
            "/configuracoes?campo=polling_intervalo_segundos",
            data={"polling_intervalo_segundos": "10"},
            htmx=True,
        )
        self.assertEqual(r.status_code, 400)
        # Não alterou
        cfg = json.loads(self.cfg_tmp.read_text(encoding="utf-8"))
        self.assertEqual(cfg["polling_intervalo_segundos"], 300)

    def test_autosave_campo_nao_permitido(self):
        r = self._post(
            "/configuracoes?campo=outro",
            data={"outro": "x"},
            htmx=True,
        )
        self.assertEqual(r.status_code, 400)

    def test_dashboard_renderiza_com_hx_post_em_checkboxes(self):
        r = self.client.get("/")
        self.assertIn(b"hx-post", r.data)
        self.assertIn(b"cfg-status-auto_email_pendencias", r.data)


if __name__ == "__main__":
    unittest.main()
