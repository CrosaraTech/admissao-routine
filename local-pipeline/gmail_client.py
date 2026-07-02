"""Cliente Gmail — autenticação via env var GMAIL_TOKEN.

Reproduz o padrão usado em ../main.py mas focado em operações que o pipeline
local precisa: buscar emails pendentes, extrair corpo (texto) + anexos
(PDF/PNG/JPG em bytes brutos), aplicar labels e enviar email pro DP.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import mimetypes
import os
import re
import sys
import unicodedata
import zipfile
from email.mime.text import MIMEText
from pathlib import Path
from typing import Iterable

try:
    import rarfile  # type: ignore
    _HAS_RARFILE = True
except ImportError:
    _HAS_RARFILE = False


# Regex pra extrair endereço de email puro de "Nome <email@host>" ou direto
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _extrair_email(s: str | None) -> str | None:
    """Extrai endereço puro de uma string `From`/`To`. None se inválido."""
    if not s:
        return None
    m = _EMAIL_RE.search(s)
    return m.group(0) if m else None

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import google_auth_httplib2
import httplib2


log = logging.getLogger("admissao.gmail")


# Label aplicada em TODA mensagem enviada pelo bot (resposta no thread).
# Usada pra distinguir o que o bot escreveu do que o colaborador escreveu —
# detecção por `From:` falha quando o colaborador usa a mesma conta Gmail.
LABEL_BOT_ENVIADO = "Bot-Crosara/Enviado"


GMAIL_SCOPES_DEFAULT = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]


# Mimes que o Claude consegue ler via Vision
ANEXO_MIMES_OK = ("image/png", "image/jpeg", "image/jpg",
                  "image/webp", "image/gif", "application/pdf")

# Extensoes que sao arquivos compactados — descompactamos antes de mandar
EXT_COMPACTADO = (".zip", ".rar")

# MIMEs de PDF — variações vistas em clientes reais (Gmail, Outlook, Adobe,
# webmails antigos). NÃO inclui octet-stream/binary/vazio — esses passam pelo
# fallback de extensão abaixo.
PDF_MIMES = frozenset({
    "application/pdf",
    "application/x-pdf",
    "application/acrobat",
    "application/vnd.pdf",
    "text/pdf",
    "text/x-pdf",
})

# MIMEs genéricos — Outlook/sistemas internos mandam PDF/imagem assim quando
# não conseguem detectar o tipo. Pra esses, confia na extensão do filename.
MIMES_GENERICOS = frozenset({
    "application/octet-stream",
    "binary/octet-stream",
    "application/binary",
    "",
})

# Extensões confiáveis pra fallback quando MIME é genérico. APENAS formatos
# suportados nativamente pela Anthropic Vision API (espelha EXT_TO_MIME_CANONICO
# em claude_client.py). Aceitar aqui formatos que Claude rejeitaria depois só
# polui o pipeline — preferimos rejeitar cedo com mensagem clara.
#
# Formatos REMOVIDOS: .bmp .tif .tiff .heic .heif — Anthropic não suporta.
# Se aparecer caso real (iPhone HEIC), opções futuras:
#   (a) Converter pra PNG via Pillow antes de enviar (custo: dependência extra)
#   (b) Pedir ao remetente pra reenviar como JPG/PDF
EXTENSOES_CONFIAVEIS = (
    ".pdf",
    ".jpg", ".jpeg", ".png", ".gif", ".webp",
)


def _configurar_unrar() -> None:
    """Aponta rarfile.UNRAR_TOOL pro unrar.exe da pasta do projeto.

    Procura nesta ordem:
      1. `unrar.exe` ao lado do .py (dev) ou do .exe empacotado (PyInstaller)
      2. PATH do sistema (fallback)

    Sem unrar disponivel, rarfile.RarFile() vai lancar RarExecError no extract;
    capturado em _extrair_compactado e tratado como "arquivo nao-extraivel".
    """
    if not _HAS_RARFILE:
        return
    candidatos: list[Path] = []
    # Bundle do PyInstaller (--onefile descompacta em _MEIPASS)
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidatos.append(Path(base) / "unrar.exe")
    # Pasta do script/.exe rodando
    if getattr(sys, "frozen", False):
        candidatos.append(Path(sys.executable).parent / "unrar.exe")
    else:
        candidatos.append(Path(__file__).parent / "unrar.exe")
    for p in candidatos:
        if p.exists():
            rarfile.UNRAR_TOOL = str(p)
            log.debug(f"unrar configurado em {p}")
            return
    # Fallback: deixa o rarfile procurar no PATH


_configurar_unrar()


def _mime_por_extensao(filename: str) -> str:
    """Retorna mime guess pra um arquivo extraido de compactado."""
    guess, _ = mimetypes.guess_type(filename)
    return guess or "application/octet-stream"


def _extrair_compactado(filename: str, data: bytes) -> list[dict]:
    """Tenta extrair PDFs/imagens de um .zip ou .rar em memoria.

    Retorna lista de anexos no mesmo formato que `baixar_anexos`:
        [{filename, mime, data, grupo}]

    O campo `grupo` eh o nome do compactado sem extensao (ex:
    "JENNIFFY CAROLINA LEAO DE OLIVEIRA" pra um JENNIFFY...rar). Permite ao
    pipeline dividir requests grandes ao Claude por funcionario.

    Falhas (formato corrompido, .rar sem unrar.exe disponivel, senha) → lista
    vazia + log warning. O caller decide o que fazer (geralmente vai cair em
    "sem anexos validos" e gerar pendencia pro cliente reenviar).
    """
    fname_lower = filename.lower()
    grupo = Path(filename).stem  # nome do .rar/.zip sem extensao = funcionario
    out: list[dict] = []

    try:
        if fname_lower.endswith(".zip"):
            arq_iter = _iter_zip(data)
        elif fname_lower.endswith(".rar"):
            if not _HAS_RARFILE:
                log.warning(f"   ⚠ '{filename}' eh .rar mas rarfile nao esta instalado")
                return []
            arq_iter = _iter_rar(data)
        else:
            return []

        for membro_nome, membro_data in arq_iter:
            membro_lower = membro_nome.lower()
            # Ignora arquivos do sistema (macOS resource forks, Thumbs.db, etc.)
            if "__MACOSX" in membro_nome or membro_lower.endswith((".ds_store", "thumbs.db")):
                continue
            mime = _mime_por_extensao(membro_nome)
            if mime not in ANEXO_MIMES_OK:
                log.info(f"     -> ignorado '{membro_nome}' [{mime}]")
                continue
            # Nome flat (sem subpastas) — facilita debug
            nome_flat = Path(membro_nome).name
            out.append({"filename": nome_flat, "mime": mime,
                        "data": membro_data, "grupo": grupo})
            log.info(f"     -> extraido '{nome_flat}' [{mime}] ({len(membro_data)} bytes)")
    except (zipfile.BadZipFile, Exception) as e:
        log.warning(f"   ⚠ Falha extraindo '{filename}': {type(e).__name__}: {e}")
        return []

    if not out:
        log.warning(f"   ⚠ '{filename}' descompactado mas sem PDFs/imagens dentro")
    return out


def _iter_zip(data: bytes):
    """Itera (nome, bytes) de cada arquivo dentro de um .zip em memoria."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            yield info.filename, zf.read(info.filename)


def _iter_rar(data: bytes):
    """Itera (nome, bytes) de cada arquivo dentro de um .rar em memoria.

    `rarfile` aceita BytesIO desde 4.0; precisa do binario unrar.exe no PATH
    (ou apontado via rarfile.UNRAR_TOOL — feito em _configurar_unrar).
    """
    with rarfile.RarFile(io.BytesIO(data)) as rf:
        for info in rf.infolist():
            if info.isdir():
                continue
            yield info.filename, rf.read(info.filename)


class GmailClient:
    """Cliente Gmail autenticado via GMAIL_TOKEN (JSON serializado em env)."""

    def __init__(self):
        raw = os.getenv("GMAIL_TOKEN")
        if not raw:
            raise RuntimeError(
                "GMAIL_TOKEN não encontrado no ambiente. Configure no .env "
                "ou exporte a variável antes de rodar."
            )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"GMAIL_TOKEN não é JSON válido: {e}")

        creds = Credentials(
            token=data.get("token"),
            refresh_token=data.get("refresh_token"),
            token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=data.get("client_id"),
            client_secret=data.get("client_secret"),
            scopes=data.get("scopes") or GMAIL_SCOPES_DEFAULT,
        )

        # SSL: usa bundle CA do sistema se existir (corrige cadeia em
        # ambientes Linux). Em Windows o httplib2 usa o bundle embutido.
        ca_certs_path = "/etc/ssl/certs/ca-certificates.crt"
        http_args = {"ca_certs": ca_certs_path} if os.path.exists(ca_certs_path) else {}
        http = httplib2.Http(**http_args)

        if creds.expired and creds.refresh_token:
            log.info("Token Gmail expirado — fazendo refresh automático...")
            creds.refresh(Request())

        authed_http = google_auth_httplib2.AuthorizedHttp(creds, http=http)
        self.service = build("gmail", "v1", http=authed_http, cache_discovery=False)
        self._meu_email: str | None = None

    # ---- Identidade do bot ---------------------------------------

    def meu_email(self) -> str:
        """Endereço da conta Gmail autenticada. Cacheado."""
        if self._meu_email is None:
            profile = self.service.users().getProfile(userId="me").execute()
            self._meu_email = (profile.get("emailAddress") or "").lower()
        return self._meu_email

    # ---- Labels --------------------------------------------------

    @staticmethod
    def _normalizar_nome_label(s: str) -> str:
        """NFC + strip — tira diferenças sutis tipo 'ã' NFD vs NFC, espaços."""
        return unicodedata.normalize("NFC", (s or "").strip())

    def _labels_cache(self) -> list[dict]:
        return (
            self.service.users().labels().list(userId="me").execute().get("labels", [])
        )

    def _label_id(self, nome: str) -> str | None:
        """Resolve ID de label por nome.

        Robusto a:
          - Normalização Unicode (NFC vs NFD — 'ã' decomposto)
          - Espaços em branco nas pontas
          - Diferenças de case ("Processado" vs "processado")
        """
        alvo = self._normalizar_nome_label(nome)
        alvo_lower = alvo.lower()
        labels = self._labels_cache()

        # 1ª tentativa: exato (após NFC + strip)
        for l in labels:
            if self._normalizar_nome_label(l.get("name", "")) == alvo:
                return l["id"]
        # 2ª tentativa: case-insensitive
        for l in labels:
            if self._normalizar_nome_label(l.get("name", "")).lower() == alvo_lower:
                return l["id"]
        return None

    def _msg_tem_label(self, msg: dict, nome_label: str) -> bool:
        """Checa se uma msg tem uma label, robusto a falha no _label_id.

        Resolve o ID via lookup robusto; se mesmo assim não achar, ainda
        compara `labelIds` da mensagem contra todos os IDs cujo nome bate.
        """
        labels_msg = set(msg.get("labelIds", []) or [])
        alvo = self._normalizar_nome_label(nome_label).lower()
        for l in self._labels_cache():
            if self._normalizar_nome_label(l.get("name", "")).lower() == alvo:
                if l["id"] in labels_msg:
                    return True
        return False

    def criar_label(self, nome: str) -> str:
        lid = self._label_id(nome)
        if lid:
            return lid
        body = {
            "name": nome,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        }
        return self.service.users().labels().create(userId="me", body=body).execute()["id"]

    def aplicar_label(self, msg_id: str, label_nome: str) -> None:
        lid = self.criar_label(label_nome)
        self.service.users().messages().modify(
            userId="me", id=msg_id, body={"addLabelIds": [lid]}
        ).execute()

    def remover_label(self, msg_id: str, label_nome: str) -> None:
        lid = self._label_id(label_nome)
        if not lid:
            return
        self.service.users().messages().modify(
            userId="me", id=msg_id, body={"removeLabelIds": [lid]}
        ).execute()

    # ---- Busca ---------------------------------------------------

    def buscar_emails_pendentes(
        self, label_entrada: str, processado: str, pendente: str,
        incluir_pendentes: bool = False,
    ) -> list[dict]:
        """Retorna a PRIMEIRA mensagem de cada thread elegível.

        Pipeline processa apenas a mensagem raiz (admissão original do cliente),
        nunca respostas subsequentes. Respostas do cliente em threads
        pendentes são tratadas pelo fluxo `buscar_threads_aguardando_cliente`.

        Lógica:
          1. Busca threads com label_entrada SEM processado (e SEM pendente,
             exceto se `incluir_pendentes=True` — v2.9.0)
          2. Para cada thread, pega messages[0]
          3. Se messages[0] tem label processado, pula
             Se tem label pendente, pula UNLESS incluir_pendentes=True
          4. Retorna lista de messages[0] dos threads não-pulados

        `incluir_pendentes=True`: o caller assume responsabilidade de remover
        a label `pendente` antes de processar, senão o email continua marcado
        e pode confundir a UI. Caso de uso: cliente respondeu no thread
        pendente e queremos retentar automaticamente no próximo polling.
        """
        if not self._label_id(label_entrada):
            log.error(f"Label '{label_entrada}' não existe no Gmail")
            return []

        # Filtros -in: SEMPRE adicionados (Gmail faz match por nome no
        # servidor; não dependemos do _label_id local pra montar a query).
        filtros = [f'in:"{label_entrada}"', f'-in:"{processado}"']
        if not incluir_pendentes:
            filtros.append(f'-in:"{pendente}"')
        q = " ".join(filtros)
        log.info(
            f"Gmail query: {q}"
            f"{' (incluindo pendentes pra retentar)' if incluir_pendentes else ''}"
        )

        res = self.service.users().threads().list(
            userId="me", q=q, maxResults=50
        ).execute()
        thread_summaries = res.get("threads", [])

        primeiras: list[dict] = []
        for ts in thread_summaries:
            thread = self.obter_thread(ts["id"])
            msgs = thread.get("messages", []) or []
            if not msgs:
                continue
            primeira = msgs[0]
            # Sanity check: a msg raiz NÃO pode ter processado
            # (defesa em profundidade caso a query não tenha filtrado bem)
            if self._msg_tem_label(primeira, processado):
                log.info(
                    f"   Pulando thread {ts['id'][:16]}: msg raiz já tem '{processado}'"
                )
                continue
            # Pendente: só pula se NÃO estamos no modo "incluir pendentes"
            if not incluir_pendentes and self._msg_tem_label(primeira, pendente):
                log.info(
                    f"   Pulando thread {ts['id'][:16]}: msg raiz já tem '{pendente}'"
                )
                continue
            primeiras.append(primeira)
        return primeiras

    # ---- Extração de corpo + anexos -----------------------------

    @staticmethod
    def _walk_parts(part: dict) -> Iterable[dict]:
        if "parts" in part:
            for p in part["parts"]:
                yield from GmailClient._walk_parts(p)
        else:
            yield part

    def extrair_corpo(self, msg: dict) -> str:
        """Concatena partes text/plain e text/html (HTML stripado)."""
        textos: list[str] = []
        for part in self._walk_parts(msg["payload"]):
            mime = part.get("mimeType", "")
            body = part.get("body", {})
            data = body.get("data")
            if not data:
                continue
            if mime == "text/plain":
                try:
                    textos.append(base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore"))
                except Exception:
                    pass
            elif mime == "text/html" and not textos:
                # Só usa HTML como fallback se não tem text/plain
                try:
                    html = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
                    textos.append(re.sub(r"<[^>]+>", " ", html))
                except Exception:
                    pass
        return "\n".join(t.strip() for t in textos if t.strip())

    def extrair_metadados(self, msg: dict) -> dict:
        headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
        return {
            "remetente": headers.get("from", ""),
            "assunto": headers.get("subject", ""),
            "data": headers.get("date", ""),
        }

    def baixar_anexos(self, msg: dict) -> list[dict]:
        """Retorna [{filename, mime, data: bytes}] pra anexos PDF/PNG/JPG.

        Inclui imagens INLINE (que Gmail manda sem `filename` quando a imagem
        foi arrastada/colada no corpo do email — Content-Disposition: inline,
        embutida via cid: em HTML). Pra elas geramos nome sintético.
        """
        anexos: list[dict] = []
        msg_id = msg["id"]
        contador_inline = 0
        partes_ignoradas: list[str] = []

        for part in self._walk_parts(msg["payload"]):
            mime = part.get("mimeType", "")
            filename = part.get("filename") or ""
            fname_lower = filename.lower()

            # Aceita PDFs, imagens E arquivos compactados (estes serao
            # descompactados depois). Tudo o resto: ignora.
            #
            # 3 camadas de detecção (cada vez mais permissiva):
            #   1. MIME explícito de PDF (PDF_MIMES) ou família image/*
            #   2. Fallback: MIME genérico + extensão confiável (caso Pedro/Outlook)
            #   3. Compactados (.zip/.rar) — extraídos depois
            eh_compactado = fname_lower.endswith(EXT_COMPACTADO)
            eh_pdf_explicito = mime in PDF_MIMES
            eh_imagem_explicita = mime.startswith("image/")
            eh_generico_confiavel = (
                mime in MIMES_GENERICOS and fname_lower.endswith(EXTENSOES_CONFIAVEIS)
            )

            if eh_generico_confiavel and not (eh_pdf_explicito or eh_imagem_explicita):
                mime_display = mime or "(vazio)"
                log.info(
                    f"   ⚠ Aceitando '{filename}' apesar do mime '{mime_display}' "
                    f"— extensão confiável (Outlook/sistemas internos mandam assim)"
                )

            eh_pdf_ou_imagem = eh_pdf_explicito or eh_imagem_explicita or eh_generico_confiavel

            if not eh_compactado and not eh_pdf_ou_imagem:
                if filename or part.get("body", {}).get("attachmentId"):
                    partes_ignoradas.append(f"{filename or '(sem nome)'} [{mime}]")
                continue

            body = part.get("body", {})
            attach_id = body.get("attachmentId")

            if not filename:
                # Imagem inline / parte sem nome → sintetiza
                contador_inline += 1
                ext = mime.split("/")[-1] if "/" in mime else "bin"
                ext = ext.replace("jpeg", "jpg")
                filename = f"inline_{contador_inline}.{ext}"

            if attach_id:
                att = (
                    self.service.users().messages().attachments()
                    .get(userId="me", messageId=msg_id, id=attach_id).execute()
                )
                data = base64.urlsafe_b64decode(att["data"])
            elif "data" in body:
                data = base64.urlsafe_b64decode(body["data"])
            else:
                continue

            if eh_compactado:
                log.info(f"   📦 Descompactando '{filename}' ({len(data)} bytes)...")
                extraidos = _extrair_compactado(filename, data)
                anexos.extend(extraidos)
            else:
                # Anexo avulso do email — grupo vazio (nao agrupado)
                anexos.append({"filename": filename, "mime": mime,
                               "data": data, "grupo": ""})

        if partes_ignoradas:
            log.info(f"   Partes ignoradas (mime não-image/pdf): {partes_ignoradas}")

        return anexos

    # ---- Envio ---------------------------------------------------

    def enviar_email(self, destinatario: str, assunto: str, corpo: str) -> None:
        mime = MIMEText(corpo, "plain", "utf-8")
        mime["to"] = destinatario
        mime["subject"] = assunto
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
        self.service.users().messages().send(userId="me", body={"raw": raw}).execute()

    def responder_no_thread(
        self,
        msg_original: dict,
        corpo: str,
        destinatario: str | None = None,
        cc: str | None = None,
        assunto_override: str | None = None,
    ) -> None:
        """Envia resposta no mesmo thread do email original.

        - `destinatario`: se None, usa o `From` original (cliente).
        - `cc`: opcional (ex: email do DP).
        - Headers `In-Reply-To` / `References` setados → conversa fica linkada.
        - `threadId` do body do send → Gmail agrupa visualmente.

        Valida o endereço do destinatário (e do CC se fornecido). Se inválido
        (ex: `From` original sem email parseável, mailer-daemon, etc.), loga
        e ignora silenciosamente em vez de tentar enviar e falhar com 400.
        """
        headers_orig = {
            h["name"].lower(): h["value"]
            for h in msg_original.get("payload", {}).get("headers", [])
        }
        msgid = headers_orig.get("message-id", "")
        subject_orig = headers_orig.get("subject", "(sem assunto)")
        from_orig = headers_orig.get("from", "")

        to_raw = destinatario or from_orig
        to = _extrair_email(to_raw)
        if not to:
            log.warning(
                f"Destinatário inválido para resposta no thread "
                f"({to_raw!r}) — ignorando silenciosamente"
            )
            return

        cc_clean: str | None = None
        if cc:
            cc_email = _extrair_email(cc)
            if cc_email:
                cc_clean = cc_email
            else:
                log.warning(f"CC inválido ({cc!r}) — enviando sem CC")

        assunto = assunto_override or (
            subject_orig if subject_orig.lower().startswith("re:") else f"Re: {subject_orig}"
        )

        mime = MIMEText(corpo, "plain", "utf-8")
        mime["to"] = to
        if cc_clean:
            mime["cc"] = cc_clean
        mime["subject"] = assunto
        if msgid:
            mime["In-Reply-To"] = msgid
            mime["References"] = msgid

        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
        body = {"raw": raw, "threadId": msg_original.get("threadId")}
        sent = self.service.users().messages().send(userId="me", body=body).execute()

        # Marca a msg recém-enviada com a label do bot. Sem isso, não dá pra
        # distinguir essa resposta de uma escrita manualmente pelo colaborador
        # logado na mesma conta Gmail.
        sent_id = sent.get("id")
        if sent_id:
            try:
                self.aplicar_label(sent_id, LABEL_BOT_ENVIADO)
            except Exception as e:
                log.warning(
                    f"Falha aplicando label '{LABEL_BOT_ENVIADO}' na msg "
                    f"{sent_id}: {e} — detecção de bot pode falhar nesse thread"
                )

    # ---- Continuação de thread (resposta do cliente) -------------

    def obter_thread(self, thread_id: str) -> dict:
        """Retorna o thread inteiro com todas as mensagens."""
        return self.service.users().threads().get(
            userId="me", id=thread_id, format="full"
        ).execute()

    def _from_de(self, msg: dict) -> str:
        for h in msg.get("payload", {}).get("headers", []):
            if h["name"].lower() == "from":
                return (h["value"] or "").lower()
        return ""

    def _eh_do_bot(self, msg: dict) -> bool:
        """Detecta mensagem que o bot enviou.

        Checa a label `Bot-Crosara/Enviado` aplicada explicitamente após
        cada `responder_no_thread`. Comparação por From: foi descartada
        porque o colaborador pode estar logado na mesma conta Gmail e
        escrever manualmente — daria falso positivo.
        """
        return self._msg_tem_label(msg, LABEL_BOT_ENVIADO)

    def buscar_threads_aguardando_cliente(self, label_pendente: str) -> list[dict]:
        """Threads marcados ADMISSÃO/pendente cuja ÚLTIMA mensagem é do cliente
        E que JÁ tiveram pelo menos uma resposta do bot.

        Sem a 2ª condição, threads pendentes onde o bot nunca respondeu (ex:
        falha técnica enviando o reply) seriam reprocessados em loop infinito
        a cada passada — pq a 'última msg do cliente' continua sendo o email
        original que falhou.
        """
        if not self._label_id(label_pendente):
            return []
        res = self.service.users().threads().list(
            userId="me", q=f'label:"{label_pendente}"', maxResults=50,
        ).execute()
        threads: list[dict] = []
        for ts in res.get("threads", []):
            thread = self.obter_thread(ts["id"])
            msgs = thread.get("messages", [])
            if not msgs:
                continue
            if not any(self._eh_do_bot(m) for m in msgs):
                continue  # bot nunca respondeu → estado inicial quebrado, DP vê
            if self._eh_do_bot(msgs[-1]):
                continue  # última é do bot → esperando cliente responder
            threads.append(thread)
        return threads

    def extrair_corpo_thread(self, thread: dict) -> str:
        """Concatena o corpo de TODAS as mensagens do thread, em ordem cronológica."""
        bodies: list[str] = []
        for msg in thread.get("messages", []):
            body = self.extrair_corpo(msg)
            if not body:
                continue
            headers = {h["name"].lower(): h["value"]
                       for h in msg.get("payload", {}).get("headers", [])}
            cabecalho = (
                f"--- De: {headers.get('from', '?')} | "
                f"Data: {headers.get('date', '?')} ---"
            )
            bodies.append(f"{cabecalho}\n{body}")
        return "\n\n".join(bodies)

    def baixar_anexos_thread(self, thread: dict) -> list[dict]:
        """Concatena anexos de TODAS as mensagens do thread (em ordem)."""
        todos: list[dict] = []
        for msg in thread.get("messages", []):
            todos.extend(self.baixar_anexos(msg))
        return todos
