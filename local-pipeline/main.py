"""Pipeline de admissão local — entry point.

Roda na máquina do escritório. Faz polling do Gmail a cada N segundos
(default 300 = 5 min), processa cada email pendente:

  1. Baixa corpo + anexos do email
  2. Manda tudo pro Claude (Vision) que retorna payload + cnpj/departamento
  3. Resolve empresa_id via GET /empresas
  4. Resolve departamento via 3 regras de negócio
  5. Resolve função via planilha CBO (com re-prompt ao Claude se ambíguo)
  6. POST /candidatos no eContador
  7. Aplica label no Gmail + envia notificação ao DP

Setup:
  pip install -r requirements.txt
  cp .env.example .env  # preencha as variáveis
  python main.py        # roda em loop

Para uma única passada (debug):
  python main.py --once
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from claude_client import ClaudeClient
from departamento import resolver_departamento
from ecotador_client import EContadorAPI
from funcao import carregar_planilha, resolver_funcao
from gmail_client import GmailClient
from payload_builder import (
    CAMPOS_MANUAIS_DP,
    extrair_dados_consulta,
    finalizar_payload,
    normalizar_admissoes,
    validar_campos_obrigatorios,
)


# ============================================================
# Setup
# ============================================================

ROOT = Path(__file__).parent
CONFIG_FILE = ROOT / "config.json"
LOOKUPS_FILE = ROOT / "lookups.json"
DEPARTAMENTOS_FILE = ROOT / "departamentos.json"
PLANILHA_CBO = ROOT / "funcoes_cbo.xlsx"
LOG_FILE = ROOT / "admissao_log.ndjson"
PAYLOADS_DIR = ROOT / "payloads"


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("admissao")


@dataclass
class Config:
    base_url: str
    token: str
    label_entrada: str
    label_processado: str
    label_pendente: str
    email_dp: str
    intervalo: int
    dry_run: bool
    claude_model: str
    claude_max_tokens: int
    # ─── TEMP-CONFIRMAR-REPLIES (REMOVER em produção) ───────────
    # Default LIGADO durante calibração: pergunta confirmação antes
    # de enviar cada email de pendência. Auto-desligado quando o
    # processo roda sem TTY (Task Scheduler, cron, etc.). Pode forçar
    # off com --no-ask.
    confirmar_replies: bool = True


def carregar_config() -> Config:
    raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    api_cfg = raw.get("ecotador") or raw.get("econtador") or {}
    token = api_cfg.get("token") or os.getenv("ECONTADOR_TOKEN")
    if not token or token == "SEU_TOKEN_AQUI":
        token = os.getenv("ECONTADOR_TOKEN")
    if not token:
        raise ValueError("ECONTADOR_TOKEN ausente (defina no .env)")

    gmail = raw.get("gmail", {})
    anthropic_cfg = raw.get("anthropic", {})
    return Config(
        base_url=api_cfg.get("base_url", "https://dp.pack.alterdata.com.br/api/v1"),
        token=token,
        label_entrada=gmail.get("label_entrada", "ADMISSÃO"),
        label_processado=gmail.get("label_processado", "ADMISSÃO/processado"),
        label_pendente=gmail.get("label_pendente", "ADMISSÃO/pendente"),
        email_dp=raw.get("dp", {}).get("email_notificacao", ""),
        intervalo=int(raw.get("polling_intervalo_segundos", 300)),
        dry_run=bool(raw.get("dry_run", False)),
        claude_model=anthropic_cfg.get("model", "claude-sonnet-4-20250514"),
        claude_max_tokens=int(anthropic_cfg.get("max_tokens", 8192)),
    )


def bootstrap_arquivos_locais() -> None:
    """Copia lookups.json e departamentos.json da raiz se não existirem aqui."""
    pai = ROOT.parent
    pares = [
        (pai / "lookups.json", LOOKUPS_FILE),
        (pai / "departamentos.json", DEPARTAMENTOS_FILE),
    ]
    for origem, destino in pares:
        if destino.exists():
            continue
        if origem.exists():
            shutil.copy2(origem, destino)
            log.info(f"📋 Bootstrapped {destino.name} a partir de {origem}")
        else:
            log.warning(f"⚠ {origem} não existe — pulando bootstrap de {destino.name}")


def log_jsonl(entry: dict) -> None:
    """Append-only NDJSON. Sempre inclui `campos_faltantes` (manuais DP +
    bloqueios de validação, se houver)."""
    entry["timestamp"] = datetime.now().isoformat()
    entry.setdefault("campos_faltantes", {
        "manuais_dp": CAMPOS_MANUAIS_DP,
        "validacao_bloqueada": entry.pop("_validacao_bloqueada", []),
    })
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning(f"Falha escrevendo log: {e}")


def salvar_payload(
    msg_id: str,
    metadados: dict,
    payload: dict,
    resolucao: dict | None = None,
    resultado: dict | None = None,
) -> Path:
    """Salva o payload completo + contexto em payloads/<timestamp>_<msg_id>.json.

    Chamado em 2 momentos:
      1. Após montagem (antes do POST) — preserva o payload mesmo se crashar
      2. Após resposta do POST — atualiza com candidato_id ou erro

    Sobrescreve o arquivo do mesmo msg_id (a 2ª chamada inclui o resultado).
    """
    PAYLOADS_DIR.mkdir(exist_ok=True)
    # Timestamp + msg_id curto pra evitar nomes gigantes
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    nome_arq = f"{ts}_{msg_id[:16]}.json"
    arq = PAYLOADS_DIR / nome_arq

    # Se já existe um arquivo desse msg_id na rodada atual, sobrescreve
    # (vem da 1ª chamada antes do POST — atualizamos com resultado)
    existentes = sorted(PAYLOADS_DIR.glob(f"*_{msg_id[:16]}.json"))
    if existentes:
        arq = existentes[-1]

    doc = {
        "timestamp": datetime.now().isoformat(),
        "msg_id": msg_id,
        "remetente": metadados.get("remetente", ""),
        "assunto": metadados.get("assunto", ""),
        "data_email": metadados.get("data", ""),
        "resolucao": resolucao or {},
        "resultado": resultado or {"status": "preparado", "erro": None, "candidato_id": None},
        "payload": payload,
    }
    arq.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return arq


# ============================================================
# Notificações por email
# ============================================================

def email_sucesso(candidato_id: str, payload: dict, empresa_nome: str) -> tuple[str, str]:
    attrs = payload["data"]["attributes"]
    nome = attrs.get("nome", "?")
    assunto = f"[ADMISSÃO OK] {nome} — candidato {candidato_id}"
    corpo = (
        f"Admissão criada no eContador com sucesso.\n\n"
        f"  Nome:        {nome}\n"
        f"  CPF:         {attrs.get('cpf', '?')}\n"
        f"  Empresa:     {empresa_nome}\n"
        f"  Admissão:    {attrs.get('admissao', '?')}\n"
        f"  Candidato:   {candidato_id}\n\n"
        f"Lembrete: DP precisa preencher manualmente no Alterdata Desktop\n"
        f"os ~14 campos que não chegam pelo sync (Matrícula eSocial,\n"
        f"Regime de Jornada, Horas semanais, FGTS, etc.).\n\n"
        f"Pipeline Local — {datetime.now().isoformat(timespec='seconds')}"
    )
    return assunto, corpo


def email_pendencia(motivo: str, contexto: dict) -> tuple[str, str]:
    """Email seco pro DP em casos de erro genérico (sem thread/cliente)."""
    assunto = f"[ADMISSÃO PENDENTE] {motivo[:80]}"
    ctx_str = json.dumps(contexto, ensure_ascii=False, indent=2)[:3000]
    corpo = (
        f"O pipeline NÃO conseguiu processar essa admissão automaticamente.\n\n"
        f"Motivo: {motivo}\n\n"
        f"---\n"
        f"Contexto / dados já extraídos:\n{ctx_str}\n\n"
        f"Revise e complete manualmente no eContador.\n\n"
        f"Pipeline Local — {datetime.now().isoformat(timespec='seconds')}"
    )
    return assunto, corpo


# ─── TEMP (REMOVER em produção): confirmação interativa de envio ──────
# Bloco coeso: 3 funções + 1 uso em _processar_seguro. Pesquise por
# "TEMP-CONFIRMAR-REPLIES" pra achar tudo de uma vez.

def _previa_reply(msg_orig: dict, corpo: str, cc: str | None) -> str:
    """TEMP-CONFIRMAR-REPLIES: monta preview legível pra exibir no terminal."""
    headers = {
        h["name"].lower(): h["value"]
        for h in msg_orig.get("payload", {}).get("headers", [])
    }
    de = headers.get("from", "?")
    assunto = headers.get("subject", "?")
    sep = "─" * 70
    corpo_trunc = corpo if len(corpo) <= 800 else corpo[:800] + "\n[...corpo truncado...]"
    return (
        f"\n{sep}\n"
        f"📨 PREVIEW DA RESPOSTA PRO CLIENTE\n"
        f"  Para:    {de}\n"
        f"  CC:      {cc or '(nenhum)'}\n"
        f"  Assunto: Re: {assunto[:80]}\n"
        f"\n{corpo_trunc}\n"
        f"{sep}"
    )


def _confirmar_envio(msg_orig: dict, corpo: str, cc: str | None) -> bool:
    """TEMP-CONFIRMAR-REPLIES: imprime preview e pergunta s/N. Default N.

    Se stdin não estiver disponível (fora do esperado, pq o caller já
    desliga via `confirmar_replies = False` sem TTY), retorna False
    (não envia) por precaução.
    """
    if not sys.stdin.isatty():
        log.warning("_confirmar_envio chamado sem TTY — abortando envio")
        return False
    print(_previa_reply(msg_orig, corpo, cc))
    try:
        resp = input("Enviar este email? [s/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()  # quebra de linha após ^C
        return False
    return resp in ("s", "sim", "y", "yes")


# ─── fim do bloco TEMP-CONFIRMAR-REPLIES ──────────────────────────────


def _raise_pendencia(
    erro_tecnico: str,
    motivo_cliente: str,
    *,
    campos_faltando: list[str] | None = None,
    payload_parcial: dict | None = None,
) -> None:
    """Levanta ValueError anotada com o que o reply pro cliente precisa.

    - erro_tecnico: vai pro log/email DP (texto curto, técnico)
    - motivo_cliente: vai pro corpo da resposta amigável ao cliente
    - campos_faltando: opcional, lista estruturada (caminho do validador)
    - payload_parcial: opcional, dados que já foram extraídos
    """
    err = ValueError(erro_tecnico)
    err.motivo_cliente = motivo_cliente  # type: ignore[attr-defined]
    err.campos_faltando = campos_faltando or []  # type: ignore[attr-defined]
    err.payload_parcial = payload_parcial or {}  # type: ignore[attr-defined]
    raise err


def _lista_natural(itens: list[str]) -> str:
    """Junta itens em frase natural PT-BR: 'a', 'a e b', 'a, b e c'."""
    if not itens:
        return ""
    if len(itens) == 1:
        return itens[0]
    if len(itens) == 2:
        return f"{itens[0]} e {itens[1]}"
    return ", ".join(itens[:-1]) + " e " + itens[-1]


def email_resposta_cliente(
    payload: dict | None,
    faltantes: list[str] | None,
    motivo_livre: str | None = None,
) -> str:
    """Corpo conversacional pro cliente. Foco no que FALTA; não lista o que
    já recebemos (a info ficou em payloads/ pra auditoria — o cliente vê o
    próprio email original no thread se quiser conferir).

    3 caminhos:
      - faltantes (lista do validador) → "ainda preciso de A, B e C"
      - motivo_livre (Claude _pendente etc.) → repassa a frase do motivo
      - sem nada estruturado → pedido genérico pra reenviar docs
    """
    payload = payload or {}
    attrs = (payload.get("data") or {}).get("attributes") or {}
    dados_parciais = payload.get("_dados_parciais") or {}

    # Procura o nome em cascata pra personalizar (attrs → parciais → raiz)
    nome = None
    for fonte in (attrs, dados_parciais, payload):
        if not isinstance(fonte, dict):
            continue
        for k in ("nome", "nome_completo", "funcionario"):
            v = fonte.get(k)
            if isinstance(v, str) and v.strip():
                nome = v.strip()
                break
        if nome:
            break

    abertura_cadastro = (
        f"Pra fechar o cadastro de {nome} aqui no sistema"
        if nome
        else "Pra concluir esse cadastro aqui no sistema"
    )

    if faltantes:
        lista = _lista_natural(faltantes)
        miolo = (
            f"{abertura_cadastro}, ainda preciso de algumas informações "
            f"que não consegui identificar nos documentos: {lista}.\n\n"
            f"Pode me responder esse mesmo e-mail com esses dados? Não "
            f"precisa reenviar o que já mandou — só o que falta.\n\n"
        )
    elif motivo_livre:
        miolo = (
            f"{motivo_livre}\n\n"
            f"Pode me responder esse mesmo e-mail com o que falta? "
            f"Não precisa reenviar os documentos que já mandou.\n\n"
        )
    else:
        miolo = (
            f"Recebi sua mensagem mas não consegui ler os documentos da "
            f"admissão. Pode reenviar a ficha com RG, CPF, CTPS, comprovante "
            f"de endereço e ASO do(a) candidato(a)?\n\n"
        )

    return (
        f"Olá!\n\n"
        f"{miolo}"
        f"Qualquer dúvida, é só me chamar.\n\n"
        f"Atenciosamente,\n"
        f"DP — Crosara Contabilidade"
    )


# ============================================================
# Processamento de 1 email
# ============================================================

def processar_email(
    msg: dict,
    gmail: GmailClient,
    claude: ClaudeClient,
    api: EContadorAPI,
    planilha_cbo: list[dict],
    config: Config,
) -> None:
    """Wrapper compatível: coleta dados do msg solo e delega pra processar_admissao."""
    corpo = gmail.extrair_corpo(msg)
    anexos = gmail.baixar_anexos(msg)
    metadados = gmail.extrair_metadados(msg)
    processar_admissao(
        msg_id=msg["id"],
        msg_pra_resposta=msg,
        corpo=corpo,
        anexos=anexos,
        metadados=metadados,
        gmail=gmail, claude=claude, api=api,
        planilha_cbo=planilha_cbo, config=config,
        ids_label_pendente_remover=[],
    )


def processar_thread_resposta(
    thread: dict,
    gmail: GmailClient,
    claude: ClaudeClient,
    api: EContadorAPI,
    planilha_cbo: list[dict],
    config: Config,
) -> None:
    """Cliente respondeu num thread pendente — reprocessa com TUDO do thread."""
    msgs = thread.get("messages", [])
    if not msgs:
        raise ValueError("Thread vazio")

    # Identidade do log: usa msg original (mantém audit trail consistente)
    msg_original = msgs[0]
    msg_id = msg_original["id"]

    # Coleta agregada (corpo de todas + anexos de todas)
    corpo = gmail.extrair_corpo_thread(thread)
    anexos = gmail.baixar_anexos_thread(thread)
    metadados = gmail.extrair_metadados(msg_original)

    log.info(
        f"🔁 Reprocessando thread {thread.get('id', '?')[:16]} "
        f"({len(msgs)} mensagens) → msg_id_ref={msg_id}"
    )

    # Lista de msg ids com label pendente — vamos remover após reprocessar
    label_pendente_ids: list[str] = []
    pendente_id = gmail._label_id(config.label_pendente)
    if pendente_id:
        for m in msgs:
            if pendente_id in (m.get("labelIds") or []):
                label_pendente_ids.append(m["id"])

    processar_admissao(
        msg_id=msg_id,
        msg_pra_resposta=msgs[-1],  # responde na última msg (do cliente)
        corpo=corpo,
        anexos=anexos,
        metadados=metadados,
        gmail=gmail, claude=claude, api=api,
        planilha_cbo=planilha_cbo, config=config,
        ids_label_pendente_remover=label_pendente_ids,
    )


def processar_admissao(
    msg_id: str,
    msg_pra_resposta: dict,
    corpo: str,
    anexos: list[dict],
    metadados: dict,
    gmail: GmailClient,
    claude: ClaudeClient,
    api: EContadorAPI,
    planilha_cbo: list[dict],
    config: Config,
    ids_label_pendente_remover: list[str],
) -> None:
    """Orquestrador: Claude → quebra em N blocos de admissão → processa cada
    independentemente → agrega resultados → label + email no fim.

    Casos:
      - 1 admissão: igual ao comportamento antigo
      - N admissões no mesmo email: cada uma vira um candidato no eContador.
        Empresa/departamentos vêm de cache se compartilhados (mesmo CNPJ).
        Função pode variar por funcionário (cargo diferente cada).
        Se todas passarem → label processado, email DP com a lista.
        Se alguma falhar → label pendente, reply no thread com status por
        nome (quem foi cadastrado e quem ainda precisa de info).
    """
    log.info(f"📧 {msg_id} | {metadados.get('assunto', '')[:60]}")
    log.info(f"   Corpo: {len(corpo)} chars | Anexos: {len(anexos)}")

    if not corpo and not anexos:
        _raise_pendencia(
            "Email sem corpo nem anexos PDF/imagem",
            motivo_cliente=(
                "Não recebi nenhuma informação ou anexo nessa mensagem. "
                "Pode reenviar a ficha de admissão com os documentos "
                "(RG, CPF, CTPS, comprovante de endereço, ASO)?"
            ),
        )

    # 1. Claude — 1 chamada que pode devolver N admissões
    resposta_claude = claude.gerar_payload(corpo, metadados, anexos)

    # 2. Claude marcou pendente total? Curto-circuita.
    if resposta_claude.get("_pendente"):
        motivo = resposta_claude.get("_motivo") or "Dados insuficientes"
        _raise_pendencia(
            f"Claude marcou como pendente: {motivo}",
            motivo_cliente=motivo,
            payload_parcial=resposta_claude,
        )

    # 3. Normaliza pra lista de blocos (1 ou N)
    blocos = normalizar_admissoes(resposta_claude)
    if not blocos:
        _raise_pendencia(
            "Resposta do Claude sem admissões processáveis",
            motivo_cliente=(
                "Não consegui identificar nenhuma admissão completa nos "
                "documentos. Pode reenviar a ficha com os dados do candidato?"
            ),
            payload_parcial=resposta_claude,
        )

    log.info(f"   📋 {len(blocos)} admissão(ões) detectada(s)")

    # 4. Caches compartilhados entre blocos do mesmo email (geralmente
    #    mesmo CNPJ → mesma empresa, mesmos departamentos)
    cache_empresa: dict[str, tuple[str | None, dict]] = {}
    cache_deptos: dict[str, list[dict]] = {}

    # 5. Processa cada bloco; pega exceptions de pendência individualmente
    resultados: list[dict] = []
    for i, bloco in enumerate(blocos, 1):
        attrs_pre = (bloco.get("data") or {}).get("attributes") or {}
        nome_pre = attrs_pre.get("nome") or f"funcionário #{i}"
        try:
            r = _processar_um_bloco(
                bloco=bloco, indice=i, total=len(blocos),
                msg_id=msg_id, metadados=metadados,
                api=api, claude=claude, planilha_cbo=planilha_cbo,
                config=config, corpo=corpo, anexos=anexos,
                cache_empresa=cache_empresa, cache_deptos=cache_deptos,
            )
            resultados.append(r)
        except ValueError as e:
            log.exception(f"   ❌ [{i}/{len(blocos)}] {nome_pre}: {e}")
            resultados.append({
                "indice": i,
                "nome": nome_pre,
                "ok": False,
                "candidato_id": None,
                "erro_tecnico": str(e),
                "motivo_cliente": getattr(e, "motivo_cliente", None) or str(e),
                "campos_faltando": getattr(e, "campos_faltando", []) or [],
                "payload_parcial": getattr(e, "payload_parcial", None) or bloco,
                "razao_social": None,
            })

    # 6. Agrega: label, reply ou email DP, log
    _finalizar_lote(
        resultados, msg_id, msg_pra_resposta,
        ids_label_pendente_remover, gmail, config,
    )


def _resultado_pendencia_interna(
    *, indice: int, nome: str, razao: str | None,
    erro: str, diagnostico_dp: str, bloco: dict,
) -> dict:
    """Resultado pra pendência cujo problema é INTERNO (cargo/CBO/cadastro
    do escritório). NÃO comunica nada ao cliente — só ao DP via email seco.
    `interno: True` faz `_finalizar_lote` rotear pra esse caminho.
    """
    log.warning(f"      🔧 [{indice}] {nome}: pendência interna — {erro}")
    return {
        "indice": indice,
        "nome": nome,
        "ok": False,
        "interno": True,
        "candidato_id": None,
        "erro_tecnico": erro,
        "diagnostico_dp": diagnostico_dp,
        "motivo_cliente": None,
        "campos_faltando": [],
        "payload_parcial": bloco,
        "razao_social": razao,
    }


def _processar_um_bloco(
    *, bloco: dict, indice: int, total: int,
    msg_id: str, metadados: dict,
    api: EContadorAPI, claude: ClaudeClient, planilha_cbo: list[dict],
    config: Config, corpo: str, anexos: list[dict],
    cache_empresa: dict, cache_deptos: dict,
) -> dict:
    """Processa UM bloco de admissão. Retorna dict de resultado.
    Levanta ValueError (via _raise_pendencia) se pendência for específica deste bloco.
    """
    attrs = (bloco.get("data") or {}).get("attributes") or {}
    nome = attrs.get("nome") or f"funcionário #{indice}"
    log.info(f"   👤 [{indice}/{total}] {nome}")

    dados = extrair_dados_consulta(bloco)
    cnpj = dados["cnpj_empresa"]
    if not cnpj:
        _raise_pendencia(
            f"CNPJ ausente para {nome}",
            motivo_cliente=(
                f"Pra cadastrar {nome}, preciso do CNPJ da empresa "
                f"contratante. Não consegui identificar nos documentos."
            ),
            payload_parcial=bloco,
        )

    # Empresa (cache)
    if cnpj in cache_empresa:
        empresa_id, empresa_attrs = cache_empresa[cnpj]
    else:
        empresa_id, empresa_attrs = api.resolver_empresa(cnpj)
        cache_empresa[cnpj] = (empresa_id, empresa_attrs)
    if not empresa_id:
        _raise_pendencia(
            f"CNPJ {cnpj} não encontrado em /empresas",
            motivo_cliente=(
                f"O CNPJ {cnpj} (referenciado pra {nome}) não está "
                f"cadastrado no nosso sistema. Pode confirmar?"
            ),
            payload_parcial=bloco,
        )
    razao = empresa_attrs.get("nome", "?")
    log.info(f"      🏢 Empresa {empresa_id}: {razao}")

    # Departamento (cache de listagem por empresa)
    if empresa_id in cache_deptos:
        deptos_api = cache_deptos[empresa_id]
    else:
        deptos_api = api.listar_departamentos(empresa_id)
        cache_deptos[empresa_id] = deptos_api
    depto_id, depto_msg = resolver_departamento(
        empresa_id=empresa_id, cnpj_empresa=cnpj, razao_social=razao,
        deptos_api=deptos_api, departamento_sugerido=dados["departamento_sugerido"],
        departamentos_json_paths=[DEPARTAMENTOS_FILE, ROOT.parent / "departamentos.json"],
    )
    if depto_msg != "ok":
        log.warning(f"      🗂 Depto não resolvido: {depto_msg} — seguindo sem")
    else:
        log.info(f"      🗂 Depto: {depto_id}")

    # Função — pendências aqui são INTERNAS (problema do nosso cadastro/planilha,
    # não do cliente). Não escala pro cliente; vira email seco pro DP.
    funcao_id, conf, ambiguos, fmsg = resolver_funcao(
        planilha_cbo, dados["cargo"], dados["cbo"]
    )
    if funcao_id is None and ambiguos:
        log.info(f"      💼 Ambíguo ({len(ambiguos)}) — re-prompt Claude")
        resposta2 = claude.gerar_payload(corpo, metadados, anexos, funcoes_candidatas=ambiguos)
        blocos2 = normalizar_admissoes(resposta2)
        bloco2 = _localizar_bloco_correspondente(blocos2, attrs)
        if bloco2:
            dados2 = extrair_dados_consulta(bloco2)
            funcao_id2, conf2, _amb, fmsg2 = resolver_funcao(
                planilha_cbo, dados2["cargo"], dados2["cbo"]
            )
            if funcao_id2:
                funcao_id, conf, bloco = funcao_id2, conf2, bloco2
            else:
                return _resultado_pendencia_interna(
                    indice=indice, nome=nome, razao=razao,
                    erro=f"Função ambígua após re-prompt: {fmsg2}",
                    diagnostico_dp=(
                        f"Cargo extraído (Claude tentou 2x): "
                        f"'{dados.get('cargo')}' / CBO '{dados.get('cbo') or '?'}'. "
                        f"Candidatos da planilha (top {min(len(ambiguos), 10)}): "
                        + "; ".join(
                            f"{f['nome_cargo']} (id={f['funcao_id']}, cbo={f.get('cbo') or '?'})"
                            for f in ambiguos[:10]
                        )
                    ),
                    bloco=bloco,
                )
        else:
            return _resultado_pendencia_interna(
                indice=indice, nome=nome, razao=razao,
                erro="Re-prompt do Claude não retornou bloco correspondente",
                diagnostico_dp=(
                    f"Cargo extraído: '{dados.get('cargo')}'. "
                    f"Claude não conseguiu se localizar entre as opções."
                ),
                bloco=bloco,
            )
    elif funcao_id is None:
        return _resultado_pendencia_interna(
            indice=indice, nome=nome, razao=razao,
            erro=f"Função não encontrada: {fmsg}",
            diagnostico_dp=(
                f"Cargo extraído: '{dados.get('cargo')}' / "
                f"CBO '{dados.get('cbo') or '?'}'. "
                f"Mensagem do resolver: {fmsg}. "
                f"Considerar: cadastrar a função no eContador, ou marcar X "
                f"em um cargo equivalente da planilha funcoes_cbo.xlsx."
            ),
            bloco=bloco,
        )
    log.info(f"      💼 Função: {funcao_id} ({conf:.0%})")

    # Payload final + sanitização
    payload = finalizar_payload(bloco, empresa_id, depto_id, funcao_id)

    # Snapshot pra auditoria
    resolucao = {
        "indice": indice, "total": total, "nome": nome,
        "cnpj_empresa": cnpj, "empresa_id": empresa_id,
        "razao_social": razao,
        "departamento_id": depto_id, "departamento_motivo": depto_msg,
        "departamento_sugerido": dados.get("departamento_sugerido"),
        "funcao_id": funcao_id, "funcao_confianca": round(conf, 4),
        "cargo_extraido": dados.get("cargo"),
        "cbo_extraido": dados.get("cbo"),
    }

    # Validação determinística
    faltando = validar_campos_obrigatorios(payload)
    if faltando:
        log.error(f"      ⛔ Campos faltando ({nome}): {faltando}")
        salvar_payload(
            msg_id, metadados, payload, resolucao=resolucao,
            resultado={
                "status": "pendente_validacao", "candidato_id": None,
                "erro": "Campos obrigatórios faltando",
                "campos_faltando": faltando,
            },
        )
        _raise_pendencia(
            f"Campos faltando para {nome}: {', '.join(faltando)}",
            motivo_cliente="",  # ignorado quando campos_faltando está populado
            campos_faltando=faltando,
            payload_parcial=payload,
        )

    arq_payload = salvar_payload(msg_id, metadados, payload, resolucao=resolucao)

    if config.dry_run:
        log.info(f"      DRY-RUN — pulando POST de {nome}")
        salvar_payload(
            msg_id, metadados, payload, resolucao=resolucao,
            resultado={"status": "dry_run", "candidato_id": None, "erro": None},
        )
        return {
            "indice": indice, "nome": nome, "ok": True,
            "candidato_id": None, "dry_run": True,
            "razao_social": razao,
            "empresa_id": empresa_id, "departamento_id": depto_id, "funcao_id": funcao_id,
            "payload_path": str(arq_payload.name),
        }

    ok, ref, body_err = api.post_candidato(payload)
    if ok:
        candidato_id = ref
        log.info(f"      ✅ Candidato {candidato_id} criado pra {nome}")
        salvar_payload(
            msg_id, metadados, payload, resolucao=resolucao,
            resultado={"status": "sucesso", "candidato_id": candidato_id, "erro": None},
        )
        return {
            "indice": indice, "nome": nome, "ok": True,
            "candidato_id": candidato_id,
            "razao_social": razao,
            "empresa_id": empresa_id, "departamento_id": depto_id, "funcao_id": funcao_id,
            "payload_path": str(arq_payload.name),
        }

    log.error(f"      ❌ POST falhou pra {nome}: {ref}\n{body_err}")
    salvar_payload(
        msg_id, metadados, payload, resolucao=resolucao,
        resultado={"status": "falha_post", "candidato_id": None,
                   "erro": ref, "body": body_err[:2000]},
    )
    return {
        "indice": indice, "nome": nome, "ok": False,
        "candidato_id": None,
        "erro_tecnico": f"{ref}: {body_err[:200]}",
        "motivo_cliente": (
            f"Falha técnica ao enviar {nome} pro eContador ({ref}). "
            f"O DP foi avisado e vai investigar."
        ),
        "campos_faltando": [],
        "razao_social": razao,
        "payload_path": str(arq_payload.name),
    }


def _localizar_bloco_correspondente(blocos: list[dict], attrs_alvo: dict) -> dict | None:
    """Acha o bloco em `blocos` cujo CPF ou nome bate com `attrs_alvo`."""
    cpf_alvo = str(attrs_alvo.get("cpf", "")).strip() or None
    nome_alvo = (attrs_alvo.get("nome") or "").strip().upper() or None
    for b in blocos:
        a = (b.get("data") or {}).get("attributes") or {}
        if cpf_alvo and str(a.get("cpf", "")).strip() == cpf_alvo:
            return b
        if nome_alvo and (a.get("nome") or "").strip().upper() == nome_alvo:
            return b
    # Fallback: se só tem 1 bloco, retorna ele
    return blocos[0] if len(blocos) == 1 else None


def _finalizar_lote(
    resultados: list[dict],
    msg_id: str,
    msg_pra_resposta: dict,
    ids_label_pendente_remover: list[str],
    gmail: GmailClient,
    config: Config,
) -> None:
    """Após processar N admissões: decide label, manda 1 email consolidado,
    loga cada resultado."""
    if not resultados:
        log.warning("   _finalizar_lote chamado sem resultados")
        return

    n_ok = sum(1 for r in resultados if r["ok"])
    n_total = len(resultados)
    log.info(f"   📊 Resultado do lote: {n_ok}/{n_total} sucesso")

    todos_ok = (n_ok == n_total)

    if todos_ok:
        # Limpa pendente de msgs antigas e marca processado
        for mid in ids_label_pendente_remover:
            try:
                gmail.remover_label(mid, config.label_pendente)
            except Exception as e:
                log.warning(f"   Falha removendo pendente de {mid}: {e}")
        gmail.aplicar_label(msg_id, config.label_processado)

        # Email pro DP (não pro cliente) com resumo das criações
        if config.email_dp:
            try:
                assunto, corpo = _corpo_email_sucesso_lote(resultados)
                gmail.enviar_email(config.email_dp, assunto, corpo)
            except Exception:
                log.exception("Falha enviando email de sucesso pro DP")

        for r in resultados:
            log_jsonl({
                "msg_id": msg_id, "status": "sucesso",
                "indice": r.get("indice"), "nome": r.get("nome"),
                "candidato_id": r.get("candidato_id"),
                "empresa_id": r.get("empresa_id"),
                "departamento_id": r.get("departamento_id"),
                "funcao_id": r.get("funcao_id"),
                "payload_path": r.get("payload_path"),
            })
        return

    # Tem pelo menos UMA falha — label pendente
    gmail.aplicar_label(msg_id, config.label_pendente)

    sucessos = [r for r in resultados if r["ok"]]
    falhas_internas = [r for r in resultados if not r["ok"] and r.get("interno")]
    falhas_cliente = [r for r in resultados if not r["ok"] and not r.get("interno")]
    log.info(
        f"   📊 Detalhe: {len(sucessos)} OK | "
        f"{len(falhas_cliente)} pendente cliente | "
        f"{len(falhas_internas)} pendente interna (DP)"
    )

    # 1. Reply no thread — só se há algo a comunicar ao cliente.
    #    Inclui sucessos (pra ele saber que parte foi cadastrada),
    #    falhas_cliente (pedindo o que falta), e menciona suavemente
    #    pendências internas como "estou organizando aqui do nosso lado"
    #    (sem expor termos técnicos como CBO).
    if msg_pra_resposta and (sucessos or falhas_cliente or falhas_internas):
        try:
            corpo = _corpo_reply_lote(resultados)
            if config.confirmar_replies and not _confirmar_envio(
                msg_pra_resposta, corpo, config.email_dp or None
            ):
                log.warning("   ✋ Envio cancelado pelo usuário (--ask)")
            else:
                gmail.responder_no_thread(
                    msg_pra_resposta, corpo=corpo, cc=config.email_dp or None
                )
                log.info(
                    f"   📨 Reply consolidado enviado "
                    f"({len(sucessos)} OK + {len(falhas_cliente)} cliente "
                    f"+ {len(falhas_internas)} interna)"
                )
        except Exception:
            log.exception("Falha enviando reply consolidado")

    # 2. Email seco pro DP — só se há pendências internas (técnicas)
    if falhas_internas and config.email_dp:
        try:
            assunto, corpo = _corpo_email_pendencia_interna(falhas_internas, sucessos)
            gmail.enviar_email(config.email_dp, assunto, corpo)
            log.info(f"   📧 Email DP enviado ({len(falhas_internas)} pendência(s) interna(s))")
        except Exception:
            log.exception("Falha enviando email seco pro DP")

    for r in resultados:
        log_jsonl({
            "msg_id": msg_id,
            "status": (
                "sucesso" if r["ok"]
                else (
                    "pendente_interno" if r.get("interno")
                    else ("pendente_validacao" if r.get("campos_faltando") else "erro")
                )
            ),
            "indice": r.get("indice"),
            "nome": r.get("nome"),
            "candidato_id": r.get("candidato_id"),
            "erro": r.get("erro_tecnico"),
            "_validacao_bloqueada": r.get("campos_faltando", []),
        })


def _corpo_email_sucesso_lote(resultados: list[dict]) -> tuple[str, str]:
    """Email DP quando TODOS deram certo (1 ou N)."""
    n = len(resultados)
    razao = next((r.get("razao_social") for r in resultados if r.get("razao_social")), "?")
    if n == 1:
        r = resultados[0]
        assunto = f"[ADMISSÃO OK] {r['nome']} — candidato {r.get('candidato_id', '?')}"
    else:
        assunto = f"[ADMISSÃO OK] {n} candidatos criados ({razao})"

    linhas = []
    for r in resultados:
        cid = r.get("candidato_id") or ("(dry-run)" if r.get("dry_run") else "?")
        linhas.append(f"  • {r['nome']} → candidato {cid}")

    corpo = (
        f"Admissão criada no eContador com sucesso.\n\n"
        f"Empresa: {razao}\n\n"
        f"Funcionário(s) cadastrado(s):\n"
        + "\n".join(linhas)
        + "\n\n"
        + "Lembrete: DP precisa preencher manualmente no Alterdata Desktop\n"
        + "os ~14 campos que não chegam pelo sync E-plugin (Matrícula eSocial,\n"
        + "Regime de Jornada, FGTS, etc.).\n\n"
        + f"Pipeline Local — {datetime.now().isoformat(timespec='seconds')}"
    )
    return assunto, corpo


def _corpo_reply_lote(resultados: list[dict]) -> str:
    """Reply consolidado no thread.

    3 categorias de funcionário no mesmo email:
      - sucessos: "Já cadastrei X."
      - falhas_cliente (problema do cliente, ex: faltou ASO): "Pra Y, ainda
        preciso de Z."
      - falhas_internas (problema nosso, ex: cargo não cadastrado): "Pra W,
        estou organizando alguns detalhes do nosso lado; te aviso assim
        que estiver pronto." (sem termo técnico — transparente pro cliente)
    """
    sucessos = [r for r in resultados if r["ok"]]
    falhas_cliente = [r for r in resultados if not r["ok"] and not r.get("interno")]
    falhas_internas = [r for r in resultados if not r["ok"] and r.get("interno")]

    blocos: list[str] = []

    if sucessos:
        nomes = [r["nome"] for r in sucessos]
        if len(nomes) == 1:
            blocos.append(f"Já cadastrei {nomes[0]} no sistema.")
        else:
            blocos.append(f"Já cadastrei no sistema: {_lista_natural(nomes)}.")

    for r in falhas_cliente:
        nome = r["nome"]
        if r.get("campos_faltando"):
            lista = _lista_natural(r["campos_faltando"])
            blocos.append(f"Pra {nome}, ainda preciso de: {lista}.")
        elif r.get("motivo_cliente"):
            blocos.append(f"Pra {nome}: {r['motivo_cliente']}")
        else:
            blocos.append(f"Pra {nome}: {r.get('erro_tecnico', 'não foi possível processar.')}")

    if falhas_internas:
        nomes = [r["nome"] for r in falhas_internas]
        nomes_str = _lista_natural(nomes)
        if len(nomes) == 1:
            blocos.append(
                f"Pra {nomes_str}, estou organizando alguns detalhes "
                f"aqui do nosso lado — te aviso assim que estiver pronto."
            )
        else:
            blocos.append(
                f"Pra {nomes_str}, estou organizando alguns detalhes "
                f"aqui do nosso lado — te aviso assim que estiverem prontos."
            )

    miolo = "\n\n".join(blocos)

    pedido_resposta = (
        "Pode me responder esse mesmo e-mail com o que falta? Não precisa "
        "reenviar os documentos que já mandou.\n\n"
        if falhas_cliente else ""
    )

    return (
        f"Olá!\n\n"
        f"{miolo}\n\n"
        f"{pedido_resposta}"
        f"Qualquer dúvida, é só me chamar.\n\n"
        f"Atenciosamente,\n"
        f"DP — Crosara Contabilidade"
    )


def _corpo_email_pendencia_interna(
    internas: list[dict],
    sucessos: list[dict],
) -> tuple[str, str]:
    """Email seco pro DP com detalhes técnicos das pendências internas.

    Inclui contexto dos sucessos do mesmo email pra DP saber o que já foi
    processado e quais ainda precisam de intervenção manual.
    """
    n = len(internas)
    nomes = [r["nome"] for r in internas]
    nomes_str = ", ".join(nomes[:3]) + ("..." if len(nomes) > 3 else "")
    assunto = f"[ADMISSÃO — Resolver internamente] {n} candidato(s): {nomes_str}"

    blocos = []
    for r in internas:
        blocos.append(
            f"=== {r['nome']} ===\n"
            f"Erro: {r.get('erro_tecnico', '?')}\n"
            f"Diagnóstico: {r.get('diagnostico_dp', '(sem detalhes)')}"
        )

    if sucessos:
        contexto_ok = "\n".join(
            f"  • {s['nome']} → candidato {s.get('candidato_id') or '(dry-run)'}"
            for s in sucessos
        )
        contexto = f"\nNo mesmo email, já processei com sucesso:\n{contexto_ok}\n"
    else:
        contexto = ""

    corpo = (
        f"Pendência INTERNA — o cliente NÃO foi avisado deste problema técnico.\n"
        f"(O reply no thread mencionou apenas que 'estou organizando detalhes'.)\n\n"
        f"As admissões abaixo não puderam ser processadas por questões do nosso\n"
        f"cadastro (cargo/CBO não está no eContador, função ambígua na planilha\n"
        f"funcoes_cbo.xlsx, etc.). O cliente já mandou os dados — agora é o DP\n"
        f"que precisa cadastrar a função correta no eContador (ou marcar X em\n"
        f"um cargo equivalente na planilha) e reprocessar manualmente.\n"
        f"{contexto}\n"
        + "\n\n".join(blocos)
        + f"\n\nPipeline Local — {datetime.now().isoformat(timespec='seconds')}"
    )
    return assunto, corpo


# ============================================================
# Loop principal
# ============================================================

def rodar_uma_passada(config: Config, claude: ClaudeClient, planilha: list[dict]) -> None:
    gmail = GmailClient()
    api = EContadorAPI(config.base_url, config.token)
    try:
        # ---- 1. Emails NOVOS (sem label de processado/pendente) ------
        emails = gmail.buscar_emails_pendentes(
            config.label_entrada, config.label_processado, config.label_pendente
        )
        log.info(f"📥 {len(emails)} email(s) novo(s)")

        for msg in emails:
            _processar_seguro(
                lambda m=msg: processar_email(m, gmail, claude, api, planilha, config),
                msg_id=msg["id"],
                msg_pra_label=msg["id"],
                msg_pra_resposta=msg,
                gmail=gmail, config=config,
            )

        # ---- 2. Threads PENDENTES com resposta do cliente ------------
        threads = gmail.buscar_threads_aguardando_cliente(config.label_pendente)
        if threads:
            log.info(f"🔁 {len(threads)} thread(s) com resposta do cliente")
        for thread in threads:
            tid = thread.get("id", "?")
            msgs = thread.get("messages", []) or []
            ref_id = msgs[0]["id"] if msgs else tid
            ultima_msg = msgs[-1] if msgs else None
            _processar_seguro(
                lambda t=thread: processar_thread_resposta(
                    t, gmail, claude, api, planilha, config
                ),
                msg_id=ref_id,
                msg_pra_label=msgs[-1]["id"] if msgs else ref_id,
                msg_pra_resposta=ultima_msg,
                gmail=gmail, config=config,
            )
    finally:
        api.close()


def _processar_seguro(
    fn,
    msg_id: str,
    msg_pra_label: str,
    msg_pra_resposta: dict | None,
    gmail: GmailClient,
    config: Config,
) -> None:
    """Executa fn() capturando exceptions:
      - aplica label pendente
      - envia reply amigável NO THREAD original (CC pro DP) — único canal de
        comunicação em casos de pendência, evitando email seco duplicado
      - loga em admissao_log.ndjson
    """
    try:
        fn()
    except Exception as e:
        log.exception(f"❌ {msg_id}: {e}")

        motivo_cliente = getattr(e, "motivo_cliente", None)
        campos_faltando = getattr(e, "campos_faltando", None) or []
        payload_parcial = getattr(e, "payload_parcial", None)
        e_e_pendencia = bool(motivo_cliente or campos_faltando)

        # 1. Label pendente
        try:
            gmail.aplicar_label(msg_pra_label, config.label_pendente)
        except Exception:
            log.exception("Falha aplicando label pendente")

        # 2. Resposta no thread (preferido) OU email seco pro DP (fallback)
        reply_enviado = False
        envio_pulado_pelo_usuario = False  # TEMP-CONFIRMAR-REPLIES
        if msg_pra_resposta and e_e_pendencia:
            try:
                corpo = email_resposta_cliente(
                    payload_parcial, campos_faltando, motivo_livre=motivo_cliente
                )
                # ─── TEMP-CONFIRMAR-REPLIES (remover este if em produção) ─
                if config.confirmar_replies and not _confirmar_envio(
                    msg_pra_resposta, corpo, config.email_dp or None
                ):
                    log.warning("   ✋ Envio cancelado pelo usuário (--ask)")
                    envio_pulado_pelo_usuario = True
                # ─── fim TEMP-CONFIRMAR-REPLIES ───────────────────────────
                else:
                    gmail.responder_no_thread(
                        msg_pra_resposta, corpo=corpo, cc=config.email_dp or None
                    )
                    reply_enviado = True
                    log.info(
                        f"   📨 Reply enviado no thread "
                        f"(cliente + DP em CC)"
                    )
            except Exception:
                log.exception("Falha enviando reply no thread")

        # Fallback email seco pro DP — pula se usuário cancelou (--ask)
        if (
            not reply_enviado
            and not envio_pulado_pelo_usuario  # TEMP-CONFIRMAR-REPLIES
            and config.email_dp
        ):
            try:
                assunto, corpo = email_pendencia(str(e), {})
                gmail.enviar_email(config.email_dp, assunto, corpo)
            except Exception:
                log.exception("Falha enviando email seco pro DP")

        # 3. Log NDJSON
        log_jsonl({
            "msg_id": msg_id,
            "status": "pendente_validacao" if campos_faltando else "erro",
            "erro": str(e),
            "_validacao_bloqueada": campos_faltando,
        })


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pipeline de admissão local da Crosara. "
                    "Padrão: roda UMA passada (emails novos + threads aguardando "
                    "cliente) e sai — feito pra Windows Task Scheduler."
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Em vez de uma passada, fica em loop infinito fazendo polling "
             "(intervalo do config.json). Útil pra rodar manualmente em terminal.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="(Compatibilidade) idêntico ao padrão.",
    )
    # ─── TEMP-CONFIRMAR-REPLIES (remover em produção) ────────────────
    # Default: confirma antes de cada email (modo calibração). Auto-desliga
    # quando não tem TTY (Task Scheduler). Flag --no-ask força off sempre.
    parser.add_argument(
        "--no-ask",
        action="store_true",
        help="[TEMP] Desliga a confirmação interativa antes de cada email "
             "de pendência. Default é PERGUNTAR; só passe esta flag se quiser "
             "que o pipeline envie sem perguntar (ex: rodando em script).",
    )
    parser.add_argument(
        "--ask",
        action="store_true",
        help="[TEMP/DEPRECATED] No-op — confirmação já é default. Mantido "
             "pra compatibilidade com atalhos existentes.",
    )
    # ─── fim TEMP-CONFIRMAR-REPLIES ──────────────────────────────────
    args = parser.parse_args()

    log.info("=" * 70)
    log.info("Pipeline de Admissão Local — Crosara Contabilidade")
    log.info("=" * 70)

    try:
        config = carregar_config()
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as e:
        log.error(f"Erro de configuração: {e}")
        return 1

    # ─── TEMP-CONFIRMAR-REPLIES (remover em produção) ────────────────
    # Resolução do modo confirmação:
    #   1. --no-ask explícito → SEMPRE desliga
    #   2. Sem TTY (Task Scheduler/redirect) → desliga automaticamente
    #   3. Caso contrário → default LIGADO (configurado na Config)
    if args.no_ask:
        config.confirmar_replies = False
        log.info("ℹ Confirmação interativa DESLIGADA por --no-ask")
    elif not sys.stdin.isatty():
        config.confirmar_replies = False
        log.info(
            "ℹ Sem TTY (provavelmente Task Scheduler) — "
            "confirmação interativa DESLIGADA automaticamente"
        )
    else:
        log.warning(
            "⚠ MODO CONFIRMAR ATIVO (default): cada email de pendência "
            "exigirá confirmação no terminal antes do envio. "
            "Use --no-ask pra desligar."
        )
    # ─── fim TEMP-CONFIRMAR-REPLIES ──────────────────────────────────

    bootstrap_arquivos_locais()

    try:
        planilha = carregar_planilha(PLANILHA_CBO)
        log.info(f"📊 Planilha CBO: {len(planilha)} cargos")
    except (FileNotFoundError, ValueError) as e:
        log.error(f"Planilha CBO inválida: {e}")
        return 1

    try:
        claude = ClaudeClient(model=config.claude_model, max_tokens=config.claude_max_tokens)
    except Exception as e:
        log.error(f"Falha inicializando Claude: {e}")
        return 2

    if config.dry_run:
        log.warning("⚠ DRY-RUN ATIVO — não envia POSTs nem emails")

    if not args.loop:
        # Padrão: uma única passada e encerra (ideal pro Task Scheduler)
        log.info("▶ Executando passada única...")
        try:
            rodar_uma_passada(config, claude, planilha)
        except Exception as e:
            log.exception(f"Erro na passada: {e}")
            return 3
        log.info("✅ Passada concluída. Encerrando.")
        return 0

    # --loop: polling contínuo (uso manual)
    log.info(f"⏱  Modo loop ativo — polling a cada {config.intervalo}s. Ctrl+C pra parar.")
    while True:
        try:
            rodar_uma_passada(config, claude, planilha)
        except Exception as e:
            log.exception(f"Erro na passada: {e}")
        try:
            time.sleep(config.intervalo)
        except KeyboardInterrupt:
            log.info("Encerrado pelo usuário.")
            return 0


if __name__ == "__main__":
    sys.exit(main())
