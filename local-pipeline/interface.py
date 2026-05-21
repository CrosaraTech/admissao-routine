"""interface.py — GUI desktop pro pipeline de admissão (Tkinter).

3 abas:
  • Principal: status, controle (start/stop), toggle auto-email, log ao vivo
  • Processadas: tabela de admissões já cadastradas
  • Pendentes: tabela de admissões com pendência + botão "Resolver pendência"

Polling roda em thread separada; UI atualiza via fila thread-safe.
Reaproveita toda a lógica do main.py (carregar_config, rodar_uma_passada, etc.).

Uso:
    python interface.py
"""

from __future__ import annotations

import json
import queue
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

from dotenv import load_dotenv
load_dotenv()

from claude_client import ClaudeClient
from ecotador_client import EContadorAPI
from gmail_client import GmailClient
from main import (
    PAYLOADS_DIR, PLANILHA_ADMISSOES, PLANILHA_CBO, REGRAS_FILE,
    bootstrap_arquivos_locais, carregar_config, carregar_planilha,
    carregar_regras, fazer_backup_planilha_e_payloads,
    registrar_admissao_planilha, rodar_uma_passada,
    sum_billing_mes_atual,
)
from payload_builder import LABELS_AMIGAVEIS

# Toast Windows (opcional — fallback pra popup Tk se plyer não instalado)
try:
    from plyer import notification as _plyer_notif  # noqa
    _HAS_PLYER = True
except ImportError:
    _HAS_PLYER = False


# Dias após os quais uma pendência é considerada "velha" → highlight visual
DIAS_PENDENCIA_VELHA = 3

# Map reverso: label amigável → chave do attribute no payload
# (usado pelo form estruturado de resolver pendência)
LABEL_PRA_ATTR = {v: k for k, v in LABELS_AMIGAVEIS.items()}

# Hints de preenchimento por tipo de campo
HINTS_FORM = {
    "admissao": "YYYY-MM-DD (ex: 2026-06-15)",
    "nascimento": "YYYY-MM-DD (ex: 1990-03-12)",
    "dataatestadoocupacional": "YYYY-MM-DD (ex: 2026-06-10)",
    "salario": "Número decimal (ex: 1722.25)",
    "cpf": "Apenas dígitos (ex: 12345678901)",
    "diascontratoexperiencia": "Inteiro (default 30)",
    "primeiroemprego": "true ou false",
}


# ============================================================
# Janela principal
# ============================================================

class PipelineGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Crosara — Pipeline de Admissão")
        self.geometry("1300x800")
        self.minsize(1000, 600)

        self._init_state()
        self._build_ui()
        self._consumir_fila()  # loop periódico thread→UI
        self._refresh_tabelas()

        # Fechar limpo
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- Estado / inicialização --------------------------------

    def _init_state(self):
        bootstrap_arquivos_locais()
        try:
            self.config = carregar_config()
            # GUI controla via toggle, não via prompt interativo
            self.config.confirmar_replies = False
        except Exception as e:
            messagebox.showerror("Erro carregando config", str(e))
            raise

        try:
            self.planilha = carregar_planilha(PLANILHA_CBO)
        except Exception as e:
            messagebox.showerror("Erro carregando planilha CBO", str(e))
            raise

        carregar_regras()  # log info; wiring vem em commits futuros

        self.claude = ClaudeClient(
            model=self.config.claude_model,
            max_tokens=self.config.claude_max_tokens,
            chamadas_verificacao=self.config.claude_chamadas_verificacao,
        )

        self.polling = False
        self.polling_thread: threading.Thread | None = None
        self.gui_q: queue.Queue = queue.Queue()

        # Tk vars
        self.status_var = tk.StringVar(value="⏸ Parado")
        self.ultima_var = tk.StringVar(value="—")
        self.proxima_var = tk.StringVar(value="—")
        self.auto_email_var = tk.BooleanVar(value=self.config.auto_email_pendencias)
        self.intervalo_var = tk.IntVar(value=self.config.intervalo)
        self.contador_proc_var = tk.StringVar(value="0")
        self.contador_pend_var = tk.StringVar(value="0")
        self.contador_velha_var = tk.StringVar(value="0")
        self.billing_var = tk.StringVar(value="US$ 0.0000 / mês")
        # Limite mensal de billing pra alerta visual (default 50 USD)
        self.billing_limite_usd = 50.0
        # Filtros de busca por tab
        self.filtro_proc_var = tk.StringVar()
        self.filtro_pend_var = tk.StringVar()
        self.filtro_audit_var = tk.StringVar()
        self.filtro_proc_var.trace_add("write", lambda *a: self._refresh_tabelas())
        self.filtro_pend_var.trace_add("write", lambda *a: self._refresh_tabelas())
        self.filtro_audit_var.trace_add("write", lambda *a: self._refresh_auditoria())

        # Tracking pra disparar toast quando chega pendência nova
        self._msg_ids_pendentes_conhecidos: set[str] = set()
        self._primeira_carga = True

        # Dark mode (afeta log + treeviews + algumas frames)
        self.dark_mode_var = tk.BooleanVar(value=False)

    # ---- Construção das abas -----------------------------------

    def _build_ui(self):
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=10)

        self.tab_main = ttk.Frame(self.notebook)
        self.tab_proc = ttk.Frame(self.notebook)
        self.tab_pend = ttk.Frame(self.notebook)
        self.tab_audit = ttk.Frame(self.notebook)
        self.tab_stats = ttk.Frame(self.notebook)
        self.tab_regras = ttk.Frame(self.notebook)

        self.notebook.add(self.tab_main, text="🏠  Principal")
        self.notebook.add(self.tab_proc, text="✅  Processadas")
        self.notebook.add(self.tab_pend, text="⚠  Pendentes")
        self.notebook.add(self.tab_audit, text="📜  Auditoria")
        self.notebook.add(self.tab_stats, text="📈  Estatísticas")
        self.notebook.add(self.tab_regras, text="⚙  Regras")

        self._build_main()
        self._build_processadas()
        self._build_pendentes()
        self._build_auditoria()
        self._build_estatisticas()
        self._build_regras()

    def _build_main(self):
        # Linha 1 — Status (grande)
        f_status = ttk.LabelFrame(self.tab_main, text="Status do Pipeline", padding=12)
        f_status.pack(fill="x", padx=15, pady=(15, 10))

        grid = ttk.Frame(f_status)
        grid.pack(fill="x")
        ttk.Label(grid, text="Estado:", font=("Segoe UI", 10)).grid(row=0, column=0, sticky="w", padx=(0, 10))
        ttk.Label(grid, textvariable=self.status_var, font=("Segoe UI", 14, "bold"),
                  foreground="#0066cc").grid(row=0, column=1, sticky="w", columnspan=3)
        ttk.Label(grid, text="Última passada:").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=(10, 0))
        ttk.Label(grid, textvariable=self.ultima_var).grid(row=1, column=1, sticky="w", pady=(10, 0))
        ttk.Label(grid, text="Próxima passada:").grid(row=1, column=2, sticky="w", padx=(30, 10), pady=(10, 0))
        ttk.Label(grid, textvariable=self.proxima_var).grid(row=1, column=3, sticky="w", pady=(10, 0))

        # Linha 2 — Contadores rápidos
        f_cnt = ttk.Frame(self.tab_main)
        f_cnt.pack(fill="x", padx=15, pady=5)
        ttk.Label(f_cnt, text="✅ Processadas: ").pack(side="left")
        ttk.Label(f_cnt, textvariable=self.contador_proc_var, font=("Segoe UI", 11, "bold"),
                  foreground="#2e7d32").pack(side="left", padx=(0, 25))
        ttk.Label(f_cnt, text="⚠ Pendentes: ").pack(side="left")
        ttk.Label(f_cnt, textvariable=self.contador_pend_var, font=("Segoe UI", 11, "bold"),
                  foreground="#c62828").pack(side="left", padx=(0, 25))
        ttk.Label(f_cnt, text=f"🕒 Pendência >{DIAS_PENDENCIA_VELHA}d: ").pack(side="left")
        ttk.Label(f_cnt, textvariable=self.contador_velha_var, font=("Segoe UI", 11, "bold"),
                  foreground="#e65100").pack(side="left", padx=(0, 25))
        ttk.Label(f_cnt, text="💰 Custo Claude: ").pack(side="left")
        self.billing_label = ttk.Label(
            f_cnt, textvariable=self.billing_var, font=("Segoe UI", 11, "bold"),
            foreground="#1565c0",
        )
        self.billing_label.pack(side="left")

        # Linha 3 — Controles
        f_ctrl = ttk.LabelFrame(self.tab_main, text="Controle", padding=12)
        f_ctrl.pack(fill="x", padx=15, pady=10)

        self.btn_iniciar = ttk.Button(f_ctrl, text="▶  Iniciar polling", command=self._iniciar_polling)
        self.btn_iniciar.pack(side="left", padx=5)
        self.btn_parar = ttk.Button(f_ctrl, text="⏸  Parar", command=self._parar_polling, state="disabled")
        self.btn_parar.pack(side="left", padx=5)
        ttk.Button(f_ctrl, text="🔄  Rodar 1 passada agora", command=self._rodar_unica).pack(side="left", padx=5)
        ttk.Button(f_ctrl, text="📊  Atualizar tabelas", command=self._refresh_tabelas).pack(side="left", padx=5)
        ttk.Button(f_ctrl, text="💾  Backup agora", command=self._backup_agora).pack(side="left", padx=5)

        # Linha 4 — Configurações
        f_set = ttk.LabelFrame(self.tab_main, text="Configurações", padding=12)
        f_set.pack(fill="x", padx=15, pady=10)

        ttk.Checkbutton(
            f_set,
            text="Enviar email de pendência automaticamente pro cliente "
                 "(APENAS pra campos externos como salário, ASO, CPF — "
                 "pendências internas SEMPRE são resolvidas manualmente)",
            variable=self.auto_email_var,
            command=self._on_auto_email_change,
        ).pack(anchor="w", pady=3)

        intv = ttk.Frame(f_set)
        intv.pack(anchor="w", pady=(8, 0))
        ttk.Label(intv, text="Intervalo de polling (segundos):").pack(side="left")
        ttk.Spinbox(intv, from_=60, to=3600, increment=30,
                    textvariable=self.intervalo_var, width=8).pack(side="left", padx=8)
        ttk.Label(intv, text="(default 300 = 5 minutos)").pack(side="left")

        # Dark mode toggle
        ttk.Checkbutton(
            f_set, text="🌙  Modo escuro",
            variable=self.dark_mode_var,
            command=self._toggle_dark_mode,
        ).pack(anchor="w", pady=(8, 0))

        # Linha 5 — Log
        f_log = ttk.LabelFrame(self.tab_main, text="Atividade recente", padding=8)
        f_log.pack(fill="both", expand=True, padx=15, pady=(10, 15))
        self.log_text = scrolledtext.ScrolledText(
            f_log, height=14, font=("Consolas", 9), state="disabled",
            background="#1e1e1e", foreground="#d4d4d4",
        )
        self.log_text.pack(fill="both", expand=True)

    def _build_processadas(self):
        f = ttk.Frame(self.tab_proc)
        f.pack(fill="both", expand=True, padx=15, pady=15)

        # Toolbar
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Button(bar, text="🔄  Atualizar", command=self._refresh_tabelas).pack(side="left", padx=5)
        ttk.Button(bar, text="📂  Abrir payloads/", command=self._abrir_pasta_payloads).pack(side="left", padx=5)
        ttk.Button(bar, text="📧  Abrir email no Gmail",
                   command=lambda: self._abrir_email_gmail(self.tree_proc)).pack(side="left", padx=5)

        # Filtro de busca
        ttk.Label(bar, text="  Buscar:").pack(side="left", padx=(20, 4))
        ttk.Entry(bar, textvariable=self.filtro_proc_var, width=30).pack(side="left")

        # Tabela
        cols = ("ts", "nome", "empresa", "cnpj", "procedencia")
        self.tree_proc = ttk.Treeview(f, columns=cols, show="headings", height=22)
        headers = {
            "ts": ("Data/Hora", 140),
            "nome": ("Nome do colaborador", 240),
            "empresa": ("Empresa", 220),
            "cnpj": ("CNPJ", 140),
            "procedencia": ("Procedência", 460),
        }
        for c, (txt, w) in headers.items():
            self.tree_proc.heading(c, text=txt)
            self.tree_proc.column(c, width=w, anchor="w")

        sb = ttk.Scrollbar(f, orient="vertical", command=self.tree_proc.yview)
        self.tree_proc.configure(yscrollcommand=sb.set)
        self.tree_proc.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    def _build_pendentes(self):
        f = ttk.Frame(self.tab_pend)
        f.pack(fill="both", expand=True, padx=15, pady=15)

        # Toolbar
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Button(bar, text="🔧  Resolver pendência selecionada",
                   command=self._resolver_selecionada).pack(side="left", padx=5)
        ttk.Button(bar, text="🔁  Reprocessar (remove label e re-roda)",
                   command=self._reprocessar_selecionada).pack(side="left", padx=5)
        ttk.Button(bar, text="📧  Abrir email no Gmail",
                   command=lambda: self._abrir_email_gmail(self.tree_pend)).pack(side="left", padx=5)
        ttk.Button(bar, text="🔄  Atualizar", command=self._refresh_tabelas).pack(side="left", padx=5)

        # Filtro de busca
        ttk.Label(bar, text="  Buscar:").pack(side="left", padx=(20, 4))
        ttk.Entry(bar, textvariable=self.filtro_pend_var, width=30).pack(side="left")

        ttk.Label(f, text="(clique 2× na linha pra resolver — linhas amarelas/laranjas = pendência >3 dias)",
                  foreground="#666").pack(anchor="w", pady=(0, 5))

        # Tabela
        cols = ("ts", "nome", "empresa", "cnpj", "tipo", "procedencia")
        self.tree_pend = ttk.Treeview(f, columns=cols, show="headings", height=22)
        headers = {
            "ts": ("Data/Hora", 130),
            "nome": ("Nome do colaborador", 200),
            "empresa": ("Empresa", 200),
            "cnpj": ("CNPJ", 130),
            "tipo": ("Tipo", 80),
            "procedencia": ("Procedência / motivo", 440),
        }
        for c, (txt, w) in headers.items():
            self.tree_pend.heading(c, text=txt)
            self.tree_pend.column(c, width=w, anchor="w")

        sb = ttk.Scrollbar(f, orient="vertical", command=self.tree_pend.yview)
        self.tree_pend.configure(yscrollcommand=sb.set)
        self.tree_pend.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.tree_pend.bind("<Double-1>", lambda e: self._resolver_selecionada())

        # Cores por tipo + idade
        self.tree_pend.tag_configure("interno", background="#fff3e0")
        self.tree_pend.tag_configure("cliente", background="#e3f2fd")
        self.tree_pend.tag_configure("velha_interno", background="#ffab40", foreground="#000")
        self.tree_pend.tag_configure("velha_cliente", background="#ffd180", foreground="#000")

    # ---- Consumidor da fila thread→UI --------------------------

    def _consumir_fila(self):
        """Chamado a cada 200ms pelo Tkinter main loop. Lê eventos
        postados pela thread de polling e atualiza UI."""
        try:
            while True:
                kind, data = self.gui_q.get_nowait()
                if kind == "status":
                    self.status_var.set(data)
                elif kind == "ultima":
                    self.ultima_var.set(data)
                elif kind == "proxima":
                    self.proxima_var.set(data)
                elif kind == "log":
                    self._append_log(data)
                elif kind == "refresh":
                    self._refresh_tabelas()
        except queue.Empty:
            pass
        self.after(200, self._consumir_fila)

    def _append_log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line)
        self.log_text.see("end")
        # Limita a ~1000 linhas pra não inchar
        n_lines = int(self.log_text.index("end-1c").split(".")[0])
        if n_lines > 1000:
            self.log_text.delete("1.0", "500.0")
        self.log_text.configure(state="disabled")

    # ---- Ações de controle -------------------------------------

    def _iniciar_polling(self):
        if self.polling:
            return
        self.config.intervalo = max(60, int(self.intervalo_var.get()))
        self.polling = True
        self.polling_thread = threading.Thread(target=self._loop_polling, daemon=True)
        self.polling_thread.start()
        self.btn_iniciar.configure(state="disabled")
        self.btn_parar.configure(state="normal")
        self.gui_q.put(("log", f"▶ Polling iniciado (intervalo {self.config.intervalo}s)"))

    def _parar_polling(self):
        if not self.polling:
            return
        self.polling = False
        self.btn_parar.configure(state="disabled")
        self.btn_iniciar.configure(state="normal")
        self.gui_q.put(("status", "⏸ Parando..."))
        self.gui_q.put(("log", "⏸ Pedido de parada — vai parar após a passada atual"))

    def _rodar_unica(self):
        if self.polling:
            messagebox.showinfo("Polling ativo",
                                "O polling já está rodando passadas automaticamente.\nPara rodar uma extra, primeiro pare o polling.")
            return
        threading.Thread(target=self._executar_passada, daemon=True).start()

    def _executar_passada(self):
        self.gui_q.put(("status", "⚙ Executando..."))
        self.gui_q.put(("log", "▶ Iniciando passada única..."))
        try:
            rodar_uma_passada(self.config, self.claude, self.planilha)
            self.gui_q.put(("log", "✓ Passada única concluída"))
        except Exception as e:
            self.gui_q.put(("log", f"❌ Erro na passada: {e}"))
        self.gui_q.put(("status", "⏸ Parado"))
        self.gui_q.put(("ultima", datetime.now().strftime("%d/%m/%Y %H:%M:%S")))
        self.gui_q.put(("refresh", None))

    def _loop_polling(self):
        while self.polling:
            self.gui_q.put(("status", "⚙ Executando passada..."))
            self.gui_q.put(("log", "▶ Iniciando passada..."))
            try:
                rodar_uma_passada(self.config, self.claude, self.planilha)
                self.gui_q.put(("log", "✓ Passada concluída"))
            except Exception as e:
                self.gui_q.put(("log", f"❌ Erro na passada: {e}"))

            self.gui_q.put(("ultima", datetime.now().strftime("%d/%m/%Y %H:%M:%S")))
            self.gui_q.put(("refresh", None))

            # Sleep com checks periódicos pra status + parada
            t_end = time.time() + self.config.intervalo
            while time.time() < t_end and self.polling:
                t_left = int(t_end - time.time())
                self.gui_q.put(("status", f"⏱ Aguardando próxima ({t_left}s)"))
                self.gui_q.put(("proxima", datetime.fromtimestamp(t_end).strftime("%H:%M:%S")))
                time.sleep(1)

        self.gui_q.put(("status", "⏸ Parado"))
        self.gui_q.put(("proxima", "—"))
        self.gui_q.put(("log", "⏹ Polling parado"))

    def _on_auto_email_change(self):
        self.config.auto_email_pendencias = bool(self.auto_email_var.get())
        estado = "LIGADO ✉" if self.config.auto_email_pendencias else "DESLIGADO 🔇"
        self.gui_q.put(("log", f"⚙ Auto-email de pendências (apenas externas): {estado}"))

    def _abrir_pasta_payloads(self):
        if PAYLOADS_DIR.exists():
            import os
            os.startfile(PAYLOADS_DIR)  # Windows
        else:
            messagebox.showinfo("Sem payloads", "Pasta payloads/ ainda não existe.")

    # ---- Atualização das tabelas -------------------------------

    def _refresh_tabelas(self):
        """Lê admissoes.xlsx, popula as 2 tabelas, aplica filtros,
        destaca pendências velhas, atualiza contadores e billing."""
        for tv in (self.tree_proc, self.tree_pend):
            for item in tv.get_children():
                tv.delete(item)

        # Atualiza billing/limite
        self._atualizar_billing()

        if not PLANILHA_ADMISSOES.exists():
            self.contador_proc_var.set("0")
            self.contador_pend_var.set("0")
            self.contador_velha_var.set("0")
            return

        try:
            from openpyxl import load_workbook
            wb = load_workbook(PLANILHA_ADMISSOES, read_only=True)
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))
            if len(rows) <= 1:
                self.contador_proc_var.set("0")
                self.contador_pend_var.set("0")
                self.contador_velha_var.set("0")
                return

            filtro_p = (self.filtro_proc_var.get() or "").strip().lower()
            filtro_e = (self.filtro_pend_var.get() or "").strip().lower()
            hoje = datetime.now()

            n_proc = 0
            n_pend = 0
            n_velha = 0
            for row in reversed(rows[1:]):
                if not row or not any(row):
                    continue
                # 6 colunas (novas) ou 4 (legacy): detecta
                cols = list(row) + [""] * (6 - len(row))
                # Heurística: se primeira coluna parece data (tem '-' ou ':'),
                # é planilha nova. Senão é legacy (4 cols: nome,empresa,cnpj,proc).
                primeira = str(cols[0] or "")
                if ("-" in primeira and ":" in primeira) or primeira == "":
                    ts, nome, empresa, cnpj, procedencia, msg_id = cols[:6]
                else:
                    nome, empresa, cnpj, procedencia = cols[:4]
                    ts = ""
                    msg_id = ""

                ts = str(ts or "")
                nome = str(nome or "")
                empresa = str(empresa or "")
                cnpj = str(cnpj or "")
                procedencia = str(procedencia or "")
                msg_id = str(msg_id or "")
                proc_low = procedencia.lower()

                # Concatena tudo pra match de filtro
                hay_texto = " ".join((ts, nome, empresa, cnpj, procedencia)).lower()

                if proc_low.startswith("cadastrado") or proc_low.startswith("dry-run"):
                    if filtro_p and filtro_p not in hay_texto:
                        continue
                    self.tree_proc.insert(
                        "", "end",
                        values=(ts, nome, empresa, cnpj, procedencia),
                        tags=(msg_id,),  # tag carrega msg_id pra "Abrir email"
                    )
                    n_proc += 1
                elif proc_low.startswith("pendente") or proc_low.startswith("falha"):
                    if filtro_e and filtro_e not in hay_texto:
                        continue
                    tipo = "interno" if "interno" in proc_low else "cliente"
                    # Detecta pendência velha
                    velha = False
                    try:
                        dt_row = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                        if (hoje - dt_row).days >= DIAS_PENDENCIA_VELHA:
                            velha = True
                            n_velha += 1
                    except (ValueError, TypeError):
                        pass
                    tags = [tipo, msg_id]
                    if velha:
                        tags.append(f"velha_{tipo}")
                    self.tree_pend.insert(
                        "", "end",
                        values=(ts, nome, empresa, cnpj, tipo, procedencia),
                        tags=tags,
                    )
                    n_pend += 1

            self.contador_proc_var.set(str(n_proc))
            self.contador_pend_var.set(str(n_pend))
            self.contador_velha_var.set(str(n_velha))

            # Detecta pendências NOVAS (não vistas antes nessa sessão) e dispara toast
            try:
                ids_atuais = self._coletar_msg_ids_pendentes()
                if self._primeira_carga:
                    self._msg_ids_pendentes_conhecidos = ids_atuais
                    self._primeira_carga = False
                else:
                    novas = ids_atuais - self._msg_ids_pendentes_conhecidos
                    if novas:
                        self._notify_toast(
                            "⚠ Nova pendência",
                            f"{len(novas)} nova(s) admissão(ões) pendente(s) na fila.\n"
                            f"Veja na aba Pendentes.",
                        )
                    self._msg_ids_pendentes_conhecidos = ids_atuais
            except Exception:
                pass

            # Alerta de billing — toast quando passar do limite
            if hasattr(self, "_billing_alertou_dessa_sessao"):
                pass
            else:
                self._billing_alertou_dessa_sessao = False
            b = sum_billing_mes_atual()
            if (
                b["custo_usd"] >= self.billing_limite_usd
                and not self._billing_alertou_dessa_sessao
            ):
                self._notify_toast(
                    "💰 Limite de billing atingido",
                    f"O custo da API Claude no mês corrente passou de US$ "
                    f"{self.billing_limite_usd:.2f} (atual: US$ {b['custo_usd']:.4f}).",
                )
                self._billing_alertou_dessa_sessao = True

            # Atualiza outras tabs também
            if hasattr(self, "tree_audit"):
                self._refresh_auditoria()
            if hasattr(self, "stats_text"):
                self._refresh_estatisticas()
        except Exception as e:
            self.gui_q.put(("log", f"⚠ Erro lendo planilha: {e}"))

    def _coletar_msg_ids_pendentes(self) -> set[str]:
        """Conjunto de msg_ids que estão como pendência na planilha agora."""
        ids = set()
        if not PLANILHA_ADMISSOES.exists():
            return ids
        try:
            from openpyxl import load_workbook
            wb = load_workbook(PLANILHA_ADMISSOES, read_only=True)
            ws = wb.active
            for row in ws.iter_rows(min_row=2, values_only=True):
                if not row:
                    continue
                cols = list(row) + [""] * (6 - len(row))
                primeira = str(cols[0] or "")
                if ("-" in primeira and ":" in primeira) or primeira == "":
                    _ts, _nome, _emp, _cnpj, proc, mid = cols[:6]
                else:
                    _nome, _emp, _cnpj, proc = cols[:4]
                    mid = ""
                pl = str(proc or "").lower()
                if (pl.startswith("pendente") or pl.startswith("falha")) and mid:
                    ids.add(str(mid))
        except Exception:
            pass
        return ids

    def _atualizar_billing(self):
        """Lê billing.ndjson e atualiza display + cor (alerta se >= limite)."""
        try:
            resumo = sum_billing_mes_atual()
            custo = resumo["custo_usd"]
            self.billing_var.set(
                f"US$ {custo:.4f} / mês "
                f"({resumo['n_calls']} chamadas, {resumo['n_passadas']} passadas)"
            )
            # Cor: verde < 50% limite, laranja 50-100%, vermelho > 100%
            if custo >= self.billing_limite_usd:
                self.billing_label.configure(foreground="#c62828")
            elif custo >= self.billing_limite_usd * 0.5:
                self.billing_label.configure(foreground="#e65100")
            else:
                self.billing_label.configure(foreground="#1565c0")
        except Exception:
            pass

    def _msg_id_from_selection(self, tree: ttk.Treeview) -> str | None:
        sel = tree.selection()
        if not sel:
            return None
        tags = tree.item(sel[0])["tags"] or []
        # tags pode ter "interno"/"cliente"/"velha_*" + msg_id
        for t in tags:
            t = str(t)
            if t and t not in ("interno", "cliente", "velha_interno", "velha_cliente"):
                return t
        return None

    def _abrir_email_gmail(self, tree: ttk.Treeview):
        msg_id = self._msg_id_from_selection(tree)
        if not msg_id:
            messagebox.showinfo("Selecione",
                                "Selecione uma linha primeiro.\n\n"
                                "(Linhas antigas — antes do timestamp/msg_id — não conseguem abrir.)")
            return
        import webbrowser
        url = f"https://mail.google.com/mail/u/0/#all/{msg_id}"
        webbrowser.open(url)

    def _reprocessar_selecionada(self):
        msg_id = self._msg_id_from_selection(self.tree_pend)
        if not msg_id:
            messagebox.showinfo("Selecione",
                                "Selecione uma pendência com msg_id (planilha nova).\n\n"
                                "Linhas legacy precisam ser resolvidas com os outros botões.")
            return
        if not messagebox.askyesno(
            "Confirmar reprocessar",
            "Vou:\n"
            "  1. Remover labels processado/pendente da mensagem no Gmail\n"
            "  2. Rodar uma passada agora pra que o pipeline pegue de novo\n\n"
            "Confirma?"
        ):
            return
        threading.Thread(
            target=self._reprocessar_worker, args=(msg_id,), daemon=True
        ).start()

    def _reprocessar_worker(self, msg_id: str):
        self.gui_q.put(("log", f"🔁 Reprocessando msg {msg_id[:16]}..."))
        try:
            gmail = GmailClient()
            for lbl in (self.config.label_processado, self.config.label_pendente):
                try:
                    gmail.remover_label(msg_id, lbl)
                except Exception:
                    pass
            self.gui_q.put(("log", "   ✓ Labels removidas — rodando passada..."))
            rodar_uma_passada(self.config, self.claude, self.planilha)
            self.gui_q.put(("log", "✓ Reprocessamento concluído"))
        except Exception as e:
            self.gui_q.put(("log", f"❌ Erro reprocessando: {e}"))
        self.gui_q.put(("refresh", None))

    def _backup_agora(self):
        threading.Thread(target=self._backup_worker, daemon=True).start()

    def _backup_worker(self):
        self.gui_q.put(("log", "💾 Criando backup..."))
        try:
            dest = fazer_backup_planilha_e_payloads()
            if dest:
                self.gui_q.put(("log", f"✓ Backup criado em {dest}"))
            else:
                self.gui_q.put(("log", "❌ Falha criando backup"))
        except Exception as e:
            self.gui_q.put(("log", f"❌ Erro no backup: {e}"))

    # ---- Aba Auditoria (todas as admissões, todas as tentativas) ---

    def _build_auditoria(self):
        f = ttk.Frame(self.tab_audit)
        f.pack(fill="both", expand=True, padx=15, pady=15)

        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Label(bar, text="Histórico completo (todas as tentativas, ordem cronológica reversa):").pack(side="left")
        ttk.Label(bar, text="  Buscar:").pack(side="left", padx=(20, 4))
        ttk.Entry(bar, textvariable=self.filtro_audit_var, width=30).pack(side="left")
        ttk.Button(bar, text="🔄 Atualizar", command=self._refresh_auditoria).pack(side="left", padx=15)

        cols = ("ts", "nome", "empresa", "cnpj", "procedencia", "msg_id")
        self.tree_audit = ttk.Treeview(f, columns=cols, show="headings", height=22)
        headers = {
            "ts": ("Data/Hora", 140),
            "nome": ("Nome", 220),
            "empresa": ("Empresa", 220),
            "cnpj": ("CNPJ", 130),
            "procedencia": ("Procedência", 380),
            "msg_id": ("msg_id", 140),
        }
        for c, (txt, w) in headers.items():
            self.tree_audit.heading(c, text=txt)
            self.tree_audit.column(c, width=w, anchor="w")

        sb = ttk.Scrollbar(f, orient="vertical", command=self.tree_audit.yview)
        self.tree_audit.configure(yscrollcommand=sb.set)
        self.tree_audit.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        # Cores por categoria
        self.tree_audit.tag_configure("ok", background="#e8f5e9")
        self.tree_audit.tag_configure("pend_cli", background="#e3f2fd")
        self.tree_audit.tag_configure("pend_int", background="#fff3e0")
        self.tree_audit.tag_configure("falha", background="#ffebee")

    def _refresh_auditoria(self):
        if not hasattr(self, "tree_audit"):
            return
        for item in self.tree_audit.get_children():
            self.tree_audit.delete(item)
        if not PLANILHA_ADMISSOES.exists():
            return
        try:
            from openpyxl import load_workbook
            wb = load_workbook(PLANILHA_ADMISSOES, read_only=True)
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))
            if len(rows) <= 1:
                return
            filtro = (self.filtro_audit_var.get() or "").strip().lower()
            for row in reversed(rows[1:]):
                if not row or not any(row):
                    continue
                cols = list(row) + [""] * (6 - len(row))
                primeira = str(cols[0] or "")
                if ("-" in primeira and ":" in primeira) or primeira == "":
                    ts, nome, empresa, cnpj, procedencia, msg_id = cols[:6]
                else:
                    nome, empresa, cnpj, procedencia = cols[:4]
                    ts, msg_id = "", ""
                ts = str(ts or ""); nome = str(nome or ""); empresa = str(empresa or "")
                cnpj = str(cnpj or ""); procedencia = str(procedencia or "")
                msg_id = str(msg_id or "")
                hay = " ".join((ts, nome, empresa, cnpj, procedencia)).lower()
                if filtro and filtro not in hay:
                    continue
                proc_low = procedencia.lower()
                if proc_low.startswith("cadastrado") or proc_low.startswith("dry-run"):
                    tag = "ok"
                elif "interno" in proc_low:
                    tag = "pend_int"
                elif proc_low.startswith("pendente"):
                    tag = "pend_cli"
                else:
                    tag = "falha"
                self.tree_audit.insert(
                    "", "end",
                    values=(ts, nome, empresa, cnpj, procedencia, msg_id[:16] if msg_id else ""),
                    tags=(tag,),
                )
        except Exception as e:
            self.gui_q.put(("log", f"⚠ Erro lendo auditoria: {e}"))

    # ---- Aba Estatísticas ---------------------------------------

    def _build_estatisticas(self):
        f = ttk.Frame(self.tab_stats)
        f.pack(fill="both", expand=True, padx=15, pady=15)

        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Button(bar, text="🔄 Recalcular", command=self._refresh_estatisticas).pack(side="left")

        self.stats_text = scrolledtext.ScrolledText(
            f, font=("Consolas", 10), state="disabled", wrap="word",
        )
        self.stats_text.pack(fill="both", expand=True)

    def _refresh_estatisticas(self):
        if not hasattr(self, "stats_text"):
            return

        linhas = ["═" * 70, "  ESTATÍSTICAS DO PIPELINE", "═" * 70, ""]

        # Billing
        b = sum_billing_mes_atual()
        mes_atual = datetime.now().strftime("%B/%Y").upper()
        linhas += [
            f"💰 BILLING — {mes_atual}",
            f"   Custo total Claude:   US$ {b['custo_usd']:.4f}",
            f"   Chamadas API:         {b['n_calls']}",
            f"   Passadas do pipeline: {b['n_passadas']}",
            f"   Input tokens:         {b['input_tokens']:,}",
            f"   Output tokens:        {b['output_tokens']:,}",
            f"   Limite mensal:        US$ {self.billing_limite_usd:.2f}",
        ]
        if b["custo_usd"] >= self.billing_limite_usd:
            linhas.append(f"   ⚠ LIMITE EXCEDIDO! ({b['custo_usd'] / self.billing_limite_usd:.0%})")
        linhas.append("")

        # Stats da planilha
        if not PLANILHA_ADMISSOES.exists():
            linhas.append("(Planilha admissoes.xlsx ainda não existe.)")
            self._update_stats_text("\n".join(linhas))
            return

        try:
            from collections import Counter
            from openpyxl import load_workbook
            wb = load_workbook(PLANILHA_ADMISSOES, read_only=True)
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))[1:]
            rows = [r for r in rows if r and any(r)]

            cont_status = Counter()
            cont_empresa = Counter()
            cont_motivo = Counter()
            for row in rows:
                cols = list(row) + [""] * (6 - len(row))
                primeira = str(cols[0] or "")
                if ("-" in primeira and ":" in primeira) or primeira == "":
                    _ts, _nome, empresa, _cnpj, procedencia, _mid = cols[:6]
                else:
                    _nome, empresa, _cnpj, procedencia = cols[:4]
                empresa = str(empresa or "(sem empresa)")
                procedencia = str(procedencia or "")
                pl = procedencia.lower()
                if pl.startswith("cadastrado"):
                    cont_status["✅ Cadastrado"] += 1
                elif pl.startswith("dry-run"):
                    cont_status["🧪 Dry-run"] += 1
                elif "interno" in pl:
                    cont_status["🔧 Pendente interno"] += 1
                elif pl.startswith("pendente"):
                    cont_status["⚠ Pendente cliente"] += 1
                else:
                    cont_status["❌ Falha"] += 1
                cont_empresa[empresa] += 1
                # Motivo da pendência (primeira parte do procedência)
                if "—" in procedencia:
                    motivo = procedencia.split("—", 1)[1].strip()[:80]
                    if pl.startswith("pendente") or pl.startswith("falha"):
                        cont_motivo[motivo] += 1

            total = sum(cont_status.values())
            ok = cont_status.get("✅ Cadastrado", 0) + cont_status.get("🧪 Dry-run", 0)
            taxa_sucesso = (ok / total * 100) if total else 0

            linhas += [
                f"📊 TOTAL DE ADMISSÕES PROCESSADAS: {total}",
                f"   Taxa de sucesso: {taxa_sucesso:.1f}%",
                "",
                "  Por status:",
            ]
            for status, n in cont_status.most_common():
                pct = (n / total * 100) if total else 0
                bar_chr = "█" * int(pct / 3)
                linhas.append(f"    {status:30s} {n:4d}  {bar_chr} {pct:.1f}%")

            linhas += ["", "🏢 TOP 10 EMPRESAS:"]
            for emp, n in cont_empresa.most_common(10):
                linhas.append(f"   {n:4d}  {emp}")

            if cont_motivo:
                linhas += ["", "🔍 TOP 10 MOTIVOS DE PENDÊNCIA/FALHA:"]
                for motivo, n in cont_motivo.most_common(10):
                    linhas.append(f"   {n:4d}  {motivo}")

            if b["custo_usd"] > 0 and total > 0:
                custo_med = b["custo_usd"] / total
                linhas += ["", f"💵 Custo médio por admissão: US$ {custo_med:.4f}"]

        except Exception as e:
            linhas.append(f"\n⚠ Erro lendo planilha: {e}")

        self._update_stats_text("\n".join(linhas))

    def _update_stats_text(self, texto: str):
        self.stats_text.configure(state="normal")
        self.stats_text.delete("1.0", "end")
        self.stats_text.insert("1.0", texto)
        self.stats_text.configure(state="disabled")

    # ---- Aba Regras (editor de regras.json) ---------------------

    def _build_regras(self):
        f = ttk.Frame(self.tab_regras)
        f.pack(fill="both", expand=True, padx=15, pady=15)

        ttk.Label(f, text=f"Edite regras.json diretamente. Chaves começadas com '_' são docs/exemplos (ignoradas pelo pipeline).",
                  foreground="#666", wraplength=900).pack(anchor="w", pady=(0, 8))

        self.regras_text = scrolledtext.ScrolledText(
            f, font=("Consolas", 9), wrap="none",
        )
        self.regras_text.pack(fill="both", expand=True)

        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(10, 0))
        ttk.Button(bar, text="💾 Salvar", command=self._salvar_regras).pack(side="left", padx=5)
        ttk.Button(bar, text="🔄 Recarregar do disco", command=self._carregar_regras_no_editor).pack(side="left", padx=5)
        ttk.Button(bar, text="✓ Validar JSON", command=self._validar_regras_json).pack(side="left", padx=5)

        self._carregar_regras_no_editor()

    def _carregar_regras_no_editor(self):
        try:
            if REGRAS_FILE.exists():
                conteudo = REGRAS_FILE.read_text(encoding="utf-8")
            else:
                conteudo = "{}"
            self.regras_text.delete("1.0", "end")
            self.regras_text.insert("1.0", conteudo)
        except Exception as e:
            messagebox.showerror("Erro carregando regras.json", str(e))

    def _validar_regras_json(self):
        try:
            json.loads(self.regras_text.get("1.0", "end"))
            messagebox.showinfo("JSON válido", "Sintaxe OK!")
        except json.JSONDecodeError as e:
            messagebox.showerror("JSON inválido", str(e))

    def _salvar_regras(self):
        try:
            conteudo = self.regras_text.get("1.0", "end").strip()
            json.loads(conteudo)  # valida antes de salvar
        except json.JSONDecodeError as e:
            messagebox.showerror("JSON inválido — não salvou", str(e))
            return
        try:
            REGRAS_FILE.write_text(conteudo + "\n", encoding="utf-8")
            self.gui_q.put(("log", f"💾 regras.json salvo ({len(conteudo)} chars)"))
            messagebox.showinfo("Salvo", "regras.json salvo com sucesso!\n\n"
                                "Reinicie o pipeline pra que mudanças entrem em vigor.")
        except Exception as e:
            messagebox.showerror("Erro salvando", str(e))

    # ---- Toast notification + dark mode -------------------------

    def _notify_toast(self, titulo: str, msg: str):
        """Tenta toast Windows via plyer; fallback popup Tk topmost."""
        if _HAS_PLYER:
            try:
                _plyer_notif.notify(
                    title=titulo, message=msg, app_name="Crosara DP", timeout=6,
                )
                return
            except Exception:
                pass
        # Fallback: popup leve no canto
        popup = tk.Toplevel(self)
        popup.title(titulo)
        popup.attributes("-topmost", True)
        popup.geometry("400x100+100+100")
        ttk.Label(popup, text=titulo, font=("Segoe UI", 11, "bold")).pack(padx=15, pady=(10, 0))
        ttk.Label(popup, text=msg, wraplength=380).pack(padx=15, pady=(2, 10))
        popup.after(6000, popup.destroy)

    def _toggle_dark_mode(self):
        is_dark = bool(self.dark_mode_var.get())
        style = ttk.Style()
        if is_dark:
            bg, fg, sel = "#1e1e1e", "#d4d4d4", "#264f78"
            try:
                style.theme_use("clam")
            except tk.TclError:
                pass
            style.configure("Treeview", background=bg, foreground=fg, fieldbackground=bg)
            style.configure("Treeview.Heading", background="#2d2d2d", foreground=fg)
            style.map("Treeview", background=[("selected", sel)])
            self.configure(background=bg)
        else:
            style.theme_use(style.theme_names()[0])  # default
            style.configure("Treeview", background="white", foreground="black", fieldbackground="white")
            style.configure("Treeview.Heading", background="#f0f0f0", foreground="black")
            self.configure(background="SystemButtonFace")
        self.gui_q.put(("log", f"🎨 Modo {'escuro' if is_dark else 'claro'} ativado"))

    # ---- Resolver pendência ------------------------------------

    def _resolver_selecionada(self):
        sel = self.tree_pend.selection()
        if not sel:
            messagebox.showinfo("Selecione",
                                "Selecione uma pendência na tabela antes de resolver.")
            return
        values = self.tree_pend.item(sel[0])["values"]
        ResolverPendenciaDialog(
            parent=self,
            valores_linha=tuple(str(v) for v in values),
            config=self.config,
            gui_q=self.gui_q,
            on_resolved=self._refresh_tabelas,
        )

    # ---- Fechamento --------------------------------------------

    def _on_close(self):
        if self.polling:
            if not messagebox.askyesno("Polling ativo",
                                       "O polling está rodando. Tem certeza que quer fechar?\n"
                                       "(A passada atual termina antes de fechar)"):
                return
            self.polling = False
        self.destroy()


# ============================================================
# Diálogo: Resolver Pendência
# ============================================================

class ResolverPendenciaDialog(tk.Toplevel):
    """Dialog modal pra resolver uma pendência. 2 modos:

    1. **Form estruturado** (default quando procedência tem "faltam: A, B, C"):
       um Entry por campo faltante, com hint de formato. Submit injeta no
       payload e POSTa. Mais amigável que JSON cru.
    2. **Editor JSON** (fallback ou opt-in): edição livre do payload
       inteiro. Útil pra pendências internas ou casos complexos.

    Botão extra "Marcar como resolvido manualmente" pra quando DP
    cadastrou direto no eContador Desktop sem POST."""

    def __init__(self, parent, valores_linha, config, gui_q, on_resolved):
        super().__init__(parent)
        self.config = config
        self.gui_q = gui_q
        self.on_resolved = on_resolved
        self.valores = valores_linha
        # Compat: linhas novas têm 6 cols (com timestamp), antigas 5
        if len(valores_linha) == 6:
            ts, nome, empresa, cnpj, tipo, procedencia = valores_linha
        else:
            nome, empresa, cnpj, tipo, procedencia = valores_linha
            ts = ""

        self.title(f"Resolver Pendência — {nome}")
        self.geometry("950x720")
        self.transient(parent)

        # Header com info
        f_head = ttk.LabelFrame(self, text="Admissão", padding=12)
        f_head.pack(fill="x", padx=15, pady=(15, 8))
        ttk.Label(f_head, text=nome, font=("Segoe UI", 13, "bold")).pack(anchor="w")
        ttk.Label(f_head, text=f"Empresa: {empresa}").pack(anchor="w")
        ttk.Label(f_head, text=f"CNPJ: {cnpj}").pack(anchor="w")
        if ts:
            ttk.Label(f_head, text=f"Quando: {ts}").pack(anchor="w")
        cor_tipo = "#e65100" if tipo == "interno" else "#1565c0"
        ttk.Label(f_head, text=f"Tipo de pendência: {tipo.upper()}",
                  foreground=cor_tipo, font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(5, 0))
        ttk.Label(f_head, text=f"Motivo: {procedencia}",
                  wraplength=900, justify="left").pack(anchor="w", pady=(5, 0))

        # Procura o payload correspondente
        self.payload_path = self._achar_payload(nome)

        # Detecta campos faltando da procedência → habilita form estruturado
        # Padrões aceitos: "faltam: A, B, C", "faltam A, B e C", "falta X"
        self.campos_faltando = self._parsear_campos_faltando(procedencia)

        # Notebook interno: Form vs JSON
        nb_modo = ttk.Notebook(self)
        nb_modo.pack(fill="both", expand=True, padx=15, pady=(8, 0))

        # ---- Aba Form Estruturado ----
        f_form = ttk.Frame(nb_modo)
        nb_modo.add(f_form, text="📝 Form estruturado")
        self._build_form_estruturado(f_form)

        # ---- Aba JSON Editor ----
        f_json = ttk.Frame(nb_modo)
        nb_modo.add(f_json, text="{ }  Editor JSON")
        if self.payload_path:
            ttk.Label(f_json, text=f"Payload: {self.payload_path.name}",
                      foreground="#666").pack(anchor="w", padx=5, pady=(5, 3))
        else:
            ttk.Label(f_json, text="⚠ Sem payload em payloads/",
                      foreground="#c62828").pack(anchor="w", padx=5, pady=(5, 3))

        self.txt = scrolledtext.ScrolledText(f_json, font=("Consolas", 9), wrap="none")
        self.txt.pack(fill="both", expand=True, padx=5, pady=5)

        if self.payload_path:
            try:
                doc = json.loads(self.payload_path.read_text(encoding="utf-8"))
                payload = doc.get("payload") or {}
                self.txt.insert("1.0", json.dumps(payload, ensure_ascii=False, indent=2))
            except Exception as e:
                self.txt.insert("1.0", f"// Erro carregando payload:\n// {e}")
        else:
            self.txt.insert("1.0",
                "// Sem payload disponível.\n"
                "// Use 'Marcar como resolvido manualmente' se você cadastrou no eContador direto."
            )

        # Se há campos faltando, abre direto no form. Senão, no JSON.
        if self.campos_faltando:
            nb_modo.select(f_form)
        else:
            nb_modo.select(f_json)

        # Botões inferiores
        f_btn = ttk.Frame(self)
        f_btn.pack(fill="x", padx=15, pady=12)
        if self.payload_path:
            ttk.Button(f_btn, text="📤  Aplicar form e POSTar",
                       command=self._aplicar_form_e_postar).pack(side="left", padx=5)
            ttk.Button(f_btn, text="📤  POSTar JSON editado",
                       command=self._postar_json_cru).pack(side="left", padx=5)
        ttk.Button(f_btn, text="✅  Marcar como resolvido manualmente",
                   command=self._marcar_resolvido).pack(side="left", padx=5)
        ttk.Button(f_btn, text="Cancelar", command=self.destroy).pack(side="right", padx=5)

    def _parsear_campos_faltando(self, procedencia: str) -> list[str]:
        """Extrai lista de labels faltantes de 'Pendente cliente — faltam: A, B'.
        Retorna [] se não conseguir parsear."""
        import re
        m = re.search(r"falta[m]?:?\s*(.+?)(?:\.|$)", procedencia, re.IGNORECASE)
        if not m:
            return []
        bruto = m.group(1).strip()
        # Split por vírgula ou ' e '
        parts = re.split(r",\s*|\s+e\s+", bruto)
        return [p.strip() for p in parts if p.strip()]

    def _build_form_estruturado(self, parent: ttk.Frame):
        """Form com 1 Entry por campo faltante. Se não há campos parseáveis,
        mostra info amigável."""
        self.form_vars: dict[str, tk.StringVar] = {}

        canvas_frame = ttk.Frame(parent)
        canvas_frame.pack(fill="both", expand=True, padx=5, pady=5)

        if not self.campos_faltando:
            ttk.Label(
                canvas_frame,
                text=("Esta pendência não tem campos estruturados pra preencher "
                      "(provavelmente é pendência interna ou de Claude marcando "
                      "_pendente). Use a aba 'Editor JSON' pra editar o payload "
                      "manualmente, ou clique em 'Marcar como resolvido manualmente'."),
                wraplength=850,
                foreground="#666",
            ).pack(padx=10, pady=20)
            return

        ttk.Label(
            canvas_frame,
            text=f"Campos faltantes ({len(self.campos_faltando)}). "
                 f"Preencha e clique 'Aplicar form e POSTar':",
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w", pady=(0, 10))

        for label in self.campos_faltando:
            attr = LABEL_PRA_ATTR.get(label)
            row = ttk.Frame(canvas_frame)
            row.pack(fill="x", pady=4)
            ttk.Label(row, text=label, width=35, anchor="w").pack(side="left")

            v = tk.StringVar()
            self.form_vars[label] = v
            ent = ttk.Entry(row, textvariable=v, width=40)
            ent.pack(side="left", padx=5)

            hint = HINTS_FORM.get(attr or "", "")
            if hint:
                ttk.Label(row, text=hint, foreground="#888",
                          font=("Segoe UI", 8)).pack(side="left", padx=8)
            if not attr:
                ttk.Label(row, text="(label não mapeado — adicionar manualmente via JSON)",
                          foreground="#c62828", font=("Segoe UI", 8)).pack(side="left", padx=5)

    def _aplicar_form_e_postar(self):
        """Pega os valores do form, injeta nos attributes do payload e POSTa."""
        if not self.payload_path:
            messagebox.showerror("Sem payload", "Não encontrei payload pra esta pendência.")
            return

        try:
            doc = json.loads(self.payload_path.read_text(encoding="utf-8"))
        except Exception as e:
            messagebox.showerror("Erro lendo payload", str(e))
            return

        payload = doc.get("payload") or {}
        if "data" not in payload:
            messagebox.showerror("Payload inválido", "Estrutura sem 'data'.")
            return
        attrs = payload["data"].get("attributes") or {}

        # Aplica cada valor preenchido
        aplicados = []
        nao_mapeados = []
        for label, var in self.form_vars.items():
            valor = (var.get() or "").strip()
            if not valor:
                continue  # vazio = não muda
            attr = LABEL_PRA_ATTR.get(label)
            if not attr:
                nao_mapeados.append(label)
                continue
            # Conversão por tipo
            try:
                if attr in ("salario",):
                    valor_conv = float(valor.replace(",", "."))
                elif attr in ("cpf", "ctps", "diascontratoexperiencia"):
                    import re
                    digs = re.sub(r"\D", "", valor)
                    valor_conv = int(digs) if digs else None
                    if valor_conv is None:
                        raise ValueError(f"{label} precisa ter dígitos")
                elif attr == "primeiroemprego":
                    valor_conv = valor.lower() in ("true", "sim", "1", "y", "yes")
                else:
                    valor_conv = valor
            except Exception as e:
                messagebox.showerror(
                    "Valor inválido",
                    f"Campo '{label}' ({attr}): {e}\n\n"
                    f"Confira o formato e tente de novo."
                )
                return
            attrs[attr] = valor_conv
            aplicados.append(f"{label} = {valor_conv}")

        if nao_mapeados:
            messagebox.showwarning(
                "Campos não mapeados",
                f"Estes campos não pude aplicar automaticamente: {nao_mapeados}.\n"
                f"Use a aba 'Editor JSON' pra adicionar manualmente."
            )
            return

        if not aplicados:
            messagebox.showinfo("Nada pra aplicar", "Nenhum campo foi preenchido.")
            return

        payload["data"]["attributes"] = attrs

        if not messagebox.askyesno(
            "Confirmar POST",
            f"Vou aplicar {len(aplicados)} campo(s) e POSTar:\n\n"
            + "\n".join(f"  • {x}" for x in aplicados)
            + "\n\nConfirma?"
        ):
            return

        self._postar(payload)

    def _postar_json_cru(self):
        """POSTa o JSON do editor (sem usar form)."""
        try:
            payload = json.loads(self.txt.get("1.0", "end"))
        except json.JSONDecodeError as e:
            messagebox.showerror("JSON inválido", f"{e}")
            return
        if not messagebox.askyesno("Confirmar POST", "POSTar este payload em /candidatos?"):
            return
        self._postar(payload)

    def _achar_payload(self, nome: str) -> Path | None:
        """Procura em payloads/<ts>_<msgid>.json o arquivo cujo conteúdo
        contém esse nome (mais recente primeiro)."""
        if not PAYLOADS_DIR.exists() or not nome:
            return None
        nome_upper = nome.upper()
        files = sorted(PAYLOADS_DIR.glob("*.json"), reverse=True)
        for f in files:
            try:
                content = f.read_text(encoding="utf-8")
                if nome_upper in content.upper():
                    return f
            except Exception:
                pass
        return None

    def _postar(self, payload: dict):
        """POSTa o payload (já validado JSON) em /candidatos. Reusado pelos
        2 fluxos: form estruturado e editor JSON cru."""
        api = EContadorAPI(self.config.base_url, self.config.token)
        try:
            ok, ref, body_err = api.post_candidato(payload)
            if ok:
                # Extrai nome/empresa/cnpj/msg_id da linha pra append na planilha
                if len(self.valores) == 6:
                    _ts, nome, empresa, cnpj, _tipo, _proc = self.valores
                else:
                    nome, empresa, cnpj, _tipo, _proc = self.valores
                # Procura msg_id no payload original
                msg_id = ""
                if self.payload_path:
                    try:
                        doc = json.loads(self.payload_path.read_text(encoding="utf-8"))
                        msg_id = str(doc.get("msg_id", ""))
                    except Exception:
                        pass
                registrar_admissao_planilha(
                    nome=nome, empresa=empresa, cnpj=cnpj,
                    procedencia=f"Cadastrado — candidato {ref} (resolvido via UI)",
                    msg_id=msg_id,
                )
                messagebox.showinfo("Sucesso", f"Candidato criado: {ref}")
                self.gui_q.put(("log", f"✓ Pendência resolvida via UI → candidato {ref}"))
                self.on_resolved()
                self.destroy()
            else:
                messagebox.showerror("Falha no POST", f"{ref}\n\n{body_err[:800]}")
        finally:
            api.close()

    def _marcar_resolvido(self):
        if not messagebox.askyesno(
            "Confirmar",
            "Marcar essa pendência como resolvida manualmente?\n\n"
            "(NÃO faz POST — você marca quando já cadastrou no eContador direto.\n"
            "Aparece nova linha 'Cadastrado manualmente' na planilha.)"
        ):
            return
        # 5 cols (legacy) ou 6 cols (com timestamp)
        if len(self.valores) == 6:
            _ts, nome, empresa, cnpj, _tipo, _proc = self.valores
        else:
            nome, empresa, cnpj, _tipo, _proc = self.valores
        registrar_admissao_planilha(
            nome=nome, empresa=empresa, cnpj=cnpj,
            procedencia=f"Cadastrado manualmente — via UI ({datetime.now().strftime('%d/%m/%Y %H:%M')})",
        )
        self.gui_q.put(("log", f"✓ {nome} marcado como resolvido manualmente"))
        self.on_resolved()
        self.destroy()


# ============================================================
# Entry point
# ============================================================

def main():
    app = PipelineGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
