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
import time
from pathlib import Path

import anthropic


log = logging.getLogger("admissao.claude")


ROOT = Path(__file__).parent
BRIEFING_FILE = ROOT / "briefing.md"


# Preços oficiais em USD por 1 milhão de tokens (input, output).
# Fonte: https://www.anthropic.com/pricing — atualizar quando mudar.
PRICING_USD_POR_MTOK: dict[str, tuple[float, float]] = {
    # Opus 4.x (premium)
    "claude-opus-4-7":           (15.0, 75.0),
    "claude-opus-4-6":           (15.0, 75.0),
    "claude-opus-4-5":           (15.0, 75.0),
    # Sonnet 4.x (mainstream)
    "claude-sonnet-4-6":         (3.0, 15.0),
    "claude-sonnet-4-5":         (3.0, 15.0),
    "claude-sonnet-4-20250514":  (3.0, 15.0),  # Sonnet 4 (modelo deste pipeline)
    # Haiku 4.5 (econômico)
    "claude-haiku-4-5":          (1.0, 5.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}


def _precos_do_modelo(model: str) -> tuple[float, float]:
    """Retorna (preço_input, preço_output) em USD/MTok pro model dado.
    Fallback: Sonnet 4 ($3/$15) se modelo não conhecido."""
    if model in PRICING_USD_POR_MTOK:
        return PRICING_USD_POR_MTOK[model]
    # Tenta match por família (sonnet/opus/haiku)
    m_lower = model.lower()
    if "opus" in m_lower:
        return (15.0, 75.0)
    if "haiku" in m_lower:
        return (1.0, 5.0)
    return (3.0, 15.0)  # default Sonnet


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

## LEITURA CUIDADOSA DE IMAGENS DE DOCUMENTOS

As fotos de RG, CPF, CTPS, título eleitoral etc. frequentemente vêm:
  - ROTACIONADAS (90/180/270 graus) ou de cabeça pra baixo
  - Tortas, com luz ruim, manchadas
  - Com texto em MAIÚSCULAS pequenas (cartão de identidade)
  - Com frente E verso na mesma imagem (cada lado em uma orientação)

ANTES de marcar campo como "não encontrado", examine CADA imagem em
TODAS as orientações possíveis. Procure ativamente por:
  - `identidade` (RG): número com 7+ dígitos, frequentemente rotulado
    "REGISTRO GERAL", "RG" ou "Nº". Ex: "9.462.146" ou "94.62146"
  - `dataidentidade`: rotulada "DATA DE EXPEDIÇÃO", formato DD/MM/AAAA
  - `orgaoemissoridentidade`: sigla do órgão, ex: "SSP/SP", "SDS/PE",
    "SSP/GO", "SECC/RJ"
  - `nascimento`: rotulada "DATA DE NASCIMENTO", formato DD/MM/AAAA
  - `nomedamae`: rotulado "FILIAÇÃO" ou "MÃE" (geralmente vem 2 nomes
    — pai primeiro, mãe segundo, ou vice-versa)
  - `naturalidade`: rotulada "NATURALIDADE" — cidade + UF (ex: "Itacuruba-PE")

Se um campo está VISÍVEL na imagem, EXTRAIA mesmo que a imagem esteja
inclinada/girada. Só marque como faltante se realmente não conseguir ler.
- Os IDs de `empresa`, `departamento` e `funcao` serão substituídos pelo
  pipeline depois — use placeholders "1" nesses 3 relationships.
- Se faltarem dados essenciais (lista da seção 10 do briefing), OU se o
  email contém múltiplas admissões (mais de um funcionário), retorne:
  {"_pendente": true, "_motivo": "<descrição curta e ESPECÍFICA>",
   "_dados_parciais": {...}}

  ⚠ IMPORTANTE: SEMPRE popule `_dados_parciais` com TUDO que você conseguiu
  extrair do email/anexos, mesmo quando incompleto. Esse objeto é mostrado
  ao cliente no email de resposta — ele precisa VER o que você identificou
  pra confiar no pipeline. NUNCA retorne _dados_parciais vazio se há algum
  dado identificável nos documentos.

  Use chaves PT-BR simples no _dados_parciais (ex: nome, cpf, nascimento,
  nomedamae, cargo, salario, cnpj_empresa). Se há múltiplos funcionários,
  liste TODOS em _dados_parciais com prefixo (ex: "funcionario_1_nome",
  "funcionario_2_nome") e explique no _motivo que precisa de 1 email por
  funcionário.

- IMPORTANTE: extraia também o campo `cnpj_empresa` (raiz) com o CNPJ
  da empresa contratante, e `departamento_sugerido` (string livre, opcional)
  pro pipeline resolver depois. Ambos vão FORA do `data` — no nível raiz
  do JSON retornado, ao lado de `data`.

## MÚLTIPLAS ADMISSÕES NO MESMO EMAIL

Se você identificar dados COMPLETOS de mais de um funcionário no mesmo
email (ex: "segue documentos de Silvani e Lourrana..." com docs de ambas),
NÃO marque pendente — gere UM PAYLOAD PARA CADA usando o formato `admissoes`:

```json
{
  "cnpj_empresa": "12345678000190",
  "admissoes": [
    {
      "departamento_sugerido": "COZINHA",
      "data": {"type": "candidatos", "attributes": {...}, "relationships": {...}}
    },
    {
      "departamento_sugerido": "COZINHA",
      "data": {"type": "candidatos", "attributes": {...}, "relationships": {...}}
    }
  ]
}
```

Use o formato `admissoes` SEMPRE que houver 2+ pessoas, mesmo se uma
delas tiver dados incompletos — você pode incluir o que conseguiu de cada
uma. O pipeline processa CADA admissão independentemente: a que tiver
todos os dados sobe, a que faltar algum vai pra pendência (e o cliente
recebe só pedindo o que falta daquela específica).

Pra UMA admissão só, retorne o formato simples (data no root):
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

Só use `_pendente: true` quando NÃO conseguiu identificar dados úteis de
NENHUMA admissão (ex: anexo só de comprovante, email sem corpo nem docs).
"""


def carregar_briefing() -> str:
    if not BRIEFING_FILE.exists():
        raise FileNotFoundError(
            f"briefing.md não encontrado em {BRIEFING_FILE} — "
            "copie do Obsidian (DP/Automação Admissão API/12 - Briefing...)"
        )
    return BRIEFING_FILE.read_text(encoding="utf-8") + SYSTEM_SUFIXO


class ClaudeClient:
    # Intervalo mínimo entre chamadas consecutivas ao Claude (segundos).
    # Throttle simples pra evitar rate-limit em pipelines com vários emails
    # processados em sequência (cada email pode disparar 1-2 calls).
    INTERVALO_MIN_ENTRE_CHAMADAS = 3.0

    # Campos críticos pra detectar divergência entre chamadas de verificação.
    # Se 2 chamadas extraem valores DIFERENTES nesses campos (ex: CPFs diferentes),
    # é red flag — Claude está alucinando em um dos casos.
    CAMPOS_CRITICOS_VERIFICACAO = [
        "cpf", "nome", "nascimento", "identidade",
        "admissao", "dataidentidade", "ctps",
    ]

    def __init__(
        self,
        model: str = "claude-sonnet-4-20250514",
        max_tokens: int = 8192,
        chamadas_verificacao: int = 1,
    ):
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY não encontrada no ambiente")
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.max_tokens = max_tokens
        self.system_prompt = carregar_briefing()
        self._ts_ultima_chamada: float = 0.0
        # Self-consistency: chama N vezes e funde. 1 = sem verificação,
        # 2 = double-check (recomendado), 3+ = ensemble.
        self.chamadas_verificacao = max(1, int(chamadas_verificacao))
        # Contadores cumulativos de billing (toda chamada feita pela instância)
        self.usage_total: dict[str, int] = {
            "n_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }

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
        """Wrapper público: faz 1 chamada ao Claude. Se a resposta tiver
        indícios de inconsistência (campos chave faltando, _pendente=true),
        dispara chamadas extra de verificação até `chamadas_verificacao`.
        Funde respostas pegando a mais completa.

        Otimização vs. self-consistency cego (sempre 2 chamadas): quando a
        1ª resposta já tá completa (caso comum), NÃO paga a 2ª. Custo médio
        cai bastante mantendo a robustez nos casos suspeitos.
        """
        primeira = self._gerar_payload_unico(
            corpo_email, metadados, anexos, funcoes_candidatas
        )

        if self.chamadas_verificacao <= 1 or not self._precisa_verificacao(primeira):
            return primeira

        log.info(
            f"   🔁 Inconsistência na 1ª resposta — disparando até "
            f"{self.chamadas_verificacao - 1} chamada(s) de verificação"
        )
        respostas: list[dict] = [primeira]
        for i in range(1, self.chamadas_verificacao):
            try:
                r = self._gerar_payload_unico(
                    corpo_email, metadados, anexos, funcoes_candidatas
                )
                respostas.append(r)
                # Se a nova resposta JÁ parece consistente, pode parar antes
                if not self._precisa_verificacao(r):
                    log.info(f"   ✅ Verificação {i+1} retornou resposta consistente — parando")
                    break
            except Exception:
                log.exception(f"   Chamada de verificação {i+1} falhou")

        if len(respostas) == 1:
            log.warning("   Verificação falhou — usando 1ª resposta sem comparação")
            return respostas[0]

        return self._mesclar_respostas(respostas)

    @classmethod
    def _precisa_verificacao(cls, resp: dict) -> bool:
        """Heurística: a resposta parece suspeita o suficiente pra justificar
        uma 2ª chamada de verificação?

        Triggers:
          - _pendente=true (Claude desistiu — vamos tentar de novo)
          - Sem blocos de admissão (resposta vazia/malformada)
          - Algum bloco com 3+ campos chave faltando (nome/cpf/nascimento/
            identidade/admissao/salario) — Claude pode ter perdido coisas
            que estavam visíveis
        """
        if resp.get("_pendente"):
            log.info("   📍 _pendente=true → vamos verificar")
            return True

        blocos = resp.get("admissoes") or ([resp] if "data" in resp else [])
        if not blocos:
            log.info("   📍 resposta sem blocos → vamos verificar")
            return True

        CAMPOS_CHAVE = ["nome", "cpf", "nascimento", "identidade", "admissao", "salario"]
        for i, b in enumerate(blocos, 1):
            attrs = (b.get("data") or {}).get("attributes") or {}
            ausentes = [k for k in CAMPOS_CHAVE if not attrs.get(k)]
            if len(ausentes) >= 3:
                log.info(
                    f"   📍 bloco {i}/{len(blocos)} com {len(ausentes)}/6 campos chave "
                    f"faltando ({', '.join(ausentes)}) → vamos verificar"
                )
                return True
        return False

    def _gerar_payload_unico(
        self,
        corpo_email: str,
        metadados: dict,
        anexos: list[dict],
        funcoes_candidatas: list[dict] | None = None,
    ) -> dict:
        """Uma única chamada ao Claude. Retorna o dict parseado do JSON.

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
                "## DESAMBIGUAÇÃO DE CARGO",
                "O pipeline encontrou múltiplas funções parecidas no cadastro "
                "Crosara pro cargo que você extraiu. Sua tarefa nesse turno é "
                "ESCOLHER UMA da lista abaixo:",
                "",
                "1. Olhe a função, CBO e setor implícito de cada linha.",
                "2. Copie o `nome_cargo` EXATO (UPPERCASE) pro campo `nomecargo` "
                "do payload — o pipeline localiza a função pelo nome.",
                "3. Se houver entradas IGUAIS (mesmo nome + CBO), pode escolher "
                "qualquer uma — são duplicatas do cadastro do eContador.",
                "",
                "| funcao_id | nome_cargo | cbo |",
                "|---|---|---|",
            ]
            for f in funcoes_candidatas[:50]:
                nome = f.get("nome_cargo") or f.get("nome") or "?"
                intro_partes.append(
                    f"| {f.get('funcao_id', '?')} | {nome} | {f.get('cbo', '?')} |"
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

        # Throttle: garante intervalo mínimo entre chamadas consecutivas
        delta = time.time() - self._ts_ultima_chamada
        if 0 < delta < self.INTERVALO_MIN_ENTRE_CHAMADAS:
            espera = self.INTERVALO_MIN_ENTRE_CHAMADAS - delta
            log.info(f"⏳ Aguardando {espera:.1f}s antes da próxima chamada ao Claude")
            time.sleep(espera)

        log.info(f"Enviando {len(content)} blocos pro Claude ({self.model})")

        msg = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=self.system_prompt,
            messages=[{"role": "user", "content": content}],
        )
        self._ts_ultima_chamada = time.time()

        # Captura tokens + estima custo desta chamada
        self._registrar_uso(getattr(msg, "usage", None))

        # Concatena texto de resposta
        resposta = "\n".join(
            b.text for b in msg.content if getattr(b, "type", None) == "text"
        )
        log.debug(f"Resposta Claude (preview): {resposta[:300]}")

        return self._parsear_json(resposta)

    def _registrar_uso(self, usage) -> None:
        """Acumula tokens da chamada atual e loga estimativa de custo."""
        if usage is None:
            return
        inp = getattr(usage, "input_tokens", 0) or 0
        out = getattr(usage, "output_tokens", 0) or 0
        cache_w = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cache_r = getattr(usage, "cache_read_input_tokens", 0) or 0

        self.usage_total["n_calls"] += 1
        self.usage_total["input_tokens"] += inp
        self.usage_total["output_tokens"] += out
        self.usage_total["cache_creation_input_tokens"] += cache_w
        self.usage_total["cache_read_input_tokens"] += cache_r

        custo = self.estimar_custo_usd(inp, out, cache_w, cache_r)
        # Log conciso, separa cache pra dar visibilidade quando estiver ativo
        extra = ""
        if cache_w or cache_r:
            extra = f" [cache: +{cache_w:,} write / +{cache_r:,} read]"
        log.info(
            f"   💰 {inp:,} input + {out:,} output tokens{extra} "
            f"≈ US$ {custo:.4f}"
        )

    def estimar_custo_usd(
        self,
        input_tokens: int,
        output_tokens: int,
        cache_creation: int = 0,
        cache_read: int = 0,
    ) -> float:
        """Estima custo em USD desta chamada (ou agregado, se chamado com totais).

        Pricing oficial Anthropic:
          - input regular: 1x base
          - cache write: 1.25x base
          - cache read: 0.1x base
          - output: out_price
        """
        in_price, out_price = _precos_do_modelo(self.model)
        in_regular = (input_tokens / 1_000_000) * in_price
        in_cache_w = (cache_creation / 1_000_000) * in_price * 1.25
        in_cache_r = (cache_read / 1_000_000) * in_price * 0.10
        out_total = (output_tokens / 1_000_000) * out_price
        return in_regular + in_cache_w + in_cache_r + out_total

    def usage_resumo(self) -> dict:
        """Retorna dict com cumulativo + estimativa de custo USD."""
        u = self.usage_total
        custo = self.estimar_custo_usd(
            u["input_tokens"], u["output_tokens"],
            u["cache_creation_input_tokens"], u["cache_read_input_tokens"],
        )
        return {
            **u,
            "model": self.model,
            "custo_usd_estimado": round(custo, 4),
        }

    # ---- Self-consistency (multi-chamada) ----------------------------

    @staticmethod
    def _contar_preenchidos(resp: dict) -> int:
        """Conta valores não-vazios em campos relevantes da resposta.
        Usado pra ranquear respostas: mais campos preenchidos → mais completa.
        """
        if not isinstance(resp, dict):
            return 0
        n = 0
        # Caminho _pendente: conta o que foi pra _dados_parciais
        if resp.get("_pendente"):
            for v in (resp.get("_dados_parciais") or {}).values():
                if v not in (None, "", 0, [], {}):
                    n += 1
            return n
        # cnpj_empresa raiz vale 1
        if resp.get("cnpj_empresa"):
            n += 1
        # Multi-admissão
        blocos = resp.get("admissoes")
        if isinstance(blocos, list) and blocos:
            for b in blocos:
                if isinstance(b, dict):
                    n += ClaudeClient._contar_campos_bloco(b)
            return n
        # Single legacy
        if "data" in resp:
            n += ClaudeClient._contar_campos_bloco(resp)
        return n

    @staticmethod
    def _contar_campos_bloco(bloco: dict) -> int:
        """Conta attributes não-vazios + relationships com .data.id."""
        n = 0
        data = bloco.get("data") or {}
        attrs = data.get("attributes") or {}
        for v in attrs.values():
            if v not in (None, "", 0, [], {}):
                n += 1
        rels = data.get("relationships") or {}
        for v in rels.values():
            if isinstance(v, dict) and (v.get("data") or {}).get("id"):
                n += 1
        return n

    @classmethod
    def _mesclar_respostas(cls, respostas: list[dict]) -> dict:
        """Funde N respostas em uma. Estratégia:

        1. Se há respostas NÃO-pendentes, descarta as `_pendente: true`
           (Claude que conseguiu processar é melhor que o que desistiu).
        2. Entre as candidatas, pega a com MAIS campos preenchidos.
        3. Loga divergências em campos críticos pra auditoria/debug.

        Não tenta fundir per-field — risco alto de misturar CPF de uma
        com nome de outra, criando dados inconsistentes. Melhor confiar
        em uma resposta inteira que é internamente coerente.
        """
        if not respostas:
            return {}
        if len(respostas) == 1:
            return respostas[0]

        nao_pendentes = [r for r in respostas if not r.get("_pendente")]
        candidatas = nao_pendentes or respostas

        candidatas_ord = sorted(candidatas, key=cls._contar_preenchidos, reverse=True)
        base = candidatas_ord[0]
        contagens = [cls._contar_preenchidos(r) for r in respostas]
        log.info(
            f"   Mesclando {len(respostas)} respostas — campos preenchidos: "
            f"{contagens} → escolhida a com {max(contagens)} campos"
        )

        # Comparação em campos críticos pra auditoria
        cls._log_divergencias(respostas)
        return base

    @classmethod
    def _log_divergencias(cls, respostas: list[dict]) -> None:
        """Compara campos críticos entre as respostas. Loga warning quando
        ≥2 respostas têm valores DIFERENTES (não vazios) no mesmo campo —
        indica que Claude alucinou em pelo menos uma das chamadas.
        """
        valores_por_campo: dict[str, set] = {k: set() for k in cls.CAMPOS_CRITICOS_VERIFICACAO}

        def coletar_de_bloco(bloco: dict) -> None:
            attrs = (bloco.get("data") or {}).get("attributes") or {}
            for k in cls.CAMPOS_CRITICOS_VERIFICACAO:
                v = attrs.get(k)
                if v not in (None, "", 0):
                    valores_por_campo[k].add(str(v).strip().upper())

        for r in respostas:
            if r.get("_pendente"):
                dp = r.get("_dados_parciais") or {}
                for k in cls.CAMPOS_CRITICOS_VERIFICACAO:
                    v = dp.get(k)
                    if v not in (None, "", 0):
                        valores_por_campo[k].add(str(v).strip().upper())
                continue
            blocos = r.get("admissoes") or []
            if blocos:
                for b in blocos:
                    if isinstance(b, dict):
                        coletar_de_bloco(b)
            elif "data" in r:
                coletar_de_bloco(r)

        divergentes = {k: v for k, v in valores_por_campo.items() if len(v) > 1}
        if divergentes:
            for campo, valores in divergentes.items():
                log.warning(
                    f"   ⚠ DIVERGÊNCIA em '{campo}' entre as chamadas: "
                    f"{sorted(valores)} — Claude pode ter alucinado em uma. "
                    f"Verifique o payload final."
                )

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
