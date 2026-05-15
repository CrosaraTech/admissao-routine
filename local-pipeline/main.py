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
from payload_builder import extrair_dados_consulta, finalizar_payload


# ============================================================
# Setup
# ============================================================

ROOT = Path(__file__).parent
CONFIG_FILE = ROOT / "config.json"
LOOKUPS_FILE = ROOT / "lookups.json"
DEPARTAMENTOS_FILE = ROOT / "departamentos.json"
PLANILHA_CBO = ROOT / "funcoes_cbo.xlsx"
LOG_FILE = ROOT / "admissao_log.ndjson"


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
    entry["timestamp"] = datetime.now().isoformat()
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning(f"Falha escrevendo log: {e}")


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
    msg_id = msg["id"]
    metadados = gmail.extrair_metadados(msg)
    log.info(f"📧 {msg_id} | {metadados.get('assunto', '')[:60]}")

    # 1. Corpo + anexos
    corpo = gmail.extrair_corpo(msg)
    anexos = gmail.baixar_anexos(msg)
    log.info(f"   Corpo: {len(corpo)} chars | Anexos: {len(anexos)}")

    if not corpo and not anexos:
        raise ValueError("Email sem corpo nem anexos PDF/imagem")

    # 2. Claude extrai os campos e devolve payload mais o cnpj/departamento sugeridos
    resposta_claude = claude.gerar_payload(corpo, metadados, anexos)
    dados = extrair_dados_consulta(resposta_claude)

    if dados["pendente"]:
        raise ValueError(f"Claude marcou como pendente: {dados['motivo_pendencia']}")

    cnpj = dados["cnpj_empresa"]
    if not cnpj:
        raise ValueError("Claude não extraiu o CNPJ da empresa")

    # 3. Empresa
    empresa_id, empresa_attrs = api.resolver_empresa(cnpj)
    if not empresa_id:
        raise ValueError(f"CNPJ {cnpj} não encontrado em /empresas")
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
            raise ValueError(f"Função ainda ambígua após re-prompt: {fmsg2}")
    elif funcao_id is None:
        raise ValueError(f"Função: {fmsg}")
    log.info(f"   💼 Função: {funcao_id} ({conf:.0%})")

    # 6. Payload final
    payload = finalizar_payload(resposta_claude, empresa_id, depto_id, funcao_id)
    log.info(
        f"   📦 Payload: {len(payload['data']['attributes'])} attrs + "
        f"{len(payload['data']['relationships'])} rels"
    )

    if config.dry_run:
        log.info("   DRY-RUN — pulando POST")
        log_jsonl({"msg_id": msg_id, "status": "dry_run", "payload": payload})
        return

    # 7. POST candidato
    ok, ref, body_err = api.post_candidato(payload)
    if ok:
        candidato_id = ref
        log.info(f"   ✅ Candidato {candidato_id} criado")
        gmail.aplicar_label(msg_id, config.label_processado)
        if config.email_dp:
            assunto, corpo_email = email_sucesso(candidato_id, payload, razao)
            gmail.enviar_email(config.email_dp, assunto, corpo_email)
        log_jsonl({
            "msg_id": msg_id, "status": "sucesso",
            "candidato_id": candidato_id, "empresa_id": empresa_id,
            "departamento_id": depto_id, "funcao_id": funcao_id,
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
        log_jsonl({
            "msg_id": msg_id, "status": "falha_post",
            "motivo": ref, "body": body_err[:500],
        })


# ============================================================
# Loop principal
# ============================================================

def rodar_uma_passada(config: Config, claude: ClaudeClient, planilha: list[dict]) -> None:
    gmail = GmailClient()
    api = EContadorAPI(config.base_url, config.token)
    try:
        emails = gmail.buscar_emails_pendentes(
            config.label_entrada, config.label_processado, config.label_pendente
        )
        log.info(f"📥 {len(emails)} email(s) pendente(s)")

        for msg in emails:
            try:
                processar_email(msg, gmail, claude, api, planilha, config)
            except Exception as e:
                log.exception(f"❌ {msg['id']}: {e}")
                try:
                    gmail.aplicar_label(msg["id"], config.label_pendente)
                    if config.email_dp:
                        assunto, corpo = email_pendencia(str(e), {})
                        gmail.enviar_email(config.email_dp, assunto, corpo)
                except Exception:
                    log.exception("Falha também ao notificar pendência")
                log_jsonl({"msg_id": msg["id"], "status": "erro", "erro": str(e)})
    finally:
        api.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Pipeline de admissão local da Crosara")
    parser.add_argument("--once", action="store_true", help="Roda uma única passada e sai")
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

    if args.once:
        rodar_uma_passada(config, claude, planilha)
        return 0

    log.info(f"⏱  Polling a cada {config.intervalo}s — Ctrl+C pra parar")
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
