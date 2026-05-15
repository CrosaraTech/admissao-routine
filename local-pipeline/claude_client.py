"""Cliente da API Claude com Vision — gera payload a partir de email + anexos.

O briefing completo (regras, lookups, bugs conhecidos) está em briefing.md.
Esse arquivo é montado como system prompt e enviado em toda chamada.

Fluxo:
  - Recebe corpo do email (texto) + lista de anexos (filename, mime, bytes)
  - Constrói uma mensagem multi-content com:
      • Texto: corpo + metadados + cargo/CBO sugeridos (se houver)
      • image_*/document_* blocks pra cada anexo, em base64
  - Envia pro Claude e parseia o JSON retornado dentro de bloco ```json
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from pathlib import Path

import anthropic


log = logging.getLogger("admissao.claude")


ROOT = Path(__file__).parent
BRIEFING_FILE = ROOT / "briefing.md"


# Tipos MIME aceitos pelo bloco image/document do Anthropic
IMAGE_MIMES = {"image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp"}
DOC_MIMES = {"application/pdf"}


SYSTEM_SUFIXO = """

---

## INSTRUÇÕES DE SAÍDA (Pipeline Local)

Você está rodando dentro do pipeline local da Crosara Contabilidade.

- Receba o CORPO DO EMAIL (texto) e os ANEXOS (PDFs/imagens).
- Extraia TODOS os campos seguindo o briefing acima.
- Retorne APENAS o JSON do payload, dentro de um bloco ```json ... ```.
- NÃO inclua comentários, explicação ou texto antes/depois do JSON.
- Os IDs de `empresa`, `departamento` e `funcao` serão substituídos pelo
  pipeline depois — use placeholders "1" nesses 3 relationships.
- Se faltarem dados essenciais (lista da seção 10 do briefing), retorne:
  {"_pendente": true, "_motivo": "<descrição curta>", "_dados_parciais": {...}}
  com os campos que conseguiu extrair em `_dados_parciais`.
- IMPORTANTE: extraia também o campo `cnpj_empresa` (raiz) com o CNPJ
  da empresa contratante, e `departamento_sugerido` (string livre, opcional)
  pro pipeline resolver depois. Ambos vão FORA do `data` — no nível raiz
  do JSON retornado, ao lado de `data`.

Formato final esperado:
```json
{
  "cnpj_empresa": "12345678000190",
  "departamento_sugerido": "ADMINISTRATIVO",
  "data": {
    "type": "candidatos",
    "attributes": {...},
    "relationships": {...}
  }
}
```
"""


def carregar_briefing() -> str:
    if not BRIEFING_FILE.exists():
        raise FileNotFoundError(
            f"briefing.md não encontrado em {BRIEFING_FILE} — "
            "copie do Obsidian (DP/Automação Admissão API/12 - Briefing...)"
        )
    return BRIEFING_FILE.read_text(encoding="utf-8") + SYSTEM_SUFIXO


class ClaudeClient:
    def __init__(self, model: str = "claude-sonnet-4-20250514", max_tokens: int = 8192):
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY não encontrada no ambiente")
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.max_tokens = max_tokens
        self.system_prompt = carregar_briefing()

    def _bloco_anexo(self, anexo: dict) -> dict | None:
        """Retorna o content-block apropriado pro Claude (image_/document_)."""
        mime = anexo.get("mime", "")
        data = anexo.get("data")
        if not data:
            return None
        b64 = base64.standard_b64encode(data).decode("ascii")
        if mime in IMAGE_MIMES:
            return {
                "type": "image",
                "source": {"type": "base64", "media_type": mime, "data": b64},
            }
        if mime in DOC_MIMES:
            return {
                "type": "document",
                "source": {"type": "base64", "media_type": mime, "data": b64},
            }
        log.warning(f"Anexo com mime não suportado ignorado: {anexo.get('filename')} ({mime})")
        return None

    def gerar_payload(
        self,
        corpo_email: str,
        metadados: dict,
        anexos: list[dict],
        funcoes_candidatas: list[dict] | None = None,
    ) -> dict:
        """Envia tudo pro Claude e retorna o dict parseado do JSON.

        funcoes_candidatas: lista de {nome, cbo, funcao_id} pra desambiguar
        cargo quando a planilha CBO tem múltiplos matches.
        """
        # Texto inicial pro Claude
        intro_partes = [
            "# EMAIL RECEBIDO",
            f"De: {metadados.get('remetente', '?')}",
            f"Assunto: {metadados.get('assunto', '?')}",
            f"Data: {metadados.get('data', '?')}",
            "",
            "## CORPO DO EMAIL (texto)",
            corpo_email or "(vazio)",
            "",
            "## ANEXOS",
            f"Recebendo {len(anexos)} anexo(s): " + ", ".join(
                f"{a['filename']} ({a['mime']})" for a in anexos
            ) or "(nenhum)",
        ]

        if funcoes_candidatas:
            intro_partes += [
                "",
                "## CARGOS CANDIDATOS (planilha CBO da Crosara)",
                "O pipeline filtrou funções compatíveis com o cargo extraído. ",
                "Use o `funcao_id` mais adequado e mencione no campo `nomecargo` ",
                "o nome mais próximo da planilha (UPPERCASE).",
                "",
                "| funcao_id | nome | cbo |",
                "|---|---|---|",
            ]
            for f in funcoes_candidatas[:50]:
                intro_partes.append(
                    f"| {f.get('funcao_id', '?')} | {f.get('nome', '?')} | {f.get('cbo', '?')} |"
                )

        intro_partes += [
            "",
            "## INSTRUÇÃO",
            "Extraia os dados do funcionário, aplique todas as regras do briefing ",
            "(defaults, UPPERCASE, datas ISO, etc.) e retorne o JSON final no formato ",
            "especificado nas INSTRUÇÕES DE SAÍDA.",
        ]
        intro_texto = "\n".join(intro_partes)

        # Monta a content list (texto + anexos)
        content: list[dict] = [{"type": "text", "text": intro_texto}]
        for anexo in anexos:
            bloco = self._bloco_anexo(anexo)
            if bloco:
                content.append(bloco)

        log.info(f"Enviando {len(content)} blocos pro Claude ({self.model})")

        msg = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=self.system_prompt,
            messages=[{"role": "user", "content": content}],
        )

        # Concatena texto de resposta
        resposta = "\n".join(
            b.text for b in msg.content if getattr(b, "type", None) == "text"
        )
        log.debug(f"Resposta Claude (preview): {resposta[:300]}")

        return self._parsear_json(resposta)

    @staticmethod
    def _parsear_json(resposta: str) -> dict:
        """Extrai o JSON do primeiro bloco ```json...``` ou tenta a string toda."""
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", resposta, re.DOTALL)
        if match:
            cru = match.group(1)
        else:
            # Tenta achar primeiro { até último } balanceado
            inicio = resposta.find("{")
            fim = resposta.rfind("}")
            if inicio == -1 or fim == -1 or fim <= inicio:
                raise ValueError(f"Claude não retornou JSON parseable:\n{resposta[:500]}")
            cru = resposta[inicio:fim + 1]

        try:
            return json.loads(cru)
        except json.JSONDecodeError as e:
            raise ValueError(f"JSON inválido do Claude: {e}\nConteúdo:\n{cru[:1000]}")
