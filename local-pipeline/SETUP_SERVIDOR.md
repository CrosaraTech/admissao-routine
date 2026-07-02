# Setup do AdmitER no Servidor

Passo-a-passo pra subir do zero numa máquina nova. Assume Windows + Python
3.11 (que é o que está no desktop). Pra Linux, basta trocar comandos
PowerShell por bash e `.venv\Scripts\activate` por `source .venv/bin/activate`.

---

## 1. Pré-requisitos

```powershell
# Python 3.11 instalado e no PATH
python --version   # esperado: 3.11.x

# Git (opcional, só se for clonar via git)
git --version
```

Se não tiver Python 3.11: https://www.python.org/downloads/release/python-3119/
Marque "Add to PATH" no instalador.

---

## 2. Extrair o zip

Extrai pra `C:\AdmitER\` (ou onde quiser — só evita Desktop/OneDrive
pra não sincronizar dados sensíveis).

```powershell
# Estrutura esperada após extrair:
# C:\AdmitER\
#   ├── main.py
#   ├── webapp.py
#   ├── interface.py
#   ├── requirements.txt
#   ├── .env.example      (NÃO tem .env — você cria)
#   ├── admissoes.xlsx
#   ├── perfis_remetente.json
#   ├── payloads\
#   ├── rascunhos\
#   ├── web\
#   └── ...
```

---

## 3. Criar .env (CRÍTICO)

```powershell
cd C:\AdmitER
copy .env.example .env
notepad .env
```

Preencha:

| Variável | Como obter |
|---|---|
| `ECONTADOR_TOKEN` | JWT do E-plugin Alterdata. Use o **mesmo** do desktop (não rotaciona, é a mesma org) |
| `ANTHROPIC_API_KEY` | https://console.anthropic.com — pode gerar uma key separada pra esse servidor |
| `GMAIL_TOKEN` | JSON inteiro do OAuth do Gmail. Cole as 1-2 linhas exatas do desktop |
| `DIRECTDATA_TOKEN` | Painel DirectData. Opcional — sem ela, lookup CPF não roda mas resto funciona |

**Nunca commitar o `.env`.** O `.gitignore` já bloqueia.

---

## 4. Criar venv + instalar deps

```powershell
cd C:\AdmitER
python -m venv .venv
.\.venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
```

Espera ~2 minutos. Instala Flask, httpx, anthropic, google-api-python-client,
openpyxl, waitress, etc.

---

## 5. Validar setup

```powershell
# Smoke test — confere imports e tokens
python verificar-setup.py
```

Deve mostrar OK pra todos os tokens. Se reclamar de Gmail, é porque o OAuth
token expirou — abra `gmail_client.py` e siga o fluxo de re-autenticação
(geralmente abre browser e pede pra logar como `informatica@contabilidadehavai.com.br`).

---

## 6. Subir a UI web

**Manual (dev):**
```powershell
.\iniciar-web.bat
# UI em http://localhost:8080
```

**Background (esconde janela):**
```powershell
wscript iniciar-web-background.vbs
```

**Como serviço Windows (24/7):**
1. Baixar NSSM https://nssm.cc/download
2. `nssm install AdmitER`
   - Path: `C:\AdmitER\.venv\Scripts\python.exe`
   - Arguments: `webapp.py`
   - Startup directory: `C:\AdmitER`
3. `nssm start AdmitER`

---

## 7. Polling do Gmail

Se essa máquina vai **fazer o polling** (buscar emails novos):

- **Tarefa agendada**: abre Agendador do Windows → Importar tarefa → use `iniciar-web.bat` como referência ou crie task que rode `python main.py loop` a cada 5 min.
- **Polling integrado**: a partir da v2.16.x o `webapp.py` tem POLLING worker integrado (verifique se está ativo nas Configurações em http://localhost:8080/configuracoes).

Se essa máquina vai **só servir UI** (sem polling), pula esse passo.

---

## 8. Pós-instalação — primeiras coisas a verificar

| Tela | O que conferir |
|---|---|
| `/configuracoes` | Versão = **2.16.42**, tokens OK, polling ON/OFF conforme planejado |
| `/empresas` | Botão "Recarregar cache" — rode 1x (~10s, busca todas empresas do eContador) |
| `/pendentes` | Pendência da **WILDA ROSA** está lá? Clica "🔄 Reprocessar" — agora o fix da 2.16.42 deve passar |
| `/perfis/` | Procure o perfil de Mercafrutas (`contabilidade@mercafrutas...`) e na linha **MOTORISTA/ENTREGADOR** cadastre o salário fixo |

---

## 9. Continuar trabalhando com o Claude Code

1. Instale Claude Code no servidor (https://claude.com/claude-code)
2. Abra o terminal em `C:\AdmitER`
3. Rode `claude`
4. **No primeiro turno**, cole o conteúdo de `RESUMO_MIGRACAO.md`. O Claude vai ler e ter contexto da sessão anterior sem você precisar reexplicar nada.
5. O `CLAUDE.md` do projeto + a auto-memory já estão configurados — Claude vai puxar regras do escritório automaticamente.

---

## 10. Rotação de tokens (eventualmente)

- `ECONTADOR_TOKEN`: JWT, geralmente vale ~30 dias. Renove no portal Alterdata quando expirar.
- `GMAIL_TOKEN`: refresh token, dura indefinido se app não revogar. Re-autentique se quebrar.
- `ANTHROPIC_API_KEY`: troque se vazar ou se quiser limitar custo de outro server.
