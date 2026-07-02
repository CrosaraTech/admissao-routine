# AdmitER Web — Guia de Operação

Interface web do pipeline de admissão da Crosara Contabilidade. Roda no
PC do escritório e pode ser acessada de qualquer máquina da rede (LAN).

A web **coexiste** com a UI Tkinter (`interface.py`) — pode usar as duas
ao mesmo tempo, lendo os mesmos dados.

---

## Como iniciar

### Opção 1 — clique duplo (recomendado pro dia a dia)

Clique 2x em `iniciar-web.bat` (na pasta `local-pipeline`). Vai abrir uma
janela preta que mostra o IP da LAN e fica rodando até você fechar.

### Opção 2 — terminal

**CMD (Prompt de Comando):**
```cmd
cd C:\Users\Havai\Desktop\teste eContador\admissao-routine\local-pipeline
iniciar-web.bat
```

**PowerShell** (precisa do `.\` na frente — por segurança o PowerShell
não executa scripts do diretório atual sem o prefixo explícito):
```powershell
cd "C:\Users\Havai\Desktop\teste eContador\admissao-routine\local-pipeline"
.\iniciar-web.bat
```

Ou direto pelo Python:
```cmd
python webapp.py
```

A janela mostra:

```
 * Running on http://0.0.0.0:8080
 * Running on http://192.168.x.x:8080
```

---

## Como acessar

### No próprio PC

Abra o navegador (Chrome / Edge / Firefox) e vá em:

```
http://localhost:8080
```

### De outro PC da LAN

Precisa do IP do PC do escritório.

**Achando o IP:**

```cmd
ipconfig
```

Procure por `Endereço IPv4` na seção "Adaptador Ethernet" (cabo) ou
"Adaptador de Rede sem Fio Wi-Fi". Ex: `192.168.1.45`.

Depois, em qualquer PC da rede:

```
http://192.168.1.45:8080
```

---

## Liberar firewall (porta 8080)

Por padrão, o Windows bloqueia conexões de fora. Pra liberar, abra um
**Prompt de Comando como Administrador** e rode:

```cmd
netsh advfirewall firewall add rule name="AdmitER Web" dir=in action=allow protocol=TCP localport=8080
```

Pra remover depois (se quiser):

```cmd
netsh advfirewall firewall delete rule name="AdmitER Web"
```

---

## Rodar em background no boot do Windows

Pra a web subir automaticamente quando o PC liga, **sem janela preta
aparecendo**:

### Método 1 — Pasta Startup (mais simples)

1. Pressione `Win+R`, digite `shell:startup`, Enter
2. A pasta `Inicializar` abre
3. Clique direito → **Novo** → **Atalho**
4. Em "Local do item", aponte pro arquivo:
   ```
   C:\Users\Havai\Desktop\teste eContador\admissao-routine\local-pipeline\iniciar-web-background.vbs
   ```
5. Nome do atalho: `AdmitER Web`, **Concluir**

Pronto. Toda vez que o Windows iniciar com o seu usuário, a web sobe sozinha.

### Método 2 — Task Scheduler (mais robusto)

Útil se quiser:
- Rodar **antes** do login (sob conta SYSTEM)
- Reiniciar automaticamente se travar
- Logar saída em arquivo

1. Abra **Agendador de Tarefas** → **Criar Tarefa**
2. **Geral:**
   - Nome: `AdmitER Web`
   - Marque *Executar com privilégios mais altos*
   - Marque *Executar estando o usuário conectado ou não*
3. **Disparadores:** novo gatilho → *Ao iniciar* (ou *Ao fazer logon*)
4. **Ações:** novo → *Iniciar um programa*:
   - Programa: `wscript.exe`
   - Argumentos: `"C:\Users\Havai\Desktop\teste eContador\admissao-routine\local-pipeline\iniciar-web-background.vbs"`
5. **Condições:** desmarque *Iniciar somente se conectado à energia AC* se
   for notebook que pode ficar na bateria

---

## Como parar

### Se você iniciou via clique duplo no .bat

- Feche a janela preta (X no canto superior direito), OU
- Pressione `Ctrl+C` na janela e confirme

### Se está rodando em background (via .vbs)

1. Abra o **Gerenciador de Tarefas** (`Ctrl+Shift+Esc`)
2. Aba **Detalhes**
3. Procure por `python.exe` (ou `pythonw.exe`)
4. Confirme que é a web olhando a coluna *Linha de comando* — deve ter
   `webapp.py`
5. Clique direito → **Finalizar tarefa**

---

## Troubleshooting

### "Address already in use" / "Endereço já em uso" ao iniciar

A porta 8080 já está ocupada. Causas:

- A web já está rodando em outra janela ou em background — só usar a
  que já está aberta
- Outro programa pegou a porta 8080 (raro)

**Achando o que está usando a porta:**

```cmd
netstat -ano | findstr :8080
```

A última coluna é o PID. Aí no Gerenciador de Tarefas → aba *Detalhes* →
coluna PID, finalize o processo.

### "Página não carrega" / Flask travado

Algum loop infinito ou erro silencioso. Solução:

1. Feche a janela do .bat
2. Espere 5 segundos
3. Inicie de novo

Se persistir, rode `python webapp.py` direto no terminal pra ver o erro
completo.

### "Acesso negado" da LAN (funciona no PC mas não no celular/outro PC)

Firewall do Windows está bloqueando. Veja a seção **Liberar firewall**
acima.

Se mesmo com o firewall liberado não conectar, verifique:

- Os 2 PCs estão na mesma rede Wi-Fi / cabo?
- O roteador permite tráfego LAN entre dispositivos? (alguns roteadores
  têm "isolamento AP" ligado por padrão)

### "Webapp.py: command not found" / Python não encontrado

O `iniciar-web.bat` tenta `.venv\Scripts\python.exe` primeiro, depois
`python` do PATH. Se nenhum existir:

- Instale Python 3.11+ de python.org
- Marque "Add Python to PATH" no instalador
- Reinicie o terminal

### Tela em branco / 500 Internal Server Error

Olhar a janela do .bat — Flask printa o stack trace lá. Causas comuns:

- `dashboard_data.py` quebrou lendo NDJSON corrompido
- Permissão de leitura no `admissao_log.ndjson` (rodar como Admin?)

---

## Segurança

A web **não tem autenticação**. Qualquer pessoa na LAN consegue acessar.

Isso é OK enquanto a LAN é confiável (só máquinas do escritório). **Não
exponha a porta 8080 pra internet** sem antes adicionar autenticação.

### Próximos passos pra produção (futuro)

Quando for hora de endurecer:

1. **Substituir Flask dev server por Waitress** (production-grade):
   ```cmd
   pip install waitress
   waitress-serve --host=0.0.0.0 --port=8080 webapp:app
   ```

2. **Adicionar senha básica** (HTTP Basic Auth via decorator no Flask, ou
   reverse proxy com nginx/caddy)

3. **HTTPS com cert local** usando `mkcert`:
   ```cmd
   mkcert -install
   mkcert localhost 192.168.1.45
   ```
   Aí servir via Waitress + cert.

4. **Logs estruturados** (substituir prints por `logging` em arquivo
   rotativo)

Por enquanto, dev server + LAN fechada é suficiente pro uso interno.
