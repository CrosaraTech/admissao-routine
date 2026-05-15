"""Cliente Gmail — autenticação via env var GMAIL_TOKEN.

Reproduz o padrão usado em ../main.py mas focado em operações que o pipeline
local precisa: buscar emails pendentes, extrair corpo (texto) + anexos
(PDF/PNG/JPG em bytes brutos), aplicar labels e enviar email pro DP.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from email.mime.text import MIMEText
from typing import Iterable

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import google_auth_httplib2
import httplib2


log = logging.getLogger("admissao.gmail")


GMAIL_SCOPES_DEFAULT = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]


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

    def _label_id(self, nome: str) -> str | None:
        labels = self.service.users().labels().list(userId="me").execute().get("labels", [])
        return next((l["id"] for l in labels if l["name"] == nome), None)

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
        self, label_entrada: str, processado: str, pendente: str
    ) -> list[dict]:
        """Lista emails que têm label_entrada e NÃO têm processado/pendente."""
        if not self._label_id(label_entrada):
            log.error(f"Label '{label_entrada}' não existe no Gmail")
            return []

        q_parts = [f'label:"{label_entrada}"']
        if self._label_id(processado):
            q_parts.append(f'-label:"{processado}"')
        if self._label_id(pendente):
            q_parts.append(f'-label:"{pendente}"')
        q = " ".join(q_parts)

        res = self.service.users().messages().list(
            userId="me", q=q, maxResults=50
        ).execute()
        ids = res.get("messages", [])
        msgs = []
        for m in ids:
            msgs.append(
                self.service.users().messages().get(userId="me", id=m["id"]).execute()
            )
        return msgs

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
        """Retorna [{filename, mime, data: bytes}] pra anexos PDF/PNG/JPG."""
        anexos: list[dict] = []
        msg_id = msg["id"]
        for part in self._walk_parts(msg["payload"]):
            filename = part.get("filename", "")
            mime = part.get("mimeType", "")
            if not filename:
                continue
            if not (mime.startswith("image/") or mime == "application/pdf"):
                continue
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
                continue
            anexos.append({"filename": filename, "mime": mime, "data": data})
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
        """
        headers_orig = {
            h["name"].lower(): h["value"]
            for h in msg_original.get("payload", {}).get("headers", [])
        }
        msgid = headers_orig.get("message-id", "")
        subject_orig = headers_orig.get("subject", "(sem assunto)")
        from_orig = headers_orig.get("from", "")

        to = destinatario or from_orig
        assunto = assunto_override or (
            subject_orig if subject_orig.lower().startswith("re:") else f"Re: {subject_orig}"
        )

        mime = MIMEText(corpo, "plain", "utf-8")
        mime["to"] = to
        if cc:
            mime["cc"] = cc
        mime["subject"] = assunto
        if msgid:
            mime["In-Reply-To"] = msgid
            mime["References"] = msgid

        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
        body = {"raw": raw, "threadId": msg_original.get("threadId")}
        self.service.users().messages().send(userId="me", body=body).execute()

    # ---- Continuação de thread (resposta do cliente) -------------

    def obter_thread(self, thread_id: str) -> dict:
        """Retorna o thread inteiro com todas as mensagens."""
        return self.service.users().threads().get(
            userId="me", id=thread_id, format="full"
        ).execute()

    def buscar_threads_aguardando_cliente(self, label_pendente: str) -> list[dict]:
        """Threads marcados ADMISSÃO/pendente cuja ÚLTIMA mensagem é do cliente.

        Ou seja: o bot pediu informação faltante; cliente respondeu.
        """
        if not self._label_id(label_pendente):
            return []
        res = self.service.users().threads().list(
            userId="me", q=f'label:"{label_pendente}"', maxResults=50,
        ).execute()
        bot = self.meu_email()
        threads: list[dict] = []
        for ts in res.get("threads", []):
            thread = self.obter_thread(ts["id"])
            msgs = thread.get("messages", [])
            if not msgs:
                continue
            last = msgs[-1]
            from_addr = ""
            for h in last.get("payload", {}).get("headers", []):
                if h["name"].lower() == "from":
                    from_addr = h["value"].lower()
                    break
            if bot and bot in from_addr:
                continue  # última msg é do bot; cliente ainda não respondeu
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
