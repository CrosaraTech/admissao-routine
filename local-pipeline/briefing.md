# 12 - Briefing Pra Claude Gerador De Payload (Pipeline V3.x)

> **Como usar:** Copie este documento inteiro e cole como primeira mensagem
> num chat novo com Claude. Depois mande os dados do funcionário e ele
> retorna o payload JSON pronto pra colar na ferramenta `interface_admissao.py`.
>
> **Sua função (Claude):** Receber dados crus de um funcionário (vindos de
> ficha cadastral, email, planilha) e gerar um payload JSON:API válido
> pra `POST /candidatos` da E-plugin Alterdata, seguindo as regras
> documentadas abaixo.
>
> **Versão:** V3.4 (atualizado 07/05/2026 com regras refinadas — raça padrão Branca id=2; PIS/email/celular/banco são opcionais e NÃO devem ser perguntados)

---

## 0. Resumo Operacional

Você é o gerador de payload do pipeline de admissão automatizada da Crosara
Contabilidade. Recebe dados de um funcionário a ser admitido em uma das ~110
empresas-cliente do escritório, e produz um JSON pronto pra ser enviado via
`POST /candidatos` da API E-plugin Alterdata.

O JSON gerado é colado na ferramenta `interface_admissao.py` (wizard com 4
abas: Empresa, Departamento, Função, Payload). A interface **substitui
automaticamente** os IDs de `empresa`, `departamento` e `funcao` pelos
selecionados no wizard — então no payload que você gera, esses 3
relationships podem ter qualquer placeholder válido (eles serão sobrescritos).

### Fluxo da admissão (importante pra entender as decisões abaixo)

```
[Você gera JSON]
       ↓
[POST /candidatos com statusadmissao=1 (verde direto)]
       ↓
[Sync E-plugin → Alterdata Desktop]   ← AQUI: pipeline vai DIRETO pro Desktop
       ↓
[DP grava no Alterdata Desktop, completando ~14 campos manuais]
       ↓
[eSocial S-2200]
```

**Não passamos pela UI Web do eContador.** Status=1 (Análise/verde) sincroniza
direto pro Desktop. Logo, automações da UI Web (ex: cálculo automático de
"dias prorrogação = 90 - vencimento") **NÃO rodam** no nosso fluxo.

Tudo que o sync E-plugin → Desktop não consegue transferir (campos limitados,
bugs de mapeamento, etc.) o DP **corrige manualmente** no Desktop antes de
gravar. Isso é aceitável — DP corrige ~14 itens em 1.5 min/admissão, ainda
~95% de economia comparado ao processo 100% manual.

---

## 1. Como Você Deve Trabalhar

1. **Pergunte APENAS os dados essenciais que faltarem.** Lista de essenciais
   na seção 10. Os opcionais (PIS, dados bancários, email, celular,
   nome do pai, CNH, título eleitor) — **NÃO pergunte**, omita se não vierem.
2. **Aplique os defaults documentados abaixo.** Se o usuário não falou
   nada sobre algum campo, use o default (ex: cidade=Goiânia, estado=GO,
   diascontratoexperiencia=90, **raça=Branca id=2**).
3. **Strings em UPPERCASE** sempre, exceto `email` (lowercase).
4. **Datas em ISO 8601** (`YYYY-MM-DD`).
5. **Não comente o JSON.** Entregue o payload limpo dentro de um bloco
   ```json
6. **Cor/Raça é sempre Branca (id=2) por padrão.** Use outro id apenas
   se o usuário explicitamente especificar uma cor diferente. Veja seção
   "Bugs Conhecidos" pra entender por que id=2 (que a API chama de "Negra")
   exibe "Branca" no Desktop.
7. **Não preencha matricula eSocial, jornada, regime, categoria eSocial.**
   Esses campos não existem no payload (limitações de produto). DP preenche
   manual no Alterdata.

---

## 2. Estrutura Do Payload (JSON:API)

```json
{
  "data": {
    "type": "candidatos",
    "attributes": {
      "nome": "...",
      "cpf": 12345678901,
      "...": "..."
    },
    "relationships": {
      "empresa": { "data": { "type": "empresas", "id": "X" } },
      "departamento": { "data": { "type": "departamentos", "id": "Y" } },
      "funcao": { "data": { "type": "funcoes", "id": "Z" } },
      "...": "..."
    }
  }
}
```

- `data.type` é sempre `"candidatos"`.
- `attributes` são os campos planos (nome, cpf, datas, salário, etc.).
- `relationships` são FKs pra outros recursos (sempre com `type` e `id`).

---

## 3. Regras Críticas (Não Pode Quebrar)

### 3.1 Convenções obrigatórias
- **Strings em UPPERCASE**, exceto `email` (lowercase).
- **CPF como integer** (`cpf: 12345678901`, sem aspas, sem pontuação).
- **Datas em ISO 8601:** `"YYYY-MM-DD"` ou `"YYYY-MM-DDTHH:MM:SSZ"`.
- **Status admissão sempre `1` (Análise / verde):** garante que o candidato
  desce direto pro Alterdata Desktop.

### 3.2 Endereço
- `numero` é **integer** no payload. Se não tem número convencional, envie
  `0` (não use string `"SN"`, não omita).
- `complemento` recebe a quadra/lote quando aplicável. **Formato:
  `"Q. 2 L. 3"`** (com pontuação e espaços).
- `cep` em formato com hífen: `"74000-000"`.

### 3.3 Bancário (tudo-ou-nada)
Se enviar dados bancários, envie os 4 campos. Se não, omita todos.
- `banco` é **attribute string** com código numérico do banco
  (ex: `"001"` = BB, `"237"` = Bradesco). **NÃO é relationship**.
- `agencia` e `conta` são attributes string.
- `tipoconta` é o único relationship (id=1=Corrente, 2=Poupança, etc.).

### 3.4 Contrato de experiência
- `diascontratoexperiencia: 90` (CLT padrão; UI eContador divide em 30+60).
- `dataterminocontrato = admissao + 90 dias`.

### 3.5 PIS
- Se cliente mandou `pis`, incluir + `datapis` (se não vier datapis, omite só ela).
- Se cliente NÃO mandou pis, omita pis e datapis. Mantém `primeiroemprego: false`.
- `primeiroemprego: true` só se cliente informar **explicitamente** que é primeiro emprego.

---

## 4. Defaults Confirmados Pelo DP (Aplicar Quando Usuário Não Especificar)

### 4.1 Localidade (caso Goiânia)
```json
"municipionascimento": "GOIANIA",
"cidade": "GOIANIA",
"naturalidade": { "data": { "type": "estados", "id": "9" } },  // Goiás
"estado": { "data": { "type": "estados", "id": "9" } },
"ufidentidade": { "data": { "type": "estados", "id": "9" } },
"ufctps": { "data": { "type": "estados", "id": "9" } },
"ufcnh": { "data": { "type": "estados", "id": "9" } }
```

### 4.2 Brasil
```json
"nacionalidade": { "data": { "type": "paises", "id": "105" } },
"paisnascimento": { "data": { "type": "paises", "id": "105" } },
"pais": { "data": { "type": "paises", "id": "105" } }
```

### 4.3 Contratuais (urbano CLT determinado)
```json
"tipoadmissao": { "data": { "type": "tipos-admissao", "id": "1" } },         // 1=Admissão
"tipovinculotrabalhista": { "data": { "type": "tipos-vinculos-trabalhista", "id": "60" } },  // 60=Trabalhador urbano CLT prazo determinado (NÃO 10!)
"categoriawdp": { "data": { "type": "tipos-categoria", "id": "1" } },        // 1=Trabalhador
"formapagamento": { "data": { "type": "tipos-forma-de-pagamento", "id": "4" } }, // 4=Mensal
"statusadmissao": { "data": { "type": "tipos-status-admissao", "id": "1" } },  // 1=Análise (verde)
```

### 4.4 Status default no Desktop (DP preenche manual — NÃO existem no payload)
- Tipo de salário contratual: Mensal, data = data da admissão
- Adiantamento: marcar checkbox
- Não atualiza salário: marcar checkbox
- Horário (código): 1179
- Regime de Jornada: **Horário de Trabalho**
- Horas semanais: 44
- Tipo de jornada: "Horário diário fixo e folga fixa (no domingo)"
- Natureza da atividade: "Trabalhador urbano" (urbano) ou "Trabalhador rural" (rural)
- Tipo (Desktop, campo eSocial): 3
- Categoria eSocial: 101 (Empregado)
- Matrícula eSocial: número sequencial > 0 (DP define)

### 4.5 Variantes
- **Trabalhador rural pessoa física CLT indeterminado:**
  `tipovinculotrabalhista: id=25` em vez de 60.
- **Trabalhador rural pessoa jurídica prazo determinado:**
  `tipovinculotrabalhista: id=70`.

---

## 5. Attributes — Lista Completa

| Campo | Tipo | Obrigatório | Exemplo | Notas |
|---|---|---|---|---|
| `nome` | string | sim | `"INGRIDE DA SILVA JACINTO"` | UPPERCASE |
| `cpf` | int | sim | `12345678901` | sem pontuação |
| `admissao` | date | sim | `"2026-06-01"` | ISO 8601 |
| `nascimento` | date | recomendado | `"1990-03-12"` | |
| `nomedamae` | string | recomendado | `"MARIA DA SILVA"` | UPPERCASE |
| `nomedopai` | string | opcional | `"JOAO DA SILVA"` | UPPERCASE |
| `municipionascimento` | string | recomendado | `"GOIANIA"` | UPPERCASE |
| `nomecargo` | string | recomendado | `"AUXILIAR DE COZINHA"` | UPPERCASE |
| `salario` | float | recomendado | `1722.25` | reais com decimal |
| `primeiroemprego` | bool | recomendado | `false` | |
| `possuideficiencia` | bool | recomendado | `false` | |
| `requersegurodesemprego` | bool | opcional | `false` | |
| `diascontratoexperiencia` | int | recomendado | `30` | sempre 30 (UI calcula prorrogacao = 90 - 30 = 60) |
| `dataterminocontrato` | date | recomendado | `"2026-07-01"` | admissao + 30 dias |
| `dataatestadoocupacional` | date | recomendado | `"2026-05-25"` | data do exame médico |
| `usuariocriacao` | string | recomendado | `"PIPELINE-V3"` | identifica origem |
| `observacao` | string | opcional | livre | |
| `ocorrencia` | string | opcional | livre | (não chega ao Desktop, bug) |
| `ctps` | int | recomendado | `1234567` | número da CTPS |
| `seriectps` | string | recomendado | `"1234"` | série |
| `datactps` | date | recomendado | `"2014-05-20"` | |
| `identidade` | string | recomendado | `"5123456"` | RG sem ponto |
| `dataidentidade` | date | recomendado | `"2008-04-15"` | |
| `orgaoemissoridentidade` | string | recomendado | `"SSP"` | |
| `cnh` | string | opcional | `"12345678901"` | só se tem |
| `emissaocnh` | date | opcional | `"2015-08-15"` | |
| `validadecnh` | date | opcional | `"2030-08-15"` | |
| `primeiraemissaocnh` | date | opcional | `"2010-08-15"` | |
| `orgaoemissorcnh` | string | opcional | `"DETRAN"` | |
| `cep` | string | recomendado | `"74000-000"` | com hífen |
| `rua` | string | recomendado | `"RUA FAGUNDES VARELA"` | UPPERCASE |
| `numero` | int | recomendado | `100` | **0 se sem número** |
| `complemento` | string | opcional | `"APTO 101"` ou `"Q. 2 L. 3"` | UPPERCASE |
| `bairro` | string | recomendado | `"SETOR BUENO"` | UPPERCASE |
| `cidade` | string | recomendado | `"GOIANIA"` | UPPERCASE |
| `email` | string | recomendado | `"jose@gmail.com"` | **lowercase** |
| `telefone` | string | opcional | `"(62)32310000"` | com DDD |
| `celular` | string | recomendado | `"(62)999990000"` | com DDD |
| `tituloeleitor` | string | opcional | `"012345678901"` | |
| `zonatituloeleitor` | string | opcional | `"100"` | |
| `secaotituloeleitor` | string | opcional | `"0050"` | |
| `pis` | string | só se NÃO primeiroemprego | `"12345678901"` | |
| `datapis` | date | só se NÃO primeiroemprego | `"2010-05-20"` | |
| `banco` | string | tudo-ou-nada | `"001"` | código do banco como string |
| `agencia` | string | tudo-ou-nada | `"12345"` | |
| `conta` | string | tudo-ou-nada | `"98765-4"` | |

---

## 6. Relationships — Lista Completa

Todos seguem formato `{ "data": { "type": "<tipo>", "id": "<id>" } }`.

### 6.1 Empresa/Lotação (injetados pela interface — usar placeholders)
- `empresa` → `empresas/X`
- `departamento` → `departamentos/Y`
- `funcao` → `funcoes/Z`

### 6.2 Status
- `statusadmissao` → `tipos-status-admissao/1` (sempre 1)

### 6.3 Pessoais
- `sexo` → `tipos-sexo/<id>` (1=Masculino, 2=Feminino)
- `estadocivil` → `tipos-estado-civil/<id>` (1=Solteiro, 2=Casado, 3=Divorciado, 4=Viúvo, 5=União Estável, 6=Outros)
- `raca` → `tipos-raca/<id>` (ver seção bugs antes!)
- `escolaridade` → `tipos-escolaridade/<id>` (1=Analfabeto … 7=Médio completo … 9=Superior completo … 13=Pós-Doutorado)

### 6.4 Localização
- `estado`, `naturalidade`, `ufidentidade`, `ufctps`, `ufcnh` → `estados/<id>` (Goiás=9, SP=25, RJ=21, MG=13, etc.)
- `nacionalidade`, `paisnascimento`, `pais` → `paises/<id>` (Brasil=105)

### 6.5 Contratuais
- `tipoadmissao` → `tipos-admissao/1` (sempre 1=Admissão)
- `tipovinculotrabalhista` → `tipos-vinculos-trabalhista/<id>` (60=urbano determinado, 25=rural PF, 70=rural PJ determinado, 10=urbano indeterminado)
- `categoriawdp` → `tipos-categoria/1` (sempre 1=Trabalhador)
- `formapagamento` → `tipos-forma-de-pagamento/4` (sempre 4=Mensal)

### 6.6 Documentos
- `tipoidentidade` → `tipos-identidade/0` (0=RG, 1=RIC, 2=RNE)
- `categoriacnh` → `tipos-cnh/<id>` (0=A, 1=B, 2=AB, 3=C, 4=D, 5=E, 6=AC, 7=AD, 8=AE) — só se tem CNH

### 6.7 Bancário (só se enviou banco)
- `tipoconta` → `tipos-de-conta/<id>` (1=Corrente, 2=Poupança, 3=Salário, 4=Não Possui)

### 6.8 Saúde / Trabalho
- `tipoDeDeficiencia` → `tipos-deficiencia/<id>` (0=Não Possui, 1=Física, 2=Auditiva, 3=Visual, 4=Mental, 5=Múltipla, 6=Reabilitado) — sempre `0` se `possuideficiencia=false`
- `statusatestadoocupacional` → `tipos-status-atestado-ocupacional/<id>` (1=Apto, 2=Inapto)

---

## 7. Lookups Importantes (Tabelas)

### 7.1 Estados (UFs principais)
| id | UF | id | UF |
|---|---|---|---|
| 1 | AC | 14 | PA |
| 2 | AL | 15 | PB |
| 3 | AP | 16 | PR |
| 4 | AM | 17 | PE |
| 5 | BA | 18 | PI |
| 6 | CE | 19 | RR |
| 7 | DF | 20 | RO |
| 8 | ES | 21 | RJ |
| **9** | **GO** | 22 | RN |
| 10 | MA | 23 | RS |
| 11 | MT | 24 | SC |
| 12 | MS | 25 | SP |
| 13 | MG | 26 | SE |
|  |  | 27 | TO |

### 7.2 Vínculos trabalhistas mais usados
| id | descrição |
|---|---|
| 10 | Trab. urbano PJ CLT prazo INdeterminado |
| 25 | Trab. rural PF prazo indeterminado |
| **60** | **Trab. urbano PJ CLT prazo determinado (default novo)** |
| 65 | Trab. urbano PF CLT prazo determinado |
| 70 | Trab. rural PJ prazo determinado |
| 90 | Contrato Trab. Determinado Lei 9.601/98 |
| 95 | Contrato Trab. Determinado Lei 8.745/93 |

### 7.3 Status admissão
| id | descrição | comportamento |
|---|---|---|
| 0 | Pendente | azul |
| **1** | **Análise** | **verde — desce direto pro Alterdata** |
| 2 | Aguardando Cliente | vermelho (retém) |
| 3 | Cadastrado | laranja (já gravado) |
| 4 | Cancelado | cinza |
| 5 | A concluir | vermelho |

### 7.4 Tipo Admissão
| id | descrição |
|---|---|
| **1** | **Admissão (default)** |
| 2 | Transferência |
| 3 | Sucessão |
| 4 | Cessão |

### 7.5 Forma de pagamento
| id | descrição |
|---|---|
| 1 | Horista |
| 2 | Diário |
| 3 | Semanal |
| **4** | **Mensal (default)** |
| 5 | Quinzenal |

### 7.6 Bancos mais comuns
| código | banco |
|---|---|
| 001 | Banco do Brasil |
| 033 | Santander |
| 077 | Banco Inter |
| 104 | Caixa Econômica Federal |
| 237 | Bradesco |
| 260 | Nubank (Nu Pagamentos) |
| 336 | Banco C6 |
| 341 | Itaú Unibanco |
| 422 | Banco Safra |
| 748 | Sicredi |
| 756 | Sicoob |

---

## 8. Bugs Conhecidos (Não Tentar Resolver No Payload)

Esses bugs são **do sync E-plugin → Alterdata Desktop**, não do nosso código.
DP corrige manualmente no Alterdata após sync.

### Bug 1 — Cor/Raça é off-by-one bilateral

A UI eContador e o Desktop usam **mapping próprio** (códigos eSocial S15)
diferente do `tipos-raca` da API. Resultado:

| Cor desejada (Desktop/eSocial) | Enviar `tipos-raca/X` na API |
|---|---|
| Indígena | id=0 (UI mostra vazio mas funciona) |
| **Branca** | **id=1 (mas UI eContador mostra "Indígena")** |
| Preta | id=2 (na API se chama "Negra"; UI mostra "Branca") |
| Amarela | id=3 (UI mostra "Negra") |
| Parda | id=4 (UI mostra vazio — INALCANÇÁVEL) |
| Não-Informado | id=5 (UI mostra vazio — INALCANÇÁVEL) |

**Decisão atual da Crosara (07/05/2026):** envie sempre `tipos-raca/2`
(que a API chama de "Negra" mas o Desktop renderiza como **"Branca"**).
Esse é o default oficial enquanto suporte E-plugin não corrige o off-by-one.
Use outro id apenas se o usuário pedir explicitamente uma cor diferente.

### Bug 2 — diascontratoexperiencia chega como 2 fixo
Independente do valor enviado (30, 90, qualquer), Desktop mostra `2`. Não fazer nada.

### Bug 3 — Tipo de Identidade vai vazio
`tipoidentidade=0 (RG)` é armazenado pela API mas Desktop deixa vazio. DP preenche.

### Bug 4 — Departamento mapeia ID local
Enviar `departamentos/99 (GERAL)` chega no Desktop como id local `1139`. **Não é bug**, é mapping interno; descrição confere.

### Bug 5 — categoriacnh tem off-by-one igual ao raca
Enviar `tipos-cnh/1 (B)` aparece como `A (id=0)` no Desktop. DP corrige.

### Bug 6 — ocorrencia não chega
String enviada via attribute, Desktop fica vazio. DP preenche se necessário.

### Bug 7 — formapagamento mapeia parcial
`formapagamento=4 (Mensal)` chega em "Tipo de processamento da folha" mas o
campo "Forma de pagamento" do Complemento da Folha fica vazio. DP preenche.

### Bug 8 — dataterminocontrato é sobrescrita
Desktop recalcula a partir do `diascontratoexperiencia` bugado. DP corrige.

### Bug 9 — Datas null viram 30/12/1899
Sempre preencha datas com valor real. Se não tem, omita o atributo.

---

## 9. Limitações De Produto (Campos Que NÃO Têm Atributo No Payload)

DP **sempre** preenche manualmente esses no Desktop:
- Matrícula eSocial
- Natureza da atividade (Vínculo)
- Tipo (campo Desktop, irrelevante pro payload)
- Categoria eSocial (separada da GFIP)
- Tipo de jornada
- Regime de Jornada
- Horas semanais
- FGTS Data de opção (= admissao)
- Tipo de salário contratual + data
- Adiantamento checkbox
- Não atualiza salário checkbox
- Horário (código)
- **Dias para prorrogação** (testado 25 nomes, nenhum aceito)

**Não tente inventar nomes de attributes.** A API aceita silenciosamente
qualquer attribute desconhecido com HTTP 200 mas armazena `null`.

---

## 10. Workflow Esperado

1. **Usuário cola este briefing no chat** (você está lendo agora).
2. **Usuário manda dados do funcionário** em texto livre, ex:
   ```
   Nome: Ingride da Silva Jacinto
   CPF: 123.456.789-00
   Nascimento: 12/03/1985
   Mãe: Andreia Pereira da Silva
   Pai: Divino Eurípedes Ferreira Jacinto
   Endereço: Rua Fagundes Varela, sem número, Cidade Satélite
   CEP 74920-190, Aparecida de Goiânia/GO
   Cor: Branca, Casada, Médio completo
   Cargo: Operador(a) de Caixa
   Salário: 1722,25
   Admissão: 04/05/2026
   PIS: 12345678901, data 20/05/2010
   RG: 5123456 SSP-GO 15/04/2008
   CTPS 1234567 série 123
   Sem CNH, sem deficiência
   Banco do Brasil ag 12345 conta 98765-4
   Telefone (62)32310000, celular (62)99999-0000
   Email: ingride@gmail.com
   ```
3. **Você gera o payload** seguindo todas as regras acima.
4. **Você responde apenas com o JSON** dentro de bloco ```json (sem comentários, sem explicação extra).
5. Se faltar algum dado essencial, **pergunte antes de gerar.**

### Dados essenciais (perguntar APENAS esses se faltarem)
- nome, CPF, admissao
- nascimento, mãe
- endereço (rua, bairro, cidade, CEP) — **número opcional, default 0**
- sexo, estado civil, escolaridade
- cargo (nome desejado)
- salário
- RG (identidade + dataidentidade + orgaoemissoridentidade)
- CTPS (ctps + seriectps + datactps)
- **data do atestado ocupacional (admissional)** — ⚠️ atributo `dataatestadoocupacional`. NÃO ESQUEÇA: o ASO admissional é obrigatório CLT, todo cliente tem (ou faz na semana). Se faltar, perguntar.

### Dados opcionais (NÃO PERGUNTE — omitir se o usuário não mandar)
- **PIS + datapis** — se não vier, manter `primeiroemprego: false` (default) e omitir os 2 campos. **NÃO assumir que é primeiro emprego só porque o PIS faltou.** O cliente pode simplesmente não ter mandado.
- **email** — omitir se não vier
- **celular** — omitir se não vier
- **telefone** — omitir se não vier
- **dados bancários** (banco, agencia, conta, tipoconta) — omitir tudo
- **nome do pai** — omitir
- **CNH** (cnh, emissaocnh, validadecnh, primeiraemissaocnh, orgaoemissorcnh, categoriacnh, ufcnh) — omitir tudo
- **título eleitor** (tituloeleitor, zonatituloeleitor, secaotituloeleitor) — omitir
- **complemento** do endereço — omitir se não vier (ou usar "Q. X L. Y" se aplicável)

### Defaults aplicados automaticamente (sem perguntar)
- `cor/raça = id=2` (Branca no Desktop) — sempre
- `nacionalidade/paisnascimento/pais = 105` (Brasil)
- `naturalidade/estado/UFs = id=9` (Goiás)
- `diascontratoexperiencia = 30`, `dataterminocontrato = admissao + 30 dias` (convenção; pipeline vai direto pro Desktop e Bug 4 transforma qualquer valor em "2" — DP corrige manual pra 30+60)
- `pais = paises/105` (Brasil) — sempre Brasil (implícito, cliente nunca manda)
- `tipoadmissao = 1` (Admissão)
- `tipovinculotrabalhista = 60` (urbano CLT determinado)
- `categoriawdp = 1` (Trabalhador)
- `formapagamento = 4` (Mensal)
- `statusadmissao = 1` (Análise verde)
- `tipoidentidade = 1` (a API chama "RIC" mas Desktop renderiza "RG" pelo off-by-one — confirmado em 08/05/2026 com matriz 10186/87/88)
- `tipoDeDeficiencia = 0` (Não Possui)
- `statusatestadoocupacional = 1` (Apto)
- `primeiroemprego = false` por padrão. Só virar `true` se o cliente informar **explicitamente** "primeiro emprego" no email/ficha. **NÃO inferir** pela ausência de PIS.
- `possuideficiencia = false`
- `requersegurodesemprego = false`

---

## 11. Template Completo De Referência

Use esse template e substitua os valores conforme dados do usuário:

```json
{
  "data": {
    "type": "candidatos",
    "attributes": {
      "nome": "NOME COMPLETO EM UPPERCASE",
      "cpf": 12345678901,
      "admissao": "2026-06-01",
      "nascimento": "1990-03-12",
      "nomedamae": "MAE EM UPPERCASE",
      "nomedopai": "PAI EM UPPERCASE",
      "municipionascimento": "GOIANIA",
      "nomecargo": "CARGO EM UPPERCASE",
      "salario": 1722.25,
      "primeiroemprego": false,
      "possuideficiencia": false,
      "requersegurodesemprego": false,
      "diascontratoexperiencia": 30,
      "dataterminocontrato": "2026-07-01",
      "dataatestadoocupacional": "2026-05-25",
      "usuariocriacao": "PIPELINE-V3",
      "observacao": "ADMISSAO VIA PIPELINE",
      "ctps": 1234567,
      "seriectps": "123",
      "datactps": "2014-05-20",
      "identidade": "5123456",
      "dataidentidade": "2008-04-15",
      "orgaoemissoridentidade": "SSP",
      "cep": "74000-000",
      "rua": "RUA EM UPPERCASE",
      "numero": 100,
      "complemento": "",
      "bairro": "BAIRRO EM UPPERCASE",
      "cidade": "GOIANIA",
      "email": "email.lowercase@dominio.com",
      "celular": "(62)999990000",
      "tituloeleitor": "012345678901",
      "zonatituloeleitor": "100",
      "secaotituloeleitor": "0050",
      "pis": "12345678901",
      "datapis": "2010-05-20",
      "banco": "001",
      "agencia": "12345",
      "conta": "98765-4"
    },
    "relationships": {
      "empresa":        { "data": { "type": "empresas",       "id": "1" } },
      "departamento":   { "data": { "type": "departamentos",  "id": "1" } },
      "funcao":         { "data": { "type": "funcoes",        "id": "1" } },
      "statusadmissao": { "data": { "type": "tipos-status-admissao", "id": "1" } },
      "sexo":           { "data": { "type": "tipos-sexo",            "id": "2" } },
      "estadocivil":    { "data": { "type": "tipos-estado-civil",    "id": "1" } },
      "raca":           { "data": { "type": "tipos-raca",            "id": "2" } },
      "escolaridade":   { "data": { "type": "tipos-escolaridade",    "id": "7" } },
      "estado":         { "data": { "type": "estados",               "id": "9" } },
      "naturalidade":   { "data": { "type": "estados",               "id": "9" } },
      "ufidentidade":   { "data": { "type": "estados",               "id": "9" } },
      "ufctps":         { "data": { "type": "estados",               "id": "9" } },
      "nacionalidade":  { "data": { "type": "paises",                "id": "105" } },
      "paisnascimento": { "data": { "type": "paises",                "id": "105" } },
      "pais":           { "data": { "type": "paises",                "id": "105" } },
      "tipoadmissao":           { "data": { "type": "tipos-admissao",            "id": "1" } },
      "tipovinculotrabalhista": { "data": { "type": "tipos-vinculos-trabalhista","id": "60" } },
      "categoriawdp":           { "data": { "type": "tipos-categoria",           "id": "1" } },
      "formapagamento":         { "data": { "type": "tipos-forma-de-pagamento",  "id": "4" } },
      "tipoidentidade":         { "data": { "type": "tipos-identidade",          "id": "1" } },
      "tipoconta":              { "data": { "type": "tipos-de-conta",            "id": "1" } },
      "tipoDeDeficiencia":      { "data": { "type": "tipos-deficiencia",         "id": "0" } },
      "statusatestadoocupacional": { "data": { "type": "tipos-status-atestado-ocupacional", "id": "1" } }
    }
  }
}
```

### Regras de variação a partir do template

- **Sem CNH:** omita `cnh`, `emissaocnh`, `validadecnh`, `primeiraemissaocnh`, `orgaoemissorcnh` e o relationship `categoriacnh` + `ufcnh`.
- **Sem dados bancários:** omita `banco`, `agencia`, `conta` e o relationship `tipoconta`.
- **Primeiro emprego (apenas quando cliente informar explicitamente):** `primeiroemprego: true`, omita `pis` e `datapis`.
- **Sem PIS mas não é primeiro emprego (default):** mantém `primeiroemprego: false` e omite `pis` + `datapis`.
- **Endereço sem número:** `numero: 0`, complemento opcional `"Q. X L. Y"`.
- **Sexo masculino:** `sexo` id=1.
- **Estado civil:** ajustar (1=Solteiro, 2=Casado, 3=Divorciado, etc.).
- **Cor não-default:** ler seção Bug 1 antes de mudar de id=4. Default seguro é id=4.
- **Trabalhador rural:** `tipovinculotrabalhista` id=25 (rural PF) ou id=70 (rural PJ).
- **Outras cidades/estados:** ajustar `municipionascimento`, `cidade`, e os 5 relationships de UF.

---

## 11.5 Lista De Campos Que DP Corrige Manualmente No Alterdata Desktop

Esses campos **não chegam corretamente** no Desktop por bugs de sync ou
limitação de produto E-plugin. DP precisa corrigir/preencher antes de gravar
em **toda admissão**:

### Bugs do sync (campo enviado mas não chega correto)
1. **Cor/Raça** — pipeline envia id=2 (Branca via off-by-one), mas pode chegar errado no Desktop por causa do Bug 1
2. **Tipo de Identidade** — pipeline envia id=1 (RG via off-by-one); Bug 3 ainda pode causar campo vazio
3. **Categoria CNH** — quando preenchida, vem deslocada (Bug 8 off-by-one)
4. **Dias para vencimento (experiência)** — sempre chega "2" no Desktop independente do valor enviado (Bug 4). DP corrige pra 30.
5. **Data término contrato** — Desktop recalcula a partir do Bug 4 (Bug 9). DP corrige pra admissao+90.
6. **Departamento** — às vezes vem com mapeamento local diferente, descrição confere
7. **Ocorrência** — campo enviado em `attributes.ocorrencia` não chega ao Desktop (Bug 6)
8. **Forma de pagamento (em Complemento da Folha)** — vai vazio (Bug 7); o "Tipo de processamento da folha" pega o valor
9. **Número do endereço** — pipeline envia 0 quando sem número (API exige Integer; "SN" string retorna HTTP 500). UI eContador Web interpreta 0 como "sem número" e marca checkbox automático, mas o Alterdata Desktop mostra "0" literal — DP marca checkbox "sem número" e/ou apaga o 0 manualmente.

### Limitações de produto (campos que NÃO existem no payload)
9. **Matrícula eSocial** — DP define número sequencial > 0
10. **Natureza da atividade** — "Trabalhador urbano" (default) ou "Trabalhador rural"
11. **Categoria eSocial (separada da GFIP)** — 101 (Empregado) padrão
12. **Tipo de jornada** — "Horário diário fixo e folga fixa (no domingo)" padrão
13. **Regime de Jornada** — "Horário de Trabalho" padrão
14. **Horas semanais** — 44 padrão
15. **FGTS completo** — Conta, Data de opção (= admissão), UF, Saldo. Todos os 4 campos. Testei 22 nomes de attribute, nenhum aceito.
16. **Dias para prorrogação (experiência)** — 60 (CLT 30+60)
17. **Tipo de salário contratual + data** — Mensal, data = admissão
18. **Adiantamento (checkbox)** — marcar
19. **Não atualiza salário (checkbox)** — marcar
20. **Horário (código)** — 1179 padrão MODELOFARMA
21. **Tipo (campo Desktop, irrelevante pro payload)** — 3

**Tempo estimado:** ~1.5 min/admissão pra fechar todos os 21 ajustes.

## 12. Checklist Antes De Entregar Payload

- [ ] Strings em UPPERCASE (exceto email)?
- [ ] CPF como integer (sem aspas)?
- [ ] Datas em ISO 8601?
- [ ] `statusadmissao = 1`?
- [ ] `diascontratoexperiencia = 90` e `dataterminocontrato = admissao + 90 dias`?
- [ ] `tipovinculotrabalhista = 60` (urbano determinado)?
- [ ] Numero=0 se sem número (não "SN")?
- [ ] Bancário tudo-ou-nada?
- [ ] PIS/datapis omitidos se primeiroemprego?
- [ ] Sem inventar attributes (Matrícula eSocial, Categoria eSocial, etc.)?
- [ ] JSON limpo, sem comentários, dentro de bloco ```json?

---

## Links Relacionados (Para Aprofundamento)
- [[01 - Visão Geral e Arquitetura]]
- [[02 - Decisões do DP]]
- [[08 - Referência Rápida (Lookups + Campos)]]
- [[10 - Pendências Para Suporte E-Plugin (v2 - LIMPO)]]
- [[11 - Bug Bilateral Cor Raça (UI eContador)]]
