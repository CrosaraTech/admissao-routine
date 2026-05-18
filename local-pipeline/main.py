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


def email_resposta_cliente(
    payload: dict | None,
    faltantes: list[str] | None,
    motivo_livre: str | None = None,
) -> str:
    """Corpo da resposta amigável ao cliente no thread original.

    Adapta o conteúdo ao contexto:
      - faltantes não-vazio: lista estruturada (caminho do validador) + o
        que já foi extraído pra cliente conferir
      - motivo_livre: texto livre (Claude marcou _pendente, CNPJ inválido,
        função sem match, etc.)
    """
    payload = payload or {}
    attrs = (payload.get("data") or {}).get("attributes") or {}
    nome = attrs.get("nome") or "este(a) candidato(a)"

    # Resumo do que já foi extraído (campos chave)
    ja_temos: list[str] = []
    def add(rotulo: str, valor) -> None:
        if valor not in (None, "", 0):
            ja_temos.append(f"  • {rotulo}: {valor}")

    add("Nome", attrs.get("nome"))
    add("CPF", attrs.get("cpf"))
    add("Data de Admissão", attrs.get("admissao"))
    add("Data de Nascimento", attrs.get("nascimento"))
    add("Cargo", attrs.get("nomecargo"))
    add("Salário", attrs.get("salario"))
    bloco_temos = "\n".join(ja_temos) or "  (ainda não conseguimos identificar dados)"

    if faltantes:
        bloco_pendentes = "\n".join(f"  • {f}" for f in faltantes)
        miolo = (
            f"Para concluirmos o cadastro de {nome}, ainda precisamos "
            f"dos seguintes dados que não conseguimos identificar:\n\n"
            f"⚠ Pendentes:\n{bloco_pendentes}\n\n"
            f"✅ O que já recebemos:\n{bloco_temos}\n\n"
        )
    elif motivo_livre:
        miolo = (
            f"Recebemos sua mensagem, mas ainda não temos informação "
            f"suficiente pra cadastrar a admissão.\n\n"
            f"⚠ {motivo_livre}\n\n"
            f"✅ O que já recebemos:\n{bloco_temos}\n\n"
        )
    else:
        miolo = (
            f"Recebemos sua mensagem, mas ainda não conseguimos processar "
            f"a admissão automaticamente. Pode reenviar os documentos e "
            f"informações do(a) candidato(a)?\n\n"
        )

    return (
        f"Olá!\n\n"
        f"{miolo}"
        f"Por favor, responda este mesmo e-mail com o que falta — "
        f"não precisa reenviar os documentos que já mandou.\n\n"
        f"Qualquer dúvida, é só responder.\n\n"
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
    log.info(f"📧 {msg_id} | {metadados.get('assunto', '')[:60]}")
    log.info(f"   Corpo: {len(corpo)} chars | Anexos: {len(anexos)}")

    if not corpo and not anexos:
        _raise_pendencia(
            "Email sem corpo nem anexos PDF/imagem",
            motivo_cliente=(
                "Não recebemos nenhuma informação ou anexo nessa mensagem. "
                "Pode reenviar os documentos do candidato (ficha de admissão, RG, "
                "CTPS, comprovante de endereço, etc.)?"
            ),
        )

    # 2. Claude extrai os campos e devolve payload mais o cnpj/departamento sugeridos
    resposta_claude = claude.gerar_payload(corpo, metadados, anexos)
    dados = extrair_dados_consulta(resposta_claude)

    if dados["pendente"]:
        motivo = dados["motivo_pendencia"] or "Dados insuficientes"
        _raise_pendencia(
            f"Claude marcou como pendente: {motivo}",
            motivo_cliente=motivo,
            payload_parcial=resposta_claude,
        )

    cnpj = dados["cnpj_empresa"]
    if not cnpj:
        _raise_pendencia(
            "Claude não extraiu o CNPJ da empresa",
            motivo_cliente=(
                "Não conseguimos identificar o CNPJ da empresa contratante "
                "nos documentos enviados. Pode informar o CNPJ na resposta?"
            ),
            payload_parcial=resposta_claude,
        )

    # 3. Empresa
    empresa_id, empresa_attrs = api.resolver_empresa(cnpj)
    if not empresa_id:
        _raise_pendencia(
            f"CNPJ {cnpj} não encontrado em /empresas",
            motivo_cliente=(
                f"O CNPJ {cnpj} não está cadastrado no nosso sistema. "
                f"Pode confirmar se o CNPJ está correto?"
            ),
            payload_parcial=resposta_claude,
        )
    razao = empresa_attrs.get("nome", "?")
    log.info(f"   🏢 Empresa {empresa_id}: {razao}")

    # 4. Departamento
    deptos_api = api.listar_departamentos(empresa_id)
    depto_id, depto_msg = resolver_departamento(
        empresa_id=empresa_id,
        cnpj_empresa=cnpj,
        razao_social=razao,
        deptos_api=deptos_api,
        departamento_sugerido=dados["departamento_sugerido"],
        departamentos_json_paths=[DEPARTAMENTOS_FILE, ROOT.parent / "departamentos.json"],
    )
    if depto_msg != "ok":
        log.warning(f"   🗂 Depto não resolvido: {depto_msg} — seguindo sem")
    else:
        log.info(f"   🗂 Depto: {depto_id}")

    # 5. Função
    funcao_id, conf, ambiguos, fmsg = resolver_funcao(
        planilha_cbo, dados["cargo"], dados["cbo"]
    )
    if funcao_id is None and ambiguos:
        log.info(f"   💼 Função ambígua ({len(ambiguos)} candidatos) — re-prompt Claude")
        resposta_claude2 = claude.gerar_payload(
            corpo, metadados, anexos, funcoes_candidatas=ambiguos
        )
        dados2 = extrair_dados_consulta(resposta_claude2)
        funcao_id2, conf2, _ambig2, fmsg2 = resolver_funcao(
            planilha_cbo, dados2["cargo"], dados2["cbo"]
        )
        if funcao_id2:
            funcao_id, conf, fmsg = funcao_id2, conf2, fmsg2
            resposta_claude = resposta_claude2
        else:
            cargo = dados.get("cargo") or "?"
            _raise_pendencia(
                f"Função ainda ambígua após re-prompt: {fmsg2}",
                motivo_cliente=(
                    f"O cargo informado ({cargo}) tem várias variantes parecidas "
                    f"no nosso cadastro. Pode informar o nome EXATO do cargo "
                    f"(e o código CBO, se tiver)?"
                ),
                payload_parcial=resposta_claude,
            )
    elif funcao_id is None:
        cargo = dados.get("cargo") or "?"
        _raise_pendencia(
            f"Função: {fmsg}",
            motivo_cliente=(
                f"O cargo informado ({cargo}) não está cadastrado no nosso "
                f"sistema. Pode informar um nome de cargo equivalente, ou "
                f"o código CBO?"
            ),
            payload_parcial=resposta_claude,
        )
    log.info(f"   💼 Função: {funcao_id} ({conf:.0%})")

    # 6. Payload final
    payload = finalizar_payload(resposta_claude, empresa_id, depto_id, funcao_id)
    log.info(
        f"   📦 Payload: {len(payload['data']['attributes'])} attrs + "
        f"{len(payload['data']['relationships'])} rels"
    )

    # 6.5 Validação determinística — campos obrigatórios pelo eContador
    faltando = validar_campos_obrigatorios(payload)
    if faltando:
        log.error(f"   ⛔ Campos obrigatórios faltando: {faltando}")
        salvar_payload(
            msg_id, metadados, payload,
            resolucao={
                "cnpj_empresa": cnpj, "empresa_id": empresa_id,
                "razao_social": razao, "departamento_id": depto_id,
                "funcao_id": funcao_id,
            },
            resultado={
                "status": "pendente_validacao", "candidato_id": None,
                "erro": "Campos obrigatórios faltando",
                "campos_faltando": faltando,
            },
        )
        _raise_pendencia(
            f"Campos obrigatórios faltando: {', '.join(faltando)}",
            motivo_cliente="",  # ignorado quando campos_faltando está populado
            campos_faltando=faltando,
            payload_parcial=payload,
        )

    # Snapshot resolução pra auditoria (vai pro arquivo do payload)
    resolucao = {
        "cnpj_empresa": cnpj,
        "empresa_id": empresa_id,
        "razao_social": razao,
        "departamento_id": depto_id,
        "departamento_motivo": depto_msg,
        "departamento_sugerido": dados.get("departamento_sugerido"),
        "funcao_id": funcao_id,
        "funcao_confianca": round(conf, 4),
        "cargo_extraido": dados.get("cargo"),
        "cbo_extraido": dados.get("cbo"),
    }

    # Salva payload antes do POST (preserva mesmo se crashar)
    arq_payload = salvar_payload(msg_id, metadados, payload, resolucao=resolucao)
    log.info(f"   💾 Payload salvo em {arq_payload.relative_to(ROOT)}")

    if config.dry_run:
        log.info("   DRY-RUN — pulando POST")
        salvar_payload(
            msg_id, metadados, payload, resolucao=resolucao,
            resultado={"status": "dry_run", "candidato_id": None, "erro": None},
        )
        log_jsonl({"msg_id": msg_id, "status": "dry_run", "payload_path": str(arq_payload.name)})
        return

    # 7. POST candidato
    ok, ref, body_err = api.post_candidato(payload)
    if ok:
        candidato_id = ref
        log.info(f"   ✅ Candidato {candidato_id} criado")
        # Limpa label pendente de mensagens antigas (caso veio de reprocessamento)
        for mid in ids_label_pendente_remover:
            try:
                gmail.remover_label(mid, config.label_pendente)
            except Exception as e:
                log.warning(f"   Falha removendo pendente de {mid}: {e}")
        gmail.aplicar_label(msg_id, config.label_processado)
        if config.email_dp:
            assunto, corpo_email = email_sucesso(candidato_id, payload, razao)
            gmail.enviar_email(config.email_dp, assunto, corpo_email)
        salvar_payload(
            msg_id, metadados, payload, resolucao=resolucao,
            resultado={"status": "sucesso", "candidato_id": candidato_id, "erro": None},
        )
        log_jsonl({
            "msg_id": msg_id, "status": "sucesso",
            "candidato_id": candidato_id, "empresa_id": empresa_id,
            "departamento_id": depto_id, "funcao_id": funcao_id,
            "payload_path": str(arq_payload.name),
        })
    else:
        log.error(f"   ❌ POST falhou: {ref}\n{body_err}")
        gmail.aplicar_label(msg_id, config.label_pendente)
        if config.email_dp:
            assunto, corpo_email = email_pendencia(
                f"{ref} — {body_err[:200]}",
                {"payload": payload, "empresa": razao},
            )
            gmail.enviar_email(config.email_dp, assunto, corpo_email)
        salvar_payload(
            msg_id, metadados, payload, resolucao=resolucao,
            resultado={"status": "falha_post", "candidato_id": None,
                       "erro": ref, "body": body_err[:2000]},
        )
        log_jsonl({
            "msg_id": msg_id, "status": "falha_post",
            "motivo": ref, "body": body_err[:500],
            "payload_path": str(arq_payload.name),
        })


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
        if msg_pra_resposta and e_e_pendencia:
            try:
                corpo = email_resposta_cliente(
                    payload_parcial, campos_faltando, motivo_livre=motivo_cliente
                )
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

        if not reply_enviado and config.email_dp:
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
    args = parser.parse_args()

    log.info("=" * 70)
    log.info("Pipeline de Admissão Local — Crosara Contabilidade")
    log.info("=" * 70)

    try:
        config = carregar_config()
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as e:
        log.error(f"Erro de configuração: {e}")
        return 1

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
