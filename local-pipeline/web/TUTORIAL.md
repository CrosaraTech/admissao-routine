# AdmitER — Guia de Uso

Pra quem vai trabalhar com as admissões pelo navegador.

---

## 1. Como abrir

No seu computador, abra o navegador (Chrome ou Edge) e vá em:

```
http://localhost:8080
```

Se você estiver em outro PC do escritório, peça o endereço de IP de quem
cuida do servidor — vai ser algo como `http://192.168.1.45:8080`.

Pode deixar a aba aberta o dia inteiro. Atualiza sozinha de tempos em tempos.

---

## 2. A tela

Do lado esquerdo tem o menu com as cinco áreas principais:

- **Dashboard** — visão geral do dia
- **Pendentes** — admissões esperando você resolver (o número entre parênteses é a fila aberta)
- **Processadas** — admissões já cadastradas com sucesso
- **Auditoria** — histórico técnico do que o sistema fez (raramente precisa)
- **Estatísticas** — custos do mês (Claude + DirectData)
- **Importar** — pra subir documentos que vieram fora do email

No canto superior direito tem o botão **Tema escuro** se você preferir fundo
preto (a preferência fica salva no seu navegador).

---

## 3. O que cada coisa faz no Dashboard

### Os 4 cartões do topo

| Cartão | Significado |
|---|---|
| Processadas | Quantas admissões já entraram no eContador este mês |
| Pendentes (cliente) | Esperando o cliente mandar alguma info (salário, RG, etc.) |
| Pendentes (internas) | Algo que a gente precisa resolver (CNPJ não cadastrado, cargo novo) |
| Falhas técnicas | Erros do sistema — alguém precisa investigar |

### Card "Controle"

- **Iniciar polling** — liga o "robô" que fica de olho no Gmail toda hora. Liga quando chegar de manhã, desliga quando sair.
- **Parar polling** — para o robô
- **Rodar 1 passada** — força o sistema a olhar o Gmail uma vez agora (quando você está esperando um email específico)
- **Importar arquivos** — leva você pra tela de subir PDFs/fotos manualmente
- **Backup agora** — faz uma cópia de segurança da planilha + arquivos (faça pelo menos uma vez por semana)
- **Atualizar tabelas** — recarrega a página (você também pode pressionar `R` no teclado)

### Card "Configurações"

São os ajustes do sistema. As mais importantes:

- **Intervalo de polling** — de quanto em quanto tempo o robô olha o Gmail. Padrão 300 segundos (5 min) está bom.
- **Enviar email de pendência automaticamente pro cliente** — se ligado, quando faltar info do cliente o sistema responde o email dele cobrando. **Não ligue sem alinhar com a gerência primeiro.**

As outras três caixinhas (SEMPRE mandar SEM data, SEM função, REPROCESSAR
emails com label pendente) são exceções. Deixe desmarcadas.

Quando mudar alguma coisa, clique em **Salvar configurações** no final.

### Card "Manutenção"

Use só quando precisar:

- **Recarregar planilha CBO** — depois que você adicionar um cargo novo na planilha de cargos
- **Atualizar cache de empresas** — quando o cadastro de uma empresa nova for feito direto no eContador
- **Limpar fingerprint** — força o sistema a reprocessar tudo de novo, mesmo sem mudança

---

## 4. O dia a dia — Resolvendo uma pendência

Essa é a tarefa mais comum. Aparece um email novo, o sistema processa, e
às vezes não consegue terminar sozinho.

### Passo a passo

1. **Clique em "Pendentes"** no menu esquerdo.
2. **Confira a lista** agrupada por dia.
3. **Clique no botão laranja "Resolver →"** da linha que você quer atender.
4. **Você vai pra tela de detalhes** — pode completar o que falta.

### O que cada coluna da lista significa

- **Nome** — funcionário
- **Empresa** — quem está contratando
- **CNPJ** — da empresa
- **Cargo (IA)** — o cargo que o sistema identificou ("—" se não conseguiu)
- **Procedência** — motivo da pendência (ex: "Salário não informado")

### As três categorias de pendência

- **cliente** — falta informação do cliente (ex: salário, RG)
- **interna** — problema do nosso lado (CNPJ novo, cargo sem cadastro)
- **falha** — erro técnico, peça pra alguém olhar

### Na tela de detalhes

Você vê três blocos:

- **Identificação** — quem é, qual empresa, quando chegou
- **Dados extraídos pelo Claude** — tudo que a IA conseguiu ler dos documentos
- **Aplicar correções e POSTar** — o formulário pra completar o que falta

No formulário, **só preencha o que está faltando** ou está errado. Deixe
em branco os campos que já vieram certinhos — eles serão mantidos como
estão.

Quando estiver pronto, clique em **Enviar para o eContador**. Vai pedir
confirmação (porque essa ação cria um candidato real e não dá pra desfazer).

### Se já cadastrou direto no eContador

Use o botão **Marcar como resolvido manualmente** no final da página.
Ele só registra na planilha que essa pendência foi resolvida, sem mexer
em nada no eContador. Pede confirmação antes.

---

## 5. Casos especiais

### O cliente leu o CNPJ errado nos documentos

Quando você sabe qual é o CNPJ correto, na lista de Pendentes (numa linha
de pendência **interna**), clique no botãozinho cinza **CNPJ** ao lado de
"Resolver". Vai abrir uma janelinha pedindo o CNPJ correto. Digite e
confirme.

Na próxima vez que o sistema processar esse email (você pode forçar
clicando em **Rodar 1 passada** no Dashboard), ele vai usar o CNPJ
correto.

### A pendência diz "Nenhum payload no disco"

Isso acontece com pendências antigas (geradas antes de junho/2026).
Clique no botão laranja **Reprocessar email** no aviso. Vai gastar uns
US$ 0,15 em Claude, mas depois aparecem os campos extraídos pra você
completar.

### Quer adicionar uma admissão sem email

Use o **Importar**:

1. Clique em **Importar** no menu esquerdo
2. Selecione os arquivos (PDF, fotos do RG, ASO, ficha, etc. — segure
   Ctrl pra escolher vários)
3. Se a info estiver faltando nos documentos, escreva no campo "Contexto
   adicional" (ex: "Admissão da Maria Silva, CNPJ 12.345.678/0001-90,
   salário 2000")
4. Clique em **Importar e processar**
5. Aguarde 1-2 minutos
6. Vai aparecer em Pendentes ou Processadas dependendo do resultado

---

## 6. Atalhos úteis

| Tecla | O que faz |
|---|---|
| `R` | Recarrega a página atual (atualiza a lista) |
| Duplo clique numa linha | (Tkinter) abre o detalhe; na web, use "Resolver →" |
| `Ctrl+F` (navegador) | Procura na página — útil pra achar um nome rápido |

---

## 7. Quando algo dá errado

**A página não carrega** → quem cuida do servidor precisa reiniciar o
`iniciar-web.bat`.

**Clicou em "Iniciar polling" mas não acontece nada** → veja o card
"Última passada" no Dashboard. Se mostrar "Parado" continuamente, pode
ser que o token do Gmail expirou. Avise quem cuida do sistema.

**Clicou em "Enviar pro eContador" e deu "Falha técnica HTTP 422"** →
algum campo está em formato estranho. Geralmente é o CEP ou o número do
endereço. Confira e tente de novo.

**Apareceu mensagem "Já estava cadastrado"** → tudo certo. O sistema
descobriu que essa pessoa já tinha sido cadastrada antes e não criou
duplicata. Pode seguir.

**Não tem certeza se enviou** → vai em "Processadas" no menu. Se está
lá, foi cadastrado. Se está em "Pendentes" ainda, não foi.

---

## 8. Rotina sugerida do dia

### De manhã

1. Abre o navegador e vai pra `http://localhost:8080`
2. Clica em **Iniciar polling** no Dashboard
3. Confere quantas pendências têm

### Durante o dia

- A cada 1-2h, dá uma olhada na aba **Pendentes**
- Vai resolvendo as que conseguir (clicar em "Resolver →")
- Pra as que dependem do cliente, deixa quieto — eles vão responder o
  email e o sistema reprocessa sozinho

### Antes de sair

1. Clica em **Backup agora** no Dashboard (sexta-feira pelo menos)
2. Clica em **Parar polling**
3. Pode fechar a aba

---

## 9. Dúvidas comuns

**Posso usar no celular?** Sim, a interface se adapta. Mas a tela é
pequena pra preencher formulários longos — pra resolver pendência use o
PC.

**Posso deixar várias abas abertas?** Pode. Cada aba pega o estado atual
do sistema. Atualizam sozinhas.

**E se duas pessoas mexerem ao mesmo tempo?** Funciona. O sistema tem
proteção contra criar candidato duplicado — se duas pessoas clicarem
"Enviar" pra mesma pendência, só uma chega ao eContador.

**O que muda do Tkinter?** A tela do desktop (Tkinter) continua
funcionando igual e tem as mesmas informações. A web é só uma forma de
acessar de outras máquinas da rede. Pode usar os dois ao mesmo tempo.

---

Qualquer dúvida que não está aqui, fala com João Marcos.
