"""
main.py — Pipeline de admissão automática da Crosara Contabilidade.

Roda DENTRO do Claude Code Routines como helper de I/O. O agente Claude Code
faz a classificação/extração visual dos documentos NATIVAMENTE via tool Read
(Vision built-in) — main.py não chama Anthropic API separadamente.

Arquivos do repositório:
  - config.json          (placeholders; tokens reais vêm de env vars)
  - lookups.json         (enums + defaults_pipeline + workarounds de bugs)
  - departamentos.json   (mapa CNPJ → modo unico/multiplo)
  - CLAUDE.md            (instruções pro agente — fluxo, regras, bugs)

Fluxo orquestrado pelo Claude Code:
  1. `python main.py fetch` → busca emails, baixa anexos pra /tmp/admissao/<msg_id>/
     e imprime JSON com paths + metadados
  2. Claude Code lê cada arquivo com a tool Read (Vision nativo) e extrai campos
  3. `python main.py resolve <cnpj> <cargo> [<depto_hint>]` → resolve empresa/depto/funcao
  4. Claude Code monta o payload (seguindo CLAUDE.md) e grava em /tmp/admissao/<msg_id>/payload.json
  5. `python main.py post /tmp/.../payload.json` → POSTa em /candidatos
  6. `python main.py finalizar <msg_id> sucesso|pendente ...` → label + email DP

Regras críticas (CLAUDE.md + lookups.json):
  - statusadmissao SEMPRE "1" (Análise — verde, desce DIRETO pro Alterdata).
    Validado por 5 admissões reais (Gabrielle, Luiz Felipe, João Pedro, Ingride, RETESTE).
    status=2/5 retêm no eContador (vermelho) — não usar.
  - CPF como integer
  - numero=0 ou ausente: OMITIR
  - Datas nulas: OMITIR campo
  - PIS como string (zeros à esquerda)
  - Telefone/celular: sem hífens, 12-13 chars
  - tipoidentidade RG: id=1 (workaround off-by-one)
  - raca default: id=4 (Parda — UI mostra vazio mas API armazena correto)
  - diascontratoexperiencia: 30 (default)

Dependências:
  pip install httpx python-dotenv google-auth google-auth-oauthlib google-api-python-client google-auth-httplib2
"""

from __future__ import annotations

# Carrega variáveis de ambiente do .env ANTES de qualquer outro import
# que possa depender delas (GmailClient lê GMAIL_TOKEN, EContadorAPI recebe ECONTADOR_TOKEN).
from dotenv import load_dotenv
load_dotenv()

import base64
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import httpx

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import google_auth_httplib2
import httplib2


# ============================================================
# Constantes / Setup
# ============================================================

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).parent
CONFIG_FILE = ROOT / "config.json"
LOOKUPS_FILE = ROOT / "lookups.json"
DEPARTAMENTOS_FILE = ROOT / "departamentos.json"
LOG_FILE = ROOT / "admissao_log.json"

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]

# Match fuzzy de função (CLAUDE.md passo 6)
FUNCAO_CONFIANCA_ALTA = 0.80
FUNCAO_CONFIANCA_DUVIDA = 0.40

# Campos que SEMPRE precisam de preenchimento manual no Desktop
# (limitações de produto + bugs do sync — lookups.json:campos_faltando_no_payload)
CAMPOS_MANUAIS_DP = [
    "Matrícula eSocial",
    "Categoria eSocial (default: 101 Empregado)",
    "Natureza da atividade (default: Trabalhador urbano)",
    "Tipo de jornada",
    "Regime de Jornada (Horário de Trabalho)",
    "Horas semanais (default: 44)",
    "Horário (código — varia por empresa)",
    "Tipo de salário contratual (Mensal + data=admissão)",
    "Adiantamento (☑ marcar)",
    "Não atualiza salário (☑ marcar)",
    "Dias para prorrogação (default: 60)",
    "FGTS (Conta, Data opção=admissão, UF, Saldo)",
]


# ============================================================
# Logging
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("admissao")


def log_jsonl(entry: dict) -> None:
    """Append-only NDJSON log."""
    entry["timestamp"] = datetime.now().isoformat()
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning(f"Falha escrevendo log: {e}")


# ============================================================
# Config / Lookups (formato real dos arquivos)
# ============================================================

@dataclass
class Config:
    base_url: str
    token: str
    label_entrada: str
    label_processado: str
    label_pendente: str
    email_dp: str
    dry_run: bool


def carregar_config() -> Config:
    raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    # Tolerar typo "ecotador" e versão correta "econtador"
    api_cfg = raw.get("ecotador") or raw.get("econtador") or {}
    # Token: usar config.json se preenchido com valor real; senão fallback pra env
    # (config.json:token = "SEU_TOKEN_AQUI" é placeholder, não vale como token real)
    token = api_cfg.get("token")
    if not token or token == "SEU_TOKEN_AQUI":
        token = os.getenv("ECONTADOR_TOKEN")
    if not token:
        raise ValueError(
            "Token eContador ausente — configure ECONTADOR_TOKEN no ambiente "
            "(secret do Routine ou .env local)"
        )
    gmail = raw.get("gmail", {})
    return Config(
        base_url=api_cfg.get("base_url", "https://dp.pack.alterdata.com.br/api/v1"),
        token=token,
        label_entrada=gmail.get("label_entrada", "ADMISSÃO"),
        label_processado=gmail.get("label_processado", "ADMISSÃO/processado"),
        label_pendente=gmail.get("label_pendente", "ADMISSÃO/pendente"),
        email_dp=raw.get("dp", {}).get("email_notificacao", ""),
        dry_run=bool(raw.get("dry_run", False)),
    )


def carregar_lookups() -> dict:
    return json.loads(LOOKUPS_FILE.read_text(encoding="utf-8"))


def carregar_departamentos() -> dict:
    return json.loads(DEPARTAMENTOS_FILE.read_text(encoding="utf-8"))


# ============================================================
# Regras de payload (CLAUDE.md + lookups.json:regras_escritorio)
# ============================================================

CAMPOS_UPPERCASE = {
    "nome", "nomedamae", "nomedopai",
    "rua", "bairro", "cidade", "complemento",
    "municipionascimento", "nomecargo",
    "orgaoemissoridentidade", "orgaoemissorcnh",
    "observacao", "ocorrencia",
}


def aplicar_uppercase(attrs: dict) -> dict:
    for k in CAMPOS_UPPERCASE:
        v = attrs.get(k)
        if isinstance(v, str):
            attrs[k] = v.upper()
    if isinstance(attrs.get("email"), str):
        attrs["email"] = attrs["email"].lower()
    return attrs


def normalizar_telefone(s: str | None) -> str | None:
    """Remove hífens/espaços. Formato (DDD)NNNN... com 12-13 chars."""
    if not s:
        return None
    d = re.sub(r"\D", "", s)
    if len(d) == 10:
        return f"({d[:2]}){d[2:]}"
    if len(d) == 11:
        return f"({d[:2]}){d[2:]}"
    return None


def cpf_para_int(cpf_str: str | int | None) -> int | None:
    """CPF como integer (Java rejeita string)."""
    if cpf_str is None:
        return None
    s = re.sub(r"\D", "", str(cpf_str))
    if not s:
        return None
    return int(s)


def ctps_do_cpf(cpf_str: str) -> tuple[int, str]:
    """Regra escritório: CTPS = int(CPF[:7]), série = CPF[7:11]."""
    d = re.sub(r"\D", "", str(cpf_str)).zfill(11)
    return int(d[:7]), d[7:11]


def adicionar_dias_iso(data_iso: str, dias: int) -> str:
    dt = datetime.fromisoformat(data_iso).date()
    return (dt + timedelta(days=dias)).isoformat()


def limpar_payload(attrs: dict) -> dict:
    """Remove None/''/[]/{} e omite numero=0 (CLAUDE.md)."""
    return {
        k: v for k, v in attrs.items()
        if v not in (None, "", [], {})
        and not (k == "numero" and v in (0, "0"))
    }


def cnpj_limpo(cnpj: str | None) -> str | None:
    if not cnpj:
        return None
    return re.sub(r"\D", "", cnpj)


# ============================================================
# Cliente E-plugin API
# ============================================================

class EContadorAPI:
    def __init__(self, base_url: str, token: str):
        self.base = base_url
        self.client = httpx.Client(
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/vnd.api+json",
                "Accept": "application/vnd.api+json",
            },
            timeout=60.0,
        )

    def close(self):
        self.client.close()

    def resolver_empresa(self, cnpj: str) -> tuple[str | None, dict]:
        cnpj_d = cnpj_limpo(cnpj) or ""
        r = self.client.get(
            f"{self.base}/empresas",
            params={"filter[cpfcnpj]": cnpj_d, "page[limit]": 5},
        )
        if r.status_code != 200:
            log.error(f"GET /empresas filter[cpfcnpj]={cnpj_d}: HTTP {r.status_code}")
            return None, {}
        data = r.json().get("data", [])
        if not data:
            return None, {}
        return str(data[0]["id"]), data[0].get("attributes", {})

    def listar_departamentos_empresa(self, empresa_id: str) -> list[dict]:
        r = self.client.get(
            f"{self.base}/departamentos",
            params={"filter[empresaId]": empresa_id, "page[limit]": 200},
        )
        if r.status_code != 200:
            return []
        return [
            {
                "id": it["id"],
                "nome": it["attributes"].get("nome", "?"),
            }
            for it in r.json().get("data", [])
        ]

    def listar_funcoes(self, limit_total: int = 15000) -> list[dict]:
        """GET /funcoes paginado (limit=100 é cap seguro)."""
        items, offset = [], 0
        while offset < limit_total:
            r = self.client.get(
                f"{self.base}/funcoes",
                params={"page[limit]": 100, "page[offset]": offset},
            )
            if r.status_code != 200:
                break
            data = r.json().get("data", [])
            if not data:
                break
            for it in data:
                a = it["attributes"]
                items.append({
                    "id": it["id"],
                    "nome": a.get("nome", "?"),
                    "cbo": a.get("cbo"),
                })
            if len(data) < 100:
                break
            offset += 100
        return items

    def post_candidato(self, payload: dict) -> tuple[bool, str, str]:
        """Retorna (ok, candidato_id_ou_erro_texto, erro_completo)."""
        r = self.client.post(f"{self.base}/candidatos", json=payload)
        if r.status_code == 201:
            return True, r.json()["data"]["id"], ""
        return False, f"HTTP {r.status_code}", r.text[:2000]


# ============================================================
# Cliente Gmail
# ============================================================

class GmailClient:
    """Cliente Gmail autenticado via variável de ambiente GMAIL_TOKEN.

    GMAIL_TOKEN deve ser um JSON string com os campos:
      token, refresh_token, token_uri, client_id, client_secret, scopes

    Em Claude Code Routines, configurar GMAIL_TOKEN como secret no painel.
    Localmente, exportar a variável ou colocar no .env (que está no .gitignore).
    """

    def __init__(self):
        raw = os.getenv("GMAIL_TOKEN")
        if not raw:
            raise RuntimeError(
                "GMAIL_TOKEN não encontrado no ambiente. "
                "Configure como secret na Routine (ou exporte localmente)."
            )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"GMAIL_TOKEN não é JSON válido: {e}. "
                "Esperado: {{\"token\": ..., \"refresh_token\": ..., \"token_uri\": ..., "
                "\"client_id\": ..., \"client_secret\": ..., \"scopes\": [...]}}"
            )

        creds = Credentials(
            token=data.get("token"),
            refresh_token=data.get("refresh_token"),
            token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=data.get("client_id"),
            client_secret=data.get("client_secret"),
            scopes=data.get("scopes") or GMAIL_SCOPES,
        )

        # SSL: usar bundle de CAs do sistema (corrige erros de cadeia em alguns
        # ambientes de cloud — em particular Routines/Linux containers).
        ca_certs_path = "/etc/ssl/certs/ca-certificates.crt"
        http_args = {"ca_certs": ca_certs_path} if os.path.exists(ca_certs_path) else {}
        http = httplib2.Http(**http_args)

        # Auto-refresh se expirado (passa o http já configurado pra reuso do CA bundle)
        if creds.expired and creds.refresh_token:
            log.info("Token Gmail expirado — fazendo refresh automático...")
            try:
                creds.refresh(Request())
                log.info("Refresh OK")
            except Exception as e:
                raise RuntimeError(
                    f"Falha ao renovar token Gmail: {e}. "
                    "Refresh_token pode estar revogado — regere o GMAIL_TOKEN."
                )

        authed_http = google_auth_httplib2.AuthorizedHttp(creds, http=http)
        self.service = build(
            "gmail", "v1", http=authed_http, cache_discovery=False
        )

    def _label_id(self, nome: str) -> str | None:
        labels = self.service.users().labels().list(userId="me").execute().get("labels", [])
        return next((l["id"] for l in labels if l["name"] == nome), None)

    def criar_label(self, nome: str) -> str:
        lid = self._label_id(nome)
        if lid:
            return lid
        body = {"name": nome, "labelListVisibility": "labelShow", "messageListVisibility": "show"}
        return self.service.users().labels().create(userId="me", body=body).execute()["id"]

    def buscar_emails_pendentes(self, label_entrada: str, processado: str, pendente: str) -> list[dict]:
        lid_proc = self._label_id(processado)
        lid_pend = self._label_id(pendente)
        if not self._label_id(label_entrada):
            log.error(f"Label '{label_entrada}' não existe no Gmail")
            return []
        q_parts = [f'label:"{label_entrada}"']
        if lid_proc:
            q_parts.append(f'-label:"{processado}"')
        if lid_pend:
            q_parts.append(f'-label:"{pendente}"')
        q = " ".join(q_parts)
        res = self.service.users().messages().list(userId="me", q=q, maxResults=50).execute()
        ids = res.get("messages", [])
        msgs = []
        for m in ids:
            msgs.append(
                self.service.users().messages().get(userId="me", id=m["id"]).execute()
            )
        return msgs

    def baixar_anexos(self, msg: dict) -> list[dict]:
        anexos = []
        msg_id = msg["id"]

        def walk(part):
            if "parts" in part:
                for p in part["parts"]:
                    walk(p)
                return
            filename = part.get("filename", "")
            mime = part.get("mimeType", "")
            if not filename or not mime.startswith(("image/", "application/pdf")):
                return
            body = part.get("body", {})
            attach_id = body.get("attachmentId")
            if attach_id:
                att = (
                    self.service.users().messages().attachments()
                    .get(userId="me", messageId=msg_id, id=attach_id).execute()
                )
                data = base64.urlsafe_b64decode(att["data"])
            elif "data" in body:
                data = base64.urlsafe_b64decode(body["data"])
            else:
                return
            anexos.append({"filename": filename, "mime": mime, "data": data})

        walk(msg["payload"])
        return anexos

    def aplicar_label(self, msg_id: str, label_nome: str) -> None:
        lid = self.criar_label(label_nome)
        self.service.users().messages().modify(
            userId="me", id=msg_id, body={"addLabelIds": [lid]}
        ).execute()

    def enviar_email(self, destinatario: str, assunto: str, corpo: str) -> None:
        from email.mime.text import MIMEText
        mime = MIMEText(corpo, "plain", "utf-8")
        mime["to"] = destinatario
        mime["subject"] = assunto
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
        self.service.users().messages().send(userId="me", body={"raw": raw}).execute()


# ============================================================
# Download de anexos pra /tmp (consumido pelo Claude Code Vision nativo)
# ============================================================

import shutil
import tempfile

TMP_BASE = Path(tempfile.gettempdir()) / "admissao"


def baixar_anexos_pra_tmp(gmail: "GmailClient", msg: dict) -> dict:
    """Baixa anexos do email e salva em /tmp/admissao/{msg_id}/.

    Retorna metadata pro Claude Code consumir:
      {
        "msg_id": str,
        "remetente": str,
        "assunto": str,
        "anexos": [{"path": str, "filename": str, "mime": str, "size": int}, ...],
        "tmp_dir": str,
      }
    """
    msg_id = msg["id"]
    tmp_dir = TMP_BASE / msg_id
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # Cabeçalhos básicos
    headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
    remetente = headers.get("from", "")
    assunto = headers.get("subject", "")

    anexos = gmail.baixar_anexos(msg)
    saved = []
    for anexo in anexos:
        safe_name = re.sub(r"[^\w\.\-]", "_", anexo["filename"])
        path = tmp_dir / safe_name
        path.write_bytes(anexo["data"])
        saved.append({
            "path": str(path),
            "filename": anexo["filename"],
            "mime": anexo["mime"],
            "size": len(anexo["data"]),
        })

    return {
        "msg_id": msg_id,
        "remetente": remetente,
        "assunto": assunto,
        "anexos": saved,
        "tmp_dir": str(tmp_dir),
    }


def limpar_tmp_email(msg_id: str) -> None:
    """Remove o diretório temporário de um email já processado."""
    tmp_dir = TMP_BASE / msg_id
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ============================================================
# Resolução de departamento (CLAUDE.md passo 5)
# ============================================================

def resolver_departamento(
    cnpj: str,
    deptos_cfg: dict,
    deptos_api: list[dict],
    departamento_ficha: str | None,
) -> tuple[str | None, str]:
    """Aplica modo unico/multiplo conforme departamentos.json.

    Retorna (departamento_id_ou_None, motivo_pendencia_ou_'ok').
    """
    cnpj_d = cnpj_limpo(cnpj) or ""
    empresa_cfg = (deptos_cfg.get("empresas") or {}).get(cnpj_d)

    if not empresa_cfg:
        return None, f"Empresa CNPJ {cnpj_d} não está em departamentos.json — DP precisa configurar"

    modo = empresa_cfg.get("modo")

    if modo == "unico":
        return empresa_cfg.get("departamento_id"), "ok"

    if modo == "multiplo":
        if not departamento_ficha:
            return None, "Empresa modo multiplo mas ficha não informa departamento"
        ficha_norm = departamento_ficha.strip().lower()
        for d in empresa_cfg.get("departamentos", []):
            variantes = [v.lower() for v in d.get("nome_variantes", [])]
            if ficha_norm in variantes or any(v in ficha_norm for v in variantes):
                return d.get("id"), "ok"
            # Fuzzy fallback
            for v in variantes:
                if SequenceMatcher(None, ficha_norm, v).ratio() > 0.85:
                    return d.get("id"), "ok"
        return None, f"Departamento '{departamento_ficha}' não bate com nenhuma variante configurada"

    return None, f"Modo de departamento desconhecido: {modo!r}"


# ============================================================
# Resolução de função (CLAUDE.md passo 6 — fuzzy >80%/40-80%/<40%)
# ============================================================

def resolver_funcao(funcoes_api: list[dict], cargo_ficha: str | None) -> tuple[str | None, float, str]:
    """Retorna (funcao_id_ou_None, confianca, motivo)."""
    if not cargo_ficha:
        return None, 0.0, "Cargo não informado na ficha"

    q = cargo_ficha.strip().lower()
    tokens = q.split()

    melhor: tuple[float, dict] | None = None
    for f in funcoes_api:
        nome = (f.get("nome") or "").lower()
        if not nome:
            continue
        # Score combinado: similaridade textual + tokens presentes
        sim = SequenceMatcher(None, q, nome).ratio()
        if all(t in nome for t in tokens):
            sim = max(sim, 0.9)
        # Penaliza variantes "Nível I/II/III"
        nome_up = nome.upper()
        if any(s in nome_up for s in [" NIVEL ", " NÍVEL ", " I ", " II", " III"]):
            sim *= 0.85
        if melhor is None or sim > melhor[0]:
            melhor = (sim, f)

    if not melhor:
        return None, 0.0, "Lista de funções vazia"

    confianca, f = melhor
    if confianca >= FUNCAO_CONFIANCA_ALTA:
        return f["id"], confianca, "ok"
    if confianca >= FUNCAO_CONFIANCA_DUVIDA:
        return None, confianca, f"Match com dúvida ({confianca:.0%}): sugestão = {f['nome']} (id={f['id']})"
    return None, confianca, f"Função '{cargo_ficha}' não encontrada (melhor: {f['nome']} {confianca:.0%})"


# ============================================================
# Heurísticas auxiliares (lookups.json:regras_escritorio)
# ============================================================

def heuristica_escolaridade(cargo: str | None, lookups: dict) -> str:
    cfg = (lookups.get("tipos_escolaridade") or {}).get("_heuristica") or {}
    baixos = cfg.get("cargos_baixos", [])
    altos = cfg.get("cargos_altos", [])
    if cargo:
        c = cargo.lower()
        if any(x in c for x in altos):
            return cfg.get("cargo_alto_id", "9")
        if any(x in c for x in baixos):
            return cfg.get("cargo_baixo_id", "7")
    return cfg.get("cargo_baixo_id", "7")


def mapear_estado_civil(texto: str | None, lookups: dict) -> str:
    tipos = lookups.get("tipos_estado_civil", {})
    default = tipos.get("_default_escritorio", "1")
    if not texto:
        return default
    t = texto.lower()
    if "casad" in t: return tipos.get("Casado", "2")
    if "divorciad" in t: return tipos.get("Divorciado", "3")
    if "viúv" in t or "viuv" in t: return tipos.get("Viúvo", "4")
    if "união" in t or "uniao" in t: return tipos.get("União Estável", "5")
    if "solteir" in t: return tipos.get("Solteiro", "1")
    return default


def mapear_sexo(texto: str | None, lookups: dict) -> str | None:
    if not texto:
        return None
    t = texto.lower()
    tipos = lookups.get("tipos_sexo", {})
    if t.startswith("m"): return tipos.get("Masculino", "1")
    if t.startswith("f"): return tipos.get("Feminino", "2")
    return None


def mapear_categoria_cnh(categoria: str | None, lookups: dict) -> str | None:
    if not categoria:
        return None
    wa = (lookups.get("tipos_cnh") or {}).get("_workaround_para_enviar") or {}
    return wa.get(categoria.upper())


def mapear_uf(uf: str | None, lookups: dict) -> str | None:
    if not uf:
        return None
    return (lookups.get("estados") or {}).get(uf.upper())


def mapear_resultado_aso(texto: str | None) -> str:
    """1=Apto, 2=Inapto. Default Apto."""
    if texto and "inapto" in texto.lower():
        return "2"
    return "1"


# ============================================================
# Montagem do payload
# ============================================================

def montar_payload(
    campos: dict,
    empresa_id: str,
    departamento_id: str | None,
    funcao_id: str,
    lookups: dict,
) -> dict:
    defaults = lookups.get("defaults_pipeline", {})

    # ATTRIBUTES
    attrs: dict[str, Any] = {}

    def putif(k, v):
        if v not in (None, "", [], {}):
            attrs[k] = v

    # Identificação
    putif("nome", campos.get("nome") or campos.get("nome_completo"))
    cpf_int = cpf_para_int(campos.get("cpf") or campos.get("numero"))  # CPF doc pode vir como "numero"
    putif("cpf", cpf_int)
    putif("admissao", campos.get("admissao"))
    putif("nascimento", campos.get("nascimento"))
    putif("nomedamae", campos.get("nome_mae") or campos.get("nomedamae"))
    putif("nomedopai", campos.get("nome_pai") or campos.get("nomedopai"))
    putif("municipionascimento", campos.get("municipio_nascimento") or campos.get("municipionascimento"))

    # Contratuais
    putif("nomecargo", campos.get("cargo") or campos.get("nomecargo"))
    if campos.get("salario") is not None:
        try:
            attrs["salario"] = float(str(campos["salario"]).replace(",", "."))
        except (ValueError, TypeError):
            pass

    attrs["primeiroemprego"] = bool(campos.get("primeiro_emprego", defaults.get("primeiroemprego", False)))
    attrs["possuideficiencia"] = bool(campos.get("possui_deficiencia", defaults.get("possuideficiencia", False)))
    attrs["requersegurodesemprego"] = bool(defaults.get("requersegurodesemprego", False))

    attrs["diascontratoexperiencia"] = int(defaults.get("diascontratoexperiencia", 30))
    if attrs.get("admissao"):
        attrs["dataterminocontrato"] = adicionar_dias_iso(attrs["admissao"], attrs["diascontratoexperiencia"])

    putif("dataatestadoocupacional", campos.get("data_aso"))
    attrs["usuariocriacao"] = "PIPELINE-V3-CROSARA"

    # CTPS (regra escritório: gerar do CPF se não veio)
    ctps_num = campos.get("ctps") or campos.get("ctps_numero") or campos.get("numero")  # CTPS doc
    if ctps_num and re.sub(r"\D", "", str(ctps_num)):
        attrs["ctps"] = int(re.sub(r"\D", "", str(ctps_num)))
        putif("seriectps", campos.get("serie") or campos.get("seriectps") or campos.get("ctps_serie"))
        putif("datactps", campos.get("data_emissao") or campos.get("datactps") or campos.get("ctps_data_emissao"))
    elif cpf_int:
        ctps_g, serie_g = ctps_do_cpf(str(cpf_int).zfill(11))
        attrs["ctps"] = ctps_g
        attrs["seriectps"] = serie_g

    # RG
    putif("identidade", campos.get("rg_numero") or campos.get("identidade"))
    putif("dataidentidade", campos.get("rg_data_emissao") or campos.get("dataidentidade"))
    putif("orgaoemissoridentidade", campos.get("rg_orgao_emissor") or campos.get("orgaoemissoridentidade"))

    # CNH (omitir tudo se sem)
    cnh_num = campos.get("cnh_numero") or campos.get("cnh")
    if cnh_num:
        putif("cnh", str(cnh_num))
        putif("emissaocnh", campos.get("cnh_data_emissao") or campos.get("emissaocnh"))
        putif("validadecnh", campos.get("cnh_validade") or campos.get("validadecnh"))
        putif("primeiraemissaocnh", campos.get("cnh_primeira_emissao") or campos.get("primeiraemissaocnh"))
        putif("orgaoemissorcnh", campos.get("cnh_orgao_emissor") or campos.get("orgaoemissorcnh"))

    # Endereço
    putif("cep", re.sub(r"\D", "", str(campos.get("cep") or "")) or None)
    putif("rua", campos.get("rua"))
    # numero: OMITIR se 0/ausente (CLAUDE.md)
    num = campos.get("numero_endereco") or campos.get("numero")
    if num is not None and str(num).strip() not in ("", "0", "0.0"):
        try:
            n = int(re.sub(r"\D", "", str(num)))
            if n > 0:
                attrs["numero"] = n
        except ValueError:
            pass
    putif("complemento", campos.get("complemento"))
    putif("bairro", campos.get("bairro"))
    putif("cidade", campos.get("cidade"))

    # Contato (sem hífens, 12-13 chars)
    tel = normalizar_telefone(campos.get("telefone"))
    if tel: attrs["telefone"] = tel
    cel = normalizar_telefone(campos.get("celular"))
    if cel: attrs["celular"] = cel
    putif("email", campos.get("email"))

    # PIS (string com zeros à esquerda — só se NÃO primeiroemprego)
    if not attrs["primeiroemprego"]:
        pis = campos.get("pis_numero") or campos.get("pis")
        if pis:
            attrs["pis"] = re.sub(r"\D", "", str(pis))
            putif("datapis", campos.get("pis_data_emissao"))

    # Título eleitoral (opcional)
    titulo = campos.get("titulo_numero") or campos.get("tituloeleitor")
    if titulo:
        try:
            attrs["tituloeleitor"] = int(re.sub(r"\D", "", str(titulo)))
        except ValueError:
            pass
        putif("zonatituloeleitor", str(campos.get("titulo_zona") or campos.get("zonatituloeleitor") or "") or None)
        putif("secaotituloeleitor", str(campos.get("titulo_secao") or campos.get("secaotituloeleitor") or "") or None)

    # Bancário (tudo-ou-nada)
    banco = campos.get("banco")
    agencia = campos.get("agencia")
    conta = campos.get("conta")
    if banco and agencia and conta:
        attrs["banco"] = re.sub(r"\D", "", str(banco)).zfill(3)[:3]
        attrs["agencia"] = re.sub(r"\D", "", str(agencia))
        attrs["conta"] = str(conta)

    attrs = aplicar_uppercase(attrs)
    attrs = limpar_payload(attrs)

    # RELATIONSHIPS
    rels: dict[str, dict] = {
        "empresa": {"data": {"type": "empresas", "id": str(empresa_id)}},
        "funcao": {"data": {"type": "funcoes", "id": str(funcao_id)}},
        "statusadmissao": {"data": {
            "type": "tipos-status-admissao",
            "id": defaults.get("statusadmissao_id", "1"),  # SEMPRE 1 — Análise/verde (validado por 5 admissões reais)
        }},
        "tipoadmissao": {"data": {
            "type": "tipos-admissao",
            "id": defaults.get("tipoadmissao_id", "1"),
        }},
        "tipovinculotrabalhista": {"data": {
            "type": "tipos-vinculos-trabalhista",
            "id": defaults.get("tipovinculotrabalhista_id", "10"),
        }},
        "categoriawdp": {"data": {
            "type": "tipos-categoria",
            "id": defaults.get("categoriawdp_id", "1"),
        }},
        "formapagamento": {"data": {
            "type": "tipos-forma-de-pagamento",
            "id": defaults.get("formapagamento_id", "4"),
        }},
        "tipoidentidade": {"data": {
            "type": "tipos-identidade",
            "id": defaults.get("tipoidentidade_id_workaround", "1"),  # off-by-one workaround
        }},
        "raca": {"data": {
            "type": "tipos-raca",
            "id": defaults.get("raca_id", "4"),
        }},
        "tipoDeDeficiencia": {"data": {"type": "tipos-deficiencia", "id": "0"}},
        "statusatestadoocupacional": {"data": {
            "type": "tipos-status-atestado-ocupacional",
            "id": mapear_resultado_aso(campos.get("resultado")),
        }},
    }

    if departamento_id:
        rels["departamento"] = {"data": {"type": "departamentos", "id": str(departamento_id)}}

    # Pessoais
    sexo_id = mapear_sexo(campos.get("sexo"), lookups)
    if sexo_id:
        rels["sexo"] = {"data": {"type": "tipos-sexo", "id": sexo_id}}

    rels["estadocivil"] = {"data": {
        "type": "tipos-estado-civil",
        "id": mapear_estado_civil(campos.get("estado_civil"), lookups),
    }}

    rels["escolaridade"] = {"data": {
        "type": "tipos-escolaridade",
        "id": heuristica_escolaridade(campos.get("cargo") or campos.get("nomecargo"), lookups),
    }}

    # Localização
    uf_endereco = mapear_uf(campos.get("uf") or campos.get("uf_endereco"), lookups)
    uf_natural = mapear_uf(campos.get("uf_nascimento"), lookups)
    estado_default = (lookups.get("estados") or {}).get("GO", "9")  # default Goiás

    rels["estado"] = {"data": {"type": "estados", "id": uf_endereco or estado_default}}
    rels["naturalidade"] = {"data": {"type": "estados", "id": uf_natural or estado_default}}

    uf_rg = mapear_uf(campos.get("rg_uf") or campos.get("uf"), lookups) or uf_endereco or estado_default
    rels["ufidentidade"] = {"data": {"type": "estados", "id": uf_rg}}

    uf_ctps = mapear_uf(campos.get("ctps_uf") or campos.get("uf"), lookups) or uf_rg
    rels["ufctps"] = {"data": {"type": "estados", "id": uf_ctps}}

    if attrs.get("cnh"):
        uf_cnh = mapear_uf(campos.get("cnh_uf"), lookups) or uf_rg
        rels["ufcnh"] = {"data": {"type": "estados", "id": uf_cnh}}
        cat_cnh = mapear_categoria_cnh(campos.get("cnh_categoria") or campos.get("categoria"), lookups)
        if cat_cnh:
            rels["categoriacnh"] = {"data": {"type": "tipos-cnh", "id": cat_cnh}}

    # País
    pais_id = defaults.get("pais_id", "105")
    rels["nacionalidade"] = {"data": {"type": "paises", "id": pais_id}}
    rels["paisnascimento"] = {"data": {"type": "paises", "id": pais_id}}
    rels["pais"] = {"data": {"type": "paises", "id": pais_id}}

    # Tipo conta (só se tem dados bancários)
    if attrs.get("banco"):
        tc = (lookups.get("tipos_de_conta") or {}).get(
            (campos.get("tipo_conta") or "Conta Salário").title(),
            defaults.get("tipoconta_id", "3"),
        )
        rels["tipoconta"] = {"data": {"type": "tipos-de-conta", "id": str(tc)}}

    return {"data": {"type": "candidatos", "attributes": attrs, "relationships": rels}}


# ============================================================
# Emails de notificação (passo 8)
# ============================================================

def email_sucesso(
    candidato_id: str, payload: dict, empresa_attrs: dict,
    campos_faltantes_extracao: list[str],
) -> tuple[str, str]:
    attrs = payload["data"]["attributes"]
    nome = attrs.get("nome", "?")
    empresa_nome = empresa_attrs.get("nome", "?")
    assunto = f"[ADMISSÃO OK] {nome} — candidato {candidato_id}"
    faltantes_extracao = "\n".join(f"  - {c}" for c in campos_faltantes_extracao) if campos_faltantes_extracao else "  (nenhum)"
    manuais = "\n".join(f"  - {c}" for c in CAMPOS_MANUAIS_DP)
    corpo = (
        f"Admissão criada no eContador com sucesso.\n\n"
        f"  Nome:        {nome}\n"
        f"  CPF:         {attrs.get('cpf', '?')}\n"
        f"  Empresa:     {empresa_nome}\n"
        f"  Admissão:    {attrs.get('admissao', '?')}\n"
        f"  Candidato:   {candidato_id}\n\n"
        f"---\n"
        f"Campos NÃO encontrados nos documentos (completar manual no eContador):\n"
        f"{faltantes_extracao}\n\n"
        f"---\n"
        f"Campos que precisam preenchimento manual no Alterdata Desktop\n"
        f"(limitações de produto / bugs do sync E-plugin):\n"
        f"{manuais}\n\n"
        f"Pipeline V3 — {datetime.now().isoformat(timespec='seconds')}"
    )
    return assunto, corpo


def email_pendencia(motivo: str, dados: dict) -> tuple[str, str]:
    assunto = f"[ADMISSÃO PENDENTE] {motivo[:80]}"
    dados_str = json.dumps(dados, ensure_ascii=False, indent=2)[:3000]
    corpo = (
        f"O pipeline não conseguiu processar essa admissão automaticamente.\n\n"
        f"Motivo: {motivo}\n\n"
        f"---\n"
        f"Dados já extraídos dos anexos:\n{dados_str}\n\n"
        f"Revise e complete manualmente no eContador.\n\n"
        f"Pipeline V3 — {datetime.now().isoformat(timespec='seconds')}"
    )
    return assunto, corpo


# ============================================================
# Subcomandos CLI (consumidos pelo orquestrador Claude Code)
# ============================================================
#
# Fluxo:
#   1. `python main.py fetch`
#        → busca emails ADMISSÃO sem labels processado/pendente,
#          baixa anexos pra /tmp/admissao/<msg_id>/,
#          imprime JSON em stdout com paths + metadados.
#        Claude Code então lê cada arquivo via tool Read (Vision nativo),
#        classifica e extrai os campos.
#
#   2. `python main.py resolve <cnpj> <cargo> [<depto_hint>]`
#        → resolve empresa/departamento/funcao IDs.
#        Imprime JSON: {empresa_id, empresa_attrs, depto_id, funcao_id, confianca, ...}.
#
#   3. `python main.py post <payload.json>`
#        → POSTa payload em /candidatos.
#        Imprime JSON: {ok: bool, candidato_id|erro, body_erro}.
#
#   4. `python main.py finalizar <msg_id> <sucesso|pendente> [--candidato ID] [--motivo "texto"] [--dados-json file]`
#        → aplica label e envia email pro DP.
#
# Todos os comandos imprimem JSON em stdout (orquestrador consome).
# Logs operacionais vão pro stderr (não interferem no JSON).


def _stderr_logging() -> None:
    """Move logging pra stderr — stdout fica reservado pro JSON do subcomando."""
    for h in logging.getLogger().handlers:
        try:
            h.stream = sys.stderr  # type: ignore[attr-defined]
        except AttributeError:
            pass


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def cmd_fetch() -> int:
    """Baixa anexos dos emails pendentes e imprime metadados pro Claude Code."""
    _stderr_logging()
    config = carregar_config()
    gmail = GmailClient()

    emails = gmail.buscar_emails_pendentes(
        config.label_entrada, config.label_processado, config.label_pendente
    )
    log.info(f"📥 {len(emails)} email(s) pendente(s)")

    resultado = []
    for msg in emails:
        try:
            info = baixar_anexos_pra_tmp(gmail, msg)
            resultado.append(info)
            log.info(f"   {msg['id']}: {len(info['anexos'])} anexo(s) → {info['tmp_dir']}")
        except Exception as e:
            log.exception(f"   {msg['id']}: falha no download — {e}")
            resultado.append({"msg_id": msg["id"], "erro": str(e)})

    _print_json({"emails": resultado, "total": len(resultado)})
    return 0


def cmd_resolve(args: list[str]) -> int:
    """resolve <cnpj> <cargo> [<depto_hint>]"""
    _stderr_logging()
    if len(args) < 2:
        print("uso: main.py resolve <cnpj> <cargo> [<depto_hint>]", file=sys.stderr)
        return 64

    cnpj = args[0]
    cargo = args[1]
    depto_hint = args[2] if len(args) >= 3 else None

    config = carregar_config()
    lookups = carregar_lookups()
    departamentos_cfg = carregar_departamentos()
    api = EContadorAPI(config.base_url, config.token)

    try:
        empresa_id, empresa_attrs = api.resolver_empresa(cnpj)
        if not empresa_id:
            _print_json({"ok": False, "erro": f"CNPJ {cnpj} não encontrado em /empresas"})
            return 1

        deptos_api = api.listar_departamentos_empresa(empresa_id)
        depto_id, depto_msg = resolver_departamento(cnpj, departamentos_cfg, deptos_api, depto_hint)

        log.info("Carregando /funcoes (~9k itens, ~15s)...")
        funcoes = api.listar_funcoes()
        funcao_id, confianca, fmsg = resolver_funcao(funcoes, cargo)

        _print_json({
            "ok": True,
            "empresa": {"id": empresa_id, "attrs": empresa_attrs},
            "departamento": {"id": depto_id, "msg": depto_msg},
            "funcao": {"id": funcao_id, "confianca": confianca, "msg": fmsg},
        })
        return 0
    finally:
        api.close()


def cmd_post(args: list[str]) -> int:
    """post <payload.json>"""
    _stderr_logging()
    if not args:
        print("uso: main.py post <payload.json>", file=sys.stderr)
        return 64

    payload_path = Path(args[0])
    payload = json.loads(payload_path.read_text(encoding="utf-8"))

    config = carregar_config()
    if config.dry_run:
        log.warning("⚠ DRY-RUN ATIVO — pulando POST")
        _print_json({"ok": False, "dry_run": True, "payload_attrs": list(payload["data"]["attributes"].keys())})
        return 0

    api = EContadorAPI(config.base_url, config.token)
    try:
        ok, ref, body_err = api.post_candidato(payload)
        if ok:
            _print_json({"ok": True, "candidato_id": ref})
            log_jsonl({"status": "sucesso", "candidato_id": ref})
        else:
            _print_json({"ok": False, "erro": ref, "body": body_err})
            log_jsonl({"status": "falha_post", "motivo": ref, "body": body_err[:500]})
        return 0 if ok else 1
    finally:
        api.close()


def cmd_finalizar(args: list[str]) -> int:
    """finalizar <msg_id> <sucesso|pendente> [--candidato ID] [--motivo TEXTO]
                  [--empresa-nome NOME] [--dados-json FILE] [--nao-extraidos a,b,c]
                  [--payload-json FILE]"""
    _stderr_logging()
    if len(args) < 2:
        print(
            "uso: main.py finalizar <msg_id> <sucesso|pendente> [opts]",
            file=sys.stderr,
        )
        return 64

    msg_id = args[0]
    status = args[1]
    if status not in ("sucesso", "pendente"):
        print(f"status inválido: {status} (use sucesso|pendente)", file=sys.stderr)
        return 64

    # Parse opts simples
    opts: dict[str, str] = {}
    i = 2
    while i < len(args):
        if args[i].startswith("--") and i + 1 < len(args):
            opts[args[i][2:]] = args[i + 1]
            i += 2
        else:
            i += 1

    config = carregar_config()
    gmail = GmailClient()

    if status == "sucesso":
        candidato_id = opts.get("candidato", "?")
        payload = (
            json.loads(Path(opts["payload-json"]).read_text(encoding="utf-8"))
            if "payload-json" in opts else {"data": {"attributes": {}}}
        )
        empresa_attrs = {"nome": opts.get("empresa-nome", "?")}
        nao_extraidos = opts.get("nao-extraidos", "").split(",") if opts.get("nao-extraidos") else []

        gmail.aplicar_label(msg_id, config.label_processado)
        if config.email_dp:
            assunto, corpo = email_sucesso(candidato_id, payload, empresa_attrs, nao_extraidos)
            gmail.enviar_email(config.email_dp, assunto, corpo)
        limpar_tmp_email(msg_id)
        log_jsonl({"msg_id": msg_id, "status": "sucesso", "candidato_id": candidato_id})
        _print_json({"ok": True, "ação": "label processado + email DP"})
    else:
        motivo = opts.get("motivo", "Pendência não especificada")
        dados = (
            json.loads(Path(opts["dados-json"]).read_text(encoding="utf-8"))
            if "dados-json" in opts else {}
        )
        gmail.aplicar_label(msg_id, config.label_pendente)
        if config.email_dp:
            assunto, corpo = email_pendencia(motivo, dados)
            gmail.enviar_email(config.email_dp, assunto, corpo)
        log_jsonl({"msg_id": msg_id, "status": "pendente", "motivo": motivo})
        _print_json({"ok": True, "ação": "label pendente + email DP"})

    return 0


def cmd_montar_payload(args: list[str]) -> int:
    """montar-payload <campos.json> <empresa_id> <funcao_id> [<depto_id>]

    Helper: lê campos extraídos pelo Claude Code, gera o payload completo
    aplicando todas as regras de escritório (defaults, workarounds, CTPS-do-CPF
    etc.) e imprime o JSON pra ser passado ao `post`."""
    _stderr_logging()
    if len(args) < 3:
        print(
            "uso: main.py montar-payload <campos.json> <empresa_id> <funcao_id> [<depto_id>]",
            file=sys.stderr,
        )
        return 64
    campos = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    empresa_id = args[1]
    funcao_id = args[2]
    depto_id = args[3] if len(args) >= 4 else None

    lookups = carregar_lookups()
    payload = montar_payload(campos, empresa_id, depto_id, funcao_id, lookups)
    _print_json(payload)
    return 0


# ============================================================
# Entrypoint
# ============================================================

USAGE = """Pipeline Crosara — Admissão Automática V3

uso: python main.py <comando> [args]

Comandos:
  fetch
      Busca emails ADMISSÃO pendentes, baixa anexos pra /tmp/admissao/<msg_id>/
      e imprime JSON com paths/metadados (consumido pelo Claude Code Vision).

  resolve <cnpj> <cargo> [<depto_hint>]
      Resolve empresa/departamento/função no eContador.

  montar-payload <campos.json> <empresa_id> <funcao_id> [<depto_id>]
      Aplica regras de escritório (defaults, workarounds, CTPS-do-CPF) e
      imprime o payload JSON:API pronto pra POSTar.

  post <payload.json>
      POSTa payload em /candidatos. Imprime ok/candidato_id ou erro.

  finalizar <msg_id> <sucesso|pendente> [opts]
      Aplica label no Gmail e envia email pro DP.
      Sucesso:   --candidato ID --empresa-nome NOME --payload-json FILE
                 --nao-extraidos a,b,c
      Pendente:  --motivo "texto" --dados-json FILE
"""


def main() -> int:
    if len(sys.argv) < 2:
        print(USAGE, file=sys.stderr)
        return 64

    cmd = sys.argv[1]
    args = sys.argv[2:]

    try:
        if cmd == "fetch":
            return cmd_fetch()
        if cmd == "resolve":
            return cmd_resolve(args)
        if cmd == "montar-payload":
            return cmd_montar_payload(args)
        if cmd == "post":
            return cmd_post(args)
        if cmd == "finalizar":
            return cmd_finalizar(args)
        if cmd in ("-h", "--help", "help"):
            print(USAGE)
            return 0
        print(f"Comando desconhecido: {cmd}\n\n{USAGE}", file=sys.stderr)
        return 64
    except Exception as e:
        log.exception(f"Erro fatal em '{cmd}': {e}")
        _print_json({"ok": False, "erro": str(e)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
