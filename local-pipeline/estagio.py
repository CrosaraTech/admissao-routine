"""estagio.py — detector de admissão de estagiário (v2.11.0).

Estagiário não é CLT — função e provavelmente outros campos do payload
devem ser diferentes. Este módulo detecta quando o email/anexos indicam
que a admissão é de estágio, pra o pipeline tratar como caso especial.

Sinais detectados:
  - Palavras-chave no ASSUNTO: "estagiário", "estagiária", "estágio",
    "estagio", "TCE" (Termo de Compromisso de Estágio)
  - Palavras-chave no CORPO: as acima + "termo de compromisso",
    "estágio remunerado", "bolsa-estágio", "agente de integração",
    "Lei 11.788", "CIEE", "NUBE", "IEL", "CIE"
  - Nome de ANEXOS: "termo_compromisso", "TCE", "estagio_*", etc.

Caso real (YURI 11/06/2026): "ADMISSÃO ESTAGIÁRIO ASH TALENTOS YURI..."
no assunto + "termo de compromisso estagiário" no corpo.

A regra é PERMISSIVA: qualquer sinal forte basta. Falso positivo é melhor
que falso negativo aqui — admissão de estagiário processada como CLT vira
contrato errado, problema trabalhista. Admissão CLT marcada como estágio
por engano vira pendência (operador verifica).
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass

log = logging.getLogger("admissao.estagio")


# Palavras-chave fortes — qualquer ocorrência dispara
PALAVRAS_FORTES = (
    "estagiario", "estagiaria",
    "estagio remunerado", "estagio nao remunerado",
    "termo de compromisso de estagio",
    "termo de compromisso estagiario",
    "termo de compromisso estagiaria",
    "lei 11.788", "lei 11788", "lei nº 11.788",
    "bolsa estagio", "bolsa-estagio", "bolsa de estagio",
    "agente de integracao",
    "ciee", "nube", "iel ", "cie ",  # agentes de integração comuns
    "abrh estagio", "estagio supervisionado",
)

# Palavras médias — só contam se aparecerem com OUTRO contexto
# (ex: "estagio" sozinho pode ser de "estagio inicial do projeto")
PALAVRAS_MEDIAS = (
    "estagio",   # sozinho é fraco, mas combinado com "TCE"/"termo" → forte
    "tce",       # sigla pode aparecer em outros contextos, mas com "estagio" perto = forte
)

# Padrões em nomes de anexo
PADROES_ANEXO = (
    r"termo[_\-\s]*compromisso",
    # TCE com separadores comuns em filename (underscore, hífen, ponto, espaço,
    # ou início/fim). `\b` falha pq `_` é word char no Python regex.
    r"(?:^|[^a-z0-9])tce(?:[^a-z0-9]|$)",
    r"estagio",
    r"estagiari[oa]",
    r"agente[_\-\s]*integracao",
)


@dataclass(frozen=True)
class DeteccaoEstagio:
    """Resultado da detecção. `evidencias` lista os sinais encontrados
    pra auditoria/log/diagnóstico ao DP."""
    eh_estagio: bool
    confianca: float            # 0.0–1.0
    evidencias: tuple[str, ...]  # ex: ('assunto contém "estagiário"', 'anexo "termo_compromisso.pdf"')


def _norm(s: str | None) -> str:
    """Lowercase + sem acentos + colapsa espaços."""
    if not s:
        return ""
    s2 = unicodedata.normalize("NFKD", s)
    s2 = "".join(c for c in s2 if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s2.lower()).strip()


def _achar_palavras(texto_norm: str, palavras: tuple[str, ...]) -> list[str]:
    """Retorna palavras da lista que aparecem como substring (já normalizado)."""
    return [p for p in palavras if p in texto_norm]


def detectar(
    assunto: str = "",
    corpo: str = "",
    anexos_filenames: list[str] | tuple[str, ...] = (),
) -> DeteccaoEstagio:
    """Detecta se a admissão é de estagiário.

    Args:
        assunto: assunto do email
        corpo: corpo (já texto puro, sem HTML)
        anexos_filenames: lista de filenames dos anexos

    Returns:
        DeteccaoEstagio com flag, confiança e evidências.
    """
    evidencias: list[str] = []
    pontos = 0.0  # acumula pra confiança

    assunto_norm = _norm(assunto)
    corpo_norm = _norm(corpo)
    texto_combinado = f"{assunto_norm} {corpo_norm}"

    # 1. Palavras FORTES (qualquer uma já basta)
    encontradas_assunto = _achar_palavras(assunto_norm, PALAVRAS_FORTES)
    for p in encontradas_assunto:
        evidencias.append(f"assunto contém '{p}'")
        pontos += 1.0
    encontradas_corpo = _achar_palavras(corpo_norm, PALAVRAS_FORTES)
    for p in encontradas_corpo:
        if p not in encontradas_assunto:  # evita contar 2x
            evidencias.append(f"corpo contém '{p}'")
            pontos += 0.8

    # 2. Palavras MÉDIAS — só somam se houver +1 sinal independente
    medias_encontradas = _achar_palavras(texto_combinado, PALAVRAS_MEDIAS)
    if medias_encontradas and len(medias_encontradas) >= 2:
        # Ex: "estagio" + "tce" no mesmo email — combinação forte
        for p in medias_encontradas:
            evidencias.append(f"texto contém '{p}'")
        pontos += 0.7
    elif medias_encontradas and pontos > 0:
        # Palavra média + outra evidência forte = reforço
        evidencias.append(f"texto também contém '{medias_encontradas[0]}'")
        pontos += 0.3

    # 3. Filenames dos anexos — sinal forte (operador nomeou intencionalmente).
    # Padrões muito específicos como "TCE" ou "termo_compromisso" são quase
    # certeza de estágio. "estagio" sozinho no nome também conta forte.
    for filename in (anexos_filenames or ()):
        fn_norm = _norm(filename)
        for padrao in PADROES_ANEXO:
            if re.search(padrao, fn_norm):
                evidencias.append(f"anexo '{filename}' bate em /{padrao}/")
                pontos += 0.8
                break  # cada anexo conta no máx 1 vez

    # Decisão: pontuação total >= 0.7 → é estágio
    eh_estagio = pontos >= 0.7
    confianca = min(pontos / 1.5, 1.0) if eh_estagio else 0.0

    return DeteccaoEstagio(
        eh_estagio=eh_estagio,
        confianca=confianca,
        evidencias=tuple(evidencias),
    )
