"""Payload builder — injeta IDs resolvidos e faz sanitização final.

O Claude já devolve quase tudo pronto seguindo o briefing. Este módulo:
  1. Substitui os IDs placeholder de empresa/departamento/funcao
  2. Garante que `data.type = "candidatos"`
  3. Aplica regras de segurança: CPF como int, numero=0 se ausente, etc.
  4. Remove campos top-level extras (cnpj_empresa, departamento_sugerido,
     _pendente, etc.) que ficam no envelope mas não no payload final
"""

from __future__ import annotations

import logging
import re
from typing import Any

from departamento import SEM_DEPARTAMENTO


log = logging.getLogger("admissao.payload")


CHAVES_TOPO_PARA_REMOVER = {
    "cnpj_empresa", "departamento_sugerido", "cbo_sugerido", "cargo_extraido",
    "_pendente", "_motivo", "_dados_parciais",
}


# Campos que SEMPRE precisam ser preenchidos manualmente no Alterdata Desktop
# (limitações de produto + bugs do sync E-plugin). Referência: briefing.md
# seção 9 e lookups.json:campos_faltando_no_payload.
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
    "Tipo de Identidade (bug sync — chega vazio mesmo com id=1)",
    "Cor/Raça (bug off-by-one — confirmar no Desktop)",
]


# Campos obrigatórios pra subir a admissão (lista do eContador, captura tela).
# Faltando qualquer um → pendência (DP completa manual e o e-mail vira pendente).
#
# Endereço (cep/rua/bairro/cidade + relationship estado) AGORA é obrigatório —
# antes ficava em branco e o DP preenchia manualmente; mudou pra exigir que o
# cliente mande comprovante (em nome do funcionário ou de parente declarado).
#
# `numero` continua opcional (briefing: 0 = "sem número", API exige Integer e
# não aceita "SN" string — DP marca checkbox manual no Desktop quando 0).
ATTRS_OBRIGATORIOS = [
    "nome",
    "cpf",
    "admissao",
    "nascimento",
    "nomedamae",
    "municipionascimento",
    "diascontratoexperiencia",
    "primeiroemprego",
    "salario",
    # Endereço (numero é exceção — fica opcional)
    "cep",
    "rua",
    "bairro",
    "cidade",
]

RELS_OBRIGATORIOS = [
    "empresa",
    "departamento",
    "funcao",
    "estadocivil",
    "sexo",
    "raca",  # Cor
    "escolaridade",
    "naturalidade",
    "paisnascimento",
    "tipoadmissao",
    "categoriawdp",  # Categoria
    "formapagamento",
    "estado",  # UF do endereço — relationship porque a API espera id
]

LABELS_AMIGAVEIS = {
    "nome": "Nome",
    "cpf": "CPF",
    "admissao": "Data de Admissão",
    "dataatestadoocupacional": "Data ASO",
    "nascimento": "Data de Nascimento",
    "nomedamae": "Nome da Mãe",
    "nomedopai": "Nome do Pai",
    "municipionascimento": "Município de nascimento",
    "cep": "CEP",
    "rua": "Rua",
    "numero": "Número (endereço)",
    "complemento": "Complemento",
    "bairro": "Bairro",
    "cidade": "Cidade",
    "diascontratoexperiencia": "Quantidade de dias do contrato de experiência",
    "primeiroemprego": "Primeiro Emprego",
    "salario": "Salário Base",
    "empresa": "Empresa",
    "departamento": "Departamento",
    "funcao": "Função",
    "estadocivil": "Estado Civil",
    "sexo": "Sexo",
    "raca": "Cor",
    "escolaridade": "Escolaridade",
    "naturalidade": "Naturalidade",
    "paisnascimento": "País de Nascimento",
    "estado": "Estado (endereço)",
    "tipoadmissao": "Tipo de Admissão",
    "categoriawdp": "Categoria",
    "formapagamento": "Forma de Pagamento",
    # Documentos
    "identidade": "RG",
    "dataidentidade": "Data RG",
    "orgaoemissoridentidade": "Órgão Emissor",
    "ctps": "CTPS",
    "seriectps": "Série CTPS",
    "pis": "PIS",
    "tituloeleitor": "Título Eleitor",
    "zonatituloeleitor": "Zona Título",
    "secaotituloeleitor": "Seção Título",
}


def _ajustar_admissao_se_hoje(admissao, data_email_dt, hoje):
    """Se admissao == hoje E temos a hora do email, desloca pra evitar
    cadastrar com data corrente (regra de negócio do escritório):

      - email enviado até 15h00 (local)  → admissao = hoje + 1 dia
      - email enviado depois das 15h00   → admissao = hoje + 2 dias

    Se a nova data cair em sábado/domingo, empurra pra primeira terça-feira
    seguinte (regra do escritório: evita segunda movimentada).

    Quando admissao != hoje ou data_email_dt é None, retorna admissao sem
    alterar.
    """
    import datetime as _dt

    if admissao != hoje or data_email_dt is None:
        return admissao

    # Hora local do email (converte de UTC se vier com tz)
    try:
        hora_local = data_email_dt.astimezone().hour
    except (AttributeError, ValueError, TypeError):
        hora_local = getattr(data_email_dt, "hour", 0)

    delta = 1 if hora_local < 15 else 2
    nova = hoje + _dt.timedelta(days=delta)

    # Skip FDS: weekday 5=sábado, 6=domingo. Empurra pra próxima terça (1).
    if nova.weekday() in (5, 6):
        dias_pra_terca = (1 - nova.weekday()) % 7
        if dias_pra_terca == 0:
            dias_pra_terca = 7
        nova = nova + _dt.timedelta(days=dias_pra_terca)

    return nova


def aplicar_regra_data_admissao(
    payload: dict,
    hoje: "datetime.date | None" = None,
    usar_atual_se_invalida: bool = False,
    data_email_dt: "datetime.datetime | None" = None,
) -> tuple[dict, str | None]:
    """Aplica regra de negócio: data de admissão = ASO + 1 dia (default).

    Cenários (todos olhando attrs.admissao e attrs.dataatestadoocupacional):
      A) admissao já presente → mantém
      B) admissao ausente e ASO presente → seta admissao = ASO + 1 dia
      C) ambos ausentes → retorna erro pedindo data (cliente)
         (exceto se `usar_atual_se_invalida=True` → usa hoje)

    Regra pra datas retroativas (mudança 10/06/2026): se a data resultante
    < hoje, **colapsa automaticamente pra hoje** e a regra do horário do
    email aplica o deslocamento. Antes a gente bloqueava com pendência;
    agora o escritório quer que ASOs antigos e emails atrasados sejam
    processados normalmente (sem cobrar nova data do cliente). Datas com
    > 30 dias de atraso geram warning de auditoria mas seguem o fluxo.

    Regra do horário do email: se admissao final == hoje (inclusive após
    colapsar retroativa) E recebemos a data do email (`data_email_dt`):
      - email até 15h → +1 dia útil
      - email depois das 15h → +2 dias úteis
      - se cair em FDS, empurra pra terça-feira seguinte

    `usar_atual_se_invalida=True` ainda controla o caso C (sem admissão E
    sem ASO). Pra datas retroativas o parâmetro é redundante (sempre colapsa).

    Retorna (payload_atualizado, erro_motivo_cliente_ou_None).
    Quando erro → o caller deve transformar em pendência cliente.
    """
    import datetime as _dt
    if hoje is None:
        hoje = _dt.date.today()

    data = payload.get("data") or {}
    attrs = dict(data.get("attributes") or {})

    def _parse(s):
        if not s:
            return None
        try:
            return _dt.date.fromisoformat(str(s)[:10])
        except (ValueError, TypeError):
            return None

    def _aplicar_data(d: "_dt.date") -> None:
        attrs["admissao"] = d.isoformat()
        dias = int(attrs.get("diascontratoexperiencia") or 30)
        attrs["dataterminocontrato"] = (d + _dt.timedelta(days=dias)).isoformat()

    admissao = _parse(attrs.get("admissao"))
    aso = _parse(attrs.get("dataatestadoocupacional"))

    if admissao is None:
        if aso is None:
            if usar_atual_se_invalida:
                _aplicar_data(hoje)
                admissao = hoje
            else:
                return payload, (
                    "Não consegui identificar a data de admissão nem a data "
                    "do exame admissional (ASO). Pode informar uma delas?"
                )
        else:
            admissao = aso + _dt.timedelta(days=1)
            _aplicar_data(admissao)

    # Validação: admissão no passado → colapsa pra hoje e deixa a regra
    # do horário do email deslocar (+1 / +2 dias úteis, skip FDS).
    # Antes a gente bloqueava com erro de pendência, mas o escritório quer
    # que a regra de deslocamento valha tanto pra hoje quanto pra datas
    # retroativas (caso real: MARIA ANTONIA admitida em 02/06 com email do
    # cliente chegando em 10/06 — DP não quer pedir nova data, quer subir
    # com a data ajustada automaticamente). Mudança 10/06/2026.
    #
    # Datas muito velhas (>30 dias) ainda passam, mas com warning de
    # auditoria — podem indicar erro de extração ou ASO obsoleto.
    if admissao < hoje:
        dias_atraso = (hoje - admissao).days
        if dias_atraso > 30:
            log.warning(
                f"   📅 admissão retroativa em {dias_atraso} dias "
                f"({admissao.isoformat()} → colapsando pra hoje {hoje.isoformat()}) — "
                f"verificar se não é erro de extração ou ASO obsoleto"
            )
        admissao = hoje
        _aplicar_data(hoje)

    # NOVA REGRA: se admissão == hoje (inclusive após colapsar retroativa),
    # aplica deslocamento baseado em horário do email + skip FDS pra próxima terça
    admissao_ajustada = _ajustar_admissao_se_hoje(admissao, data_email_dt, hoje)
    if admissao_ajustada != admissao:
        admissao = admissao_ajustada
        _aplicar_data(admissao)

    out = dict(payload)
    out["data"] = dict(data)
    out["data"]["attributes"] = attrs
    return out, None


def validar_campos_obrigatorios(
    payload: dict,
    ignorar_rels: set[str] | None = None,
    ignorar_attrs: set[str] | None = None,
) -> list[str]:
    """Verifica os campos exigidos pelo eContador. Retorna labels faltantes.

    Lista vazia → pode postar. Lista não-vazia → pendência.

    Regras especiais:
      - `primeiroemprego` é bool: só precisa existir (True ou False valem)
      - `salario` precisa ser > 0
      - `diascontratoexperiencia` precisa ser > 0
      - relationships: precisa ter `.data.id` não-vazio

    v2.16.5: `ignorar_rels` permite pular validação de relationships que foram
    intencionalmente omitidas pelo `finalizar_payload`. Caso real:
    `departamento` é omitido quando empresa tem 0 deptos cadastrados E
    `postar_sem_departamento_quando_vazio=True`.

    v2.16.16: `ignorar_attrs` faz o mesmo pra attributes. Caso real:
    `admissao` é omitida quando `sempre_mandar_sem_data_admissao=True` e
    o Claude não conseguiu extrair — DP digita no Alterdata Desktop.
    """
    faltando: list[str] = []
    data = payload.get("data") or {}
    attrs = data.get("attributes") or {}
    rels = data.get("relationships") or {}
    ignorar = set(ignorar_rels or ())
    ignorar_a = set(ignorar_attrs or ())

    for k in ATTRS_OBRIGATORIOS:
        if k in ignorar_a:
            continue
        v = attrs.get(k)
        if k == "primeiroemprego":
            if v is None:
                faltando.append(LABELS_AMIGAVEIS.get(k, k))
            continue
        if k in ("salario", "diascontratoexperiencia"):
            try:
                if v is None or float(v) <= 0:
                    faltando.append(LABELS_AMIGAVEIS.get(k, k))
            except (TypeError, ValueError):
                faltando.append(LABELS_AMIGAVEIS.get(k, k))
            continue
        if v in (None, "", [], {}):
            faltando.append(LABELS_AMIGAVEIS.get(k, k))

    for k in RELS_OBRIGATORIOS:
        if k in ignorar:
            continue
        rel = rels.get(k) or {}
        rel_data = rel.get("data") or {}
        rid = rel_data.get("id")
        if not rid or str(rid).strip() in ("", "0", "None"):
            faltando.append(LABELS_AMIGAVEIS.get(k, k))

    return faltando


def _so_digitos(s: Any) -> str:
    return re.sub(r"\D", "", str(s or ""))


def _ensure_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    s = _so_digitos(v)
    return int(s) if s else None


"""Limites de tamanho conhecidos da API eContador (descobertos via HTTP 422).
Truncamos antes do POST pra evitar `javax.validation.constraints.Size`.
"""
MAX_LEN_ATTRS: dict[str, int] = {
    "orgaoemissoridentidade": 9,   # confirmado 28/05/2026 — sigla curta tipo "SSP/PE"
    "orgaoemissorcnh": 9,           # mesmo padrão
    "seriectps": 5,                 # CTPS série tem 3-5 dígitos
    "identidade": 15,               # v2.16.24: confirmado HTTP 422 19/06/2026 — RG limite Size(0,15)
    "complemento": 40,              # confirmado 30/05/2026 — endereço (Q. X L. Y APTO 101)
    "rua": 60,                      # observado: textos longos do ViaCEP cortam aqui
    "bairro": 50,
    "cidade": 50,
    "nome": 100,                    # limite seguro pra nomes completos
    "nomedamae": 100,
    "nomedopai": 100,
    "municipionascimento": 50,
    "observacao": 200,              # observações livres
    "apelido": 30,
    "nomesocial": 80,
}

# Campos de endereço (string) que NÃO aceitam pontuação no eContador.
# Removemos vírgula/ponto/barra/2-pontos/ponto-vírgula. Hífens e apóstrofos
# são preservados (aparecem em nomes de ruas reais tipo "Av. José-Maria").
ATTRS_ENDERECO_SEM_PONTUACAO = ("rua", "bairro", "cidade", "complemento")
_RE_PONTUACAO_ENDERECO = re.compile(r"[,.;:/\\]+")


def _limpar_pontuacao_endereco(s: str) -> str:
    """Remove pontuação típica que o eContador rejeita em campos de endereço.
    Colapsa espaços resultantes. Exemplo:
        'RUA OURO PRETO, Q. 113, L. 02'  →  'RUA OURO PRETO Q 113 L 02'
    """
    limpo = _RE_PONTUACAO_ENDERECO.sub(" ", s)
    return re.sub(r"\s+", " ", limpo).strip()


def _normalizar_telefone(v) -> str | None:
    """Normaliza celular/telefone pra 12-13 DÍGITOS PUROS com prefixo Brasil (55).

    v2.14.1 (PATCHES.md §4.2): eContador valida `Size(min=12, max=13)` mas
    rejeita formato com parênteses em alguns endpoints — observado em HTTP 422
    reais (12/06). A regra agora é 100% dígitos:

      - 10-11 dígitos (DDD + número, sem DDI)  → prefixa 55 → 12-13 dígitos
      - 12-13 dígitos (já tem DDI 55 ou similar) → mantém
      - 14+ dígitos (DDI duplicado, "+55(62)...") → tenta limpar até caber
      - <10 dígitos                               → None (omite — é opcional)

    Formato final: '5562999990000' (13 dígitos) ou '556232310000' (12 dígitos).
    Antes (até v2.13.x): '(DD)NNNNNNNN(N)' — mantido só em comentário porque o
    template do briefing ainda mostra esse formato; o eContador aceita ambos
    como Size mas o flat dígito tem ZERO 422 reportado.
    """
    if not v:
        return None
    digits = re.sub(r"\D", "", str(v))
    if 10 <= len(digits) <= 11:
        digits = "55" + digits  # prefixa código do Brasil
    if 12 <= len(digits) <= 13:
        return digits
    return None  # fora do range — omite (é opcional no eContador)


def sanitizar_attributes(attrs: dict) -> dict:
    """Aplica regras críticas de payload (CPF int, numero=0, etc.)."""
    out = dict(attrs)

    # v2.16.25: endereço como string única vira campos separados.
    # Caso real recorrente: Claude manda attrs.endereco="RUA X, BAIRRO Y,
    # CIDADE Z - UF, CEP 00000-000" em vez de cep/rua/bairro/cidade
    # separados. eContador não tem campo "endereco" → cep/rua/bairro/cidade
    # ficavam vazios → toda pendência aparecia com endereço pra DP preencher
    # mesmo o Claude tendo extraído tudo. Agora é idempotente em todo
    # payload, não importa o caminho (raiz-pendente, bloco, validação).
    try:
        from endereco_utils import expandir_endereco_string_em_attrs
        preenchidos = expandir_endereco_string_em_attrs(out)
        if preenchidos:
            log.info(
                f"[payload] Endereço string parseada → "
                f"{', '.join(preenchidos)} preenchidos"
            )
    except Exception as e:
        log.warning(f"[payload] Falha parseando endereço string: {e}")

    # v2.16.30/31: limpa SENTINELAS string em campos de endereço/contratuais.
    # Caso real Gabriel (2026-06-22): Claude colocou
    # 'NAO_LOCALIZADO — EMAIL MENCIONA FAZENDA BEM TI VI SEM CEP/RUA'
    # no campo `rua` (texto explicativo em vez de vazio). UI mostrava esse
    # texto enorme no input, eContador rejeitava com 422. v2.16.31 melhora
    # a detecção pra cobrir variações: underscore, hífen, espaço, palavras
    # similares ("não foi possível", "não foi extraído", etc).
    SENTINELAS_KEYWORDS = (
        "localizado", "informado", "consta", "extraido", "extraído",
        "fornecido", "fornecida", "encontrado", "encontrada",
        "identificado", "identificada", "preenchido", "preenchida",
        "a combinar", "a confirmar", "verificar", "pendente",
        "email menciona", "documento nao", "documento não", "sem dado",
    )
    # Frases curtas que SOZINHAS já são sentinela (sem precisar de "nao")
    SENTINELAS_FRASES = (
        "a combinar", "a confirmar", "n/a", "(vazio)", "(vazia)",
        "indefinido", "indefinida",
    )
    def _eh_sentinela(v: str) -> bool:
        s = v.lower().strip().replace("_", " ")
        # Frase sentinela curta
        if any(f in s for f in SENTINELAS_FRASES):
            return True
        # Padrão "nao/não <palavra-chave>" (ex: "nao localizado")
        if any(k in s for k in ("nao ", "não ", "n/a")):
            return any(kw in s for kw in SENTINELAS_KEYWORDS)
        return False

    for campo_texto in ("rua", "bairro", "cidade", "complemento",
                          "nomedamae", "nomedopai", "nomecargo",
                          "orgaoemissoridentidade", "municipionascimento"):
        v = out.get(campo_texto)
        if v and isinstance(v, str):
            if _eh_sentinela(v) or len(v) > 80:
                log.info(
                    f"[payload] Sentinela removida em '{campo_texto}': "
                    f"{v[:60]!r}..."
                )
                out.pop(campo_texto, None)

    # v2.16.16: diascontratoexperiencia=30 default obrigatório (CLAUDE.md
    # convenções §3.4). UI eContador calcula prorrogacao=90-30=60 quando
    # abre o candidato → resultado final é CLT padrão 30+60. Antes esse
    # default só era setado em `aplicar_data_admissao` quando a data vinha;
    # sem data, sumia → validador derrubava como pendente. Agora é incondicional.
    try:
        d = int(out.get("diascontratoexperiencia") or 0)
        if d <= 0:
            out["diascontratoexperiencia"] = 30
    except (TypeError, ValueError):
        out["diascontratoexperiencia"] = 30

    # CPF como inteiro (Java rejeita string)
    if "cpf" in out:
        cpf = _ensure_int(out["cpf"])
        if cpf is not None:
            out["cpf"] = cpf
        else:
            out.pop("cpf", None)

    # numero do endereço como int (0 se ausente — briefing seção 3.2)
    if "numero" in out:
        num = _ensure_int(out["numero"])
        out["numero"] = num if num is not None else 0
    else:
        # Se tem rua/cep mas não tem numero, força 0
        if any(k in out for k in ("rua", "cep")):
            out["numero"] = 0

    # ctps como int
    if "ctps" in out:
        ctps = _ensure_int(out["ctps"])
        if ctps is not None:
            out["ctps"] = ctps
        else:
            out.pop("ctps", None)

    # v2.16.42: normaliza TODOS os campos de data pra ISO 8601 (YYYY-MM-DD).
    # eContador é um backend Java que rejeita com HTTP 500
    # "java.time.LocalDate from String 'DD/MM/YYYY'" quando vem qualquer
    # outro formato. Casos reais:
    #   - WILDA ROSA (24/06/2026): dataidentidade='08/09/1988' → 500
    #   - Claude às vezes copia data do RG sem converter pra ISO
    #   - Operador digita data no form em formato BR
    # Aceita: ISO 'YYYY-MM-DD' (mantém), 'DD/MM/YYYY', 'DD-MM-YYYY',
    #   'YYYY/MM/DD'. Inválido → omite (evita HTTP 500).
    CAMPOS_DATA_ISO = (
        "admissao", "nascimento", "dataidentidade", "datactps", "datapis",
        "dataatestadoocupacional", "dataterminocontrato",
    )
    for campo_data in CAMPOS_DATA_ISO:
        v = out.get(campo_data)
        if v in (None, ""):
            continue
        s = str(v).strip()
        # Já é ISO?
        if len(s) >= 10 and s[4:5] == "-" and s[7:8] == "-" and s[:4].isdigit():
            out[campo_data] = s[:10]
            continue
        # BR DD/MM/YYYY ou DD-MM-YYYY
        m = re.match(r"^(\d{2})[/-](\d{2})[/-](\d{4})$", s)
        if m:
            d, mo, y = m.group(1), m.group(2), m.group(3)
            out[campo_data] = f"{y}-{mo}-{d}"
            log.info(f"[payload] Data BR convertida: {campo_data}={s!r} → {out[campo_data]!r}")
            continue
        # YYYY/MM/DD (separador errado)
        m = re.match(r"^(\d{4})[/](\d{2})[/](\d{2})$", s)
        if m:
            out[campo_data] = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
            continue
        # Formato não reconhecido → omite pra evitar HTTP 500
        log.warning(
            f"[payload] Data inválida em {campo_data}={s!r} — omitindo "
            f"(esperava YYYY-MM-DD, DD/MM/YYYY ou DD-MM-YYYY)"
        )
        out.pop(campo_data, None)

    # v2.16.24: identidade (RG) — eContador valida Size(0,15).
    # Caso real (19/06/2026): HTTP 422 javax.validation.constraints.Size em
    # /data/attributes/identidade — Claude extraiu "5.336.639" com pontos
    # (8 chars com pontos = OK, mas em outros casos pode passar de 15).
    # Sanitização: remove pontos, hífens, espaços. Preserva letras (alguns
    # RGs antigos têm letras tipo "MG-15.123.456" → "MG15123456").
    # Truncar a 15 é defesa final (MAX_LEN_ATTRS já aplica no final).
    if out.get("identidade") is not None:
        rg = str(out["identidade"]).strip()
        # Remove tudo que não é letra/dígito (pontos, hífens, espaços, vírgulas)
        rg_limpo = re.sub(r"[^A-Za-z0-9]", "", rg)
        if rg_limpo:
            out["identidade"] = rg_limpo[:15]  # limite eContador
        else:
            out.pop("identidade", None)

    # tituloeleitor: eContador valida como long Max(999.999.999.999 = 12 dígitos).
    # v2.14.1 (PATCHES.md §4.2): convertemos pra INT — alguns endpoints rejeitam
    # string mesmo com Size válido (HTTP 422 javax.validation.constraints.Max
    # observado quando vinha string). Claude às vezes extrai a inscrição com
    # zona/seção concatenadas (>12 dígitos) ou com pontuação — normaliza pra
    # dígitos e omite se exceder (DP preenche manual).
    if out.get("tituloeleitor") is not None:
        te_str = _so_digitos(out["tituloeleitor"])
        if te_str and 1 <= len(te_str) <= 12:
            try:
                out["tituloeleitor"] = int(te_str)
            except ValueError:
                out.pop("tituloeleitor", None)
        else:
            out.pop("tituloeleitor", None)

    # zonatituloeleitor e secaotituloeleitor: máximo 4 dígitos cada
    for campo in ("zonatituloeleitor", "secaotituloeleitor"):
        v = out.get(campo)
        if v is None:
            continue
        d = _so_digitos(v)
        if 1 <= len(d) <= 4:
            out[campo] = d
        else:
            out.pop(campo, None)

    # email em lowercase
    if isinstance(out.get("email"), str):
        out["email"] = out["email"].strip().lower()

    # celular/telefone: eContador valida Size(min=12, max=13) — '(DD)NNNNNNNN(N)'.
    # Normaliza dos formatos comuns ('62 99999-0000', '62999990000') pra
    # o formato canônico. Omite se não der pra normalizar (sem DDD, etc.).
    for campo in ("celular", "telefone"):
        if campo in out:
            normalizado = _normalizar_telefone(out[campo])
            if normalizado:
                out[campo] = normalizado
            else:
                log.warning(
                    f"   ⚠ {campo}={out[campo]!r} não normalizou pra (DD)NNNNNNNN — omitindo"
                )
                out.pop(campo, None)

    # Endereço sem pontuação — eContador rejeita vírgula/ponto/barra
    # em rua/bairro/cidade/complemento. Aplicado ANTES do truncamento.
    for campo in ATTRS_ENDERECO_SEM_PONTUACAO:
        v = out.get(campo)
        if isinstance(v, str) and v:
            limpo = _limpar_pontuacao_endereco(v)
            if limpo != v:
                out[campo] = limpo

    # Limites de tamanho do eContador — trunca strings longas pra evitar HTTP 422
    # com javax.validation.constraints.Size. Mantém a parte mais informativa
    # (início da string) — ex: "SSP/PE - Secretaria..." vira "SSP/PE - S"[:9]
    for campo, maxlen in MAX_LEN_ATTRS.items():
        v = out.get(campo)
        if isinstance(v, str) and len(v) > maxlen:
            out[campo] = v.strip()[:maxlen].strip()

    # v2.14.1 (PATCHES.md §4.2): defesa final do `complemento` — operadores
    # às vezes injetam complementos longos via UI (form do "Resolver pendência")
    # que bypassam a tabela MAX_LEN_ATTRS quando o campo já foi truncado.
    # Reforço idempotente: garante que o que sai daqui CABE em 40 chars.
    if out.get("complemento"):
        out["complemento"] = str(out["complemento"])[:40]

    # Remove None/"" — bug 9: datas null viram 30/12/1899 no Desktop
    return {k: v for k, v in out.items() if v not in (None, "", [], {})}


def finalizar_payload(
    payload_claude: dict,
    empresa_id: str,
    departamento_id: str | None,
    funcao_id: str,
) -> dict:
    """Recebe o JSON do Claude e produz o payload final pra POST /candidatos.

    `payload_claude` pode ter formato:
      {"cnpj_empresa": "...", "departamento_sugerido": "...",
       "data": {"type": "candidatos", "attributes": {...}, "relationships": {...}}}
    """
    if "data" not in payload_claude:
        raise ValueError("Payload do Claude não tem chave 'data' no nível raiz")

    data = dict(payload_claude["data"])
    data["type"] = "candidatos"

    # Attributes
    attrs = sanitizar_attributes(dict(data.get("attributes") or {}))
    data["attributes"] = attrs

    # Relationships — substitui IDs resolvidos
    rels = dict(data.get("relationships") or {})
    rels["empresa"] = {"data": {"type": "empresas", "id": str(empresa_id)}}
    # v2.15.14: quando funcao_id é None/vazio, OMITE a relationship em vez
    # de mandar id=1 (que no eContador é uma função REAL — "ASSISTENTE DE
    # CPD" no caso do user — e confunde o DP no Desktop). Sem função no
    # payload, DP escolhe manualmente no Desktop. Se o eContador rejeitar
    # com 422, marcar funcao_id explícito (não chegou a confirmar com cobaia).
    if funcao_id and str(funcao_id).strip():
        rels["funcao"] = {"data": {"type": "funcoes", "id": str(funcao_id)}}
    else:
        rels.pop("funcao", None)
        log.info("[payload] funcao omitida — DP escolhe no Alterdata Desktop")
    # v2.14.1 (PATCHES.md §4.1): SEM_DEPARTAMENTO é a sentinela que o
    # resolver_departamento devolve quando a empresa tem 0 deptos no eContador
    # E a flag postar_sem_departamento_quando_vazio=true. Nesse caso a
    # relationship `departamento` é OMITIDA INTEIRA do payload (DP atribui
    # no Desktop). Resolve 6 das 11 pendências abertas em 12/06.
    if departamento_id == SEM_DEPARTAMENTO:
        rels.pop("departamento", None)
        log.info(
            "Payload sem relationship 'departamento' (REGRA 0 — empresa "
            "sem deptos no eContador, DP atribui no Desktop)"
        )
    elif departamento_id:
        rels["departamento"] = {
            "data": {"type": "departamentos", "id": str(departamento_id)}
        }
    else:
        rels.pop("departamento", None)
        log.warning("Payload sem departamento — DP precisa preencher manual no Desktop")

    data["relationships"] = rels

    return {"data": data}


def normalizar_admissoes(resposta_claude: dict) -> list[dict]:
    """Normaliza a resposta do Claude pra uma lista de blocos de admissão.

    Formatos aceitos:
      A. MULTI: {"cnpj_empresa": X, "admissoes": [{...}, {...}]}
      B. SINGLE legacy: {"cnpj_empresa": X, "data": {...}, "departamento_sugerido": Y}

    Retorna sempre uma lista de blocos no formato single, com cnpj_empresa
    propagado do raiz pra cada bloco se faltar:
      [{"cnpj_empresa": X, "departamento_sugerido": Y, "data": {...}}, ...]

    Resposta vazia/inválida → [].
    Resposta com _pendente=true → [] (caller trata).
    """
    if not isinstance(resposta_claude, dict):
        return []
    if resposta_claude.get("_pendente"):
        return []

    cnpj_raiz = resposta_claude.get("cnpj_empresa")
    depto_raiz = resposta_claude.get("departamento_sugerido")

    admissoes = resposta_claude.get("admissoes")
    if isinstance(admissoes, list) and admissoes:
        out: list[dict] = []
        for bloco in admissoes:
            if not isinstance(bloco, dict):
                continue
            # Blocos individuais com _pendente=true (Claude desistiu daquele
            # funcionário específico) PRECISAM ser preservados pra virarem
            # pendência cliente no caller — em vez de sumirem silenciosamente.
            # _dados_parciais é injetado em "data.attributes" pra o caller
            # reaproveitar nome/cpf na planilha.
            if bloco.get("_pendente") and "data" not in bloco:
                dp = bloco.get("_dados_parciais") or {}
                bloco_sintetico = dict(bloco)
                bloco_sintetico["data"] = {
                    "type": "candidatos",
                    "attributes": dict(dp),
                }
                if cnpj_raiz and not bloco_sintetico.get("cnpj_empresa"):
                    bloco_sintetico["cnpj_empresa"] = cnpj_raiz
                out.append(bloco_sintetico)
                continue
            if "data" not in bloco:
                continue
            b = dict(bloco)
            # Propaga cnpj/depto se o bloco não trouxer
            if cnpj_raiz and not b.get("cnpj_empresa"):
                b["cnpj_empresa"] = cnpj_raiz
            if depto_raiz and not b.get("departamento_sugerido"):
                b["departamento_sugerido"] = depto_raiz
            out.append(b)
        return out

    # Single legacy
    if "data" in resposta_claude:
        return [resposta_claude]

    return []


def extrair_dados_consulta(payload_claude: dict) -> dict:
    """Extrai os campos top-level que o pipeline usa pra resolver IDs.

    Retorna: {cnpj_empresa, departamento_sugerido, cargo, cbo}
    """
    cnpj = _so_digitos(payload_claude.get("cnpj_empresa"))
    if not cnpj:
        # Fallback: alguns formatos podem trazer dentro de attributes
        attrs = (payload_claude.get("data") or {}).get("attributes") or {}
        cnpj = _so_digitos(attrs.get("cnpj_empresa") or attrs.get("cnpj"))

    cargo = (
        payload_claude.get("cargo_extraido")
        or (payload_claude.get("data") or {}).get("attributes", {}).get("nomecargo")
    )

    return {
        "cnpj_empresa": cnpj,
        "departamento_sugerido": payload_claude.get("departamento_sugerido"),
        "cargo": cargo,
        "cbo": _so_digitos(payload_claude.get("cbo_sugerido")),
        "pendente": bool(payload_claude.get("_pendente")),
        "motivo_pendencia": payload_claude.get("_motivo"),
    }
