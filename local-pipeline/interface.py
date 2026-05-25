"""interface.py — GUI desktop pro pipeline de admissão (Tkinter).

Visual com identidade da Crosara Contabilidade:
  • Sidebar navy (#344E5C) com logo + nav
  • Conteúdo em bege (#F4DDC8) com cards brancos
  • Accent laranja (#D95C32) pra primary + active state
  • Log "terminal" em navy escuro com timestamps em laranja

6 abas (sidebar): Principal · Processadas · Pendentes · Auditoria ·
Estatísticas · Regras. Polling em thread separada, fila thread-safe
pra atualizar a UI. Reaproveita toda a lógica do main.py.

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

# ============================================================
# Paleta de cores Crosara (extraída do Logotipo Crosara - CMYK-04.jpg)
# ============================================================

COR_NAVY = "#344E5C"          # sidebar, header dark, log terminal
COR_NAVY_DARK = "#2A4050"     # log slightly darker
COR_NAVY_HOVER = "#3F5C6E"    # nav button hover
COR_NAVY_ACTIVE = "#2A4050"   # nav button when active (não usado, vai laranja)
COR_BEIGE = "#F4DDC8"         # main background
COR_BEIGE_DARK = "#E8C9AE"    # subtle differentiation
COR_WHITE = "#FFFFFF"          # cards
COR_ORANGE = "#D95C32"        # accent / primary / active nav
COR_ORANGE_DARK = "#B84A24"   # primary button hover
COR_TEXT_DARK = "#2A4050"     # text on light bg
COR_TEXT_LIGHT = "#F4DDC8"    # text on dark bg (matches beige)
COR_TEXT_MUTED = "#7A8896"    # muted/secondary text
COR_BORDER = "#D8C5AE"        # subtle border on cards
COR_SUCCESS = "#2E7D32"       # contador processadas (verde)
COR_WARNING = "#D95C32"       # contador pendentes (laranja Crosara)
COR_DANGER = "#C62828"        # contador >3d (vermelho)
COR_INFO = "#1565C0"          # custo claude (azul)

# Pillow opcional (logo na sidebar)
try:
    from PIL import Image, ImageTk
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

LOGO_PATH = Path(__file__).parent / "Logotipo Crosara - CMYK-04.jpg"


# ============================================================
# Paleta dark mode (slots espelham os do tema light)
# ============================================================

# Mapeamento por "slot" — cada cor da paleta light tem uma equivalente dark.
# O toggle dark mode varre o widget tree e troca bg/fg por essa correspondência.
_PALETA_LIGHT = {
    "main_bg":     COR_BEIGE,        # #F4DDC8
    "card_bg":     COR_WHITE,        # #FFFFFF
    "sidebar_bg":  COR_NAVY,         # #344E5C
    "log_bg":      COR_NAVY_DARK,    # #2A4050
    "sub_bg":      COR_BEIGE_DARK,   # #E8C9AE
    "text_dark":   COR_TEXT_DARK,    # #2A4050
    "text_light":  COR_TEXT_LIGHT,   # #F4DDC8
    "text_muted":  COR_TEXT_MUTED,   # #7A8896
    "border":      COR_BORDER,       # #D8C5AE
}

_PALETA_DARK = {
    "main_bg":     "#1E2128",
    "card_bg":     "#2A2D34",
    "sidebar_bg":  "#161922",
    "log_bg":      "#0F1117",
    "sub_bg":      "#252830",
    "text_dark":   "#E8E8E8",
    "text_light":  "#F4DDC8",       # accent claro mantém pra brilho
    "text_muted":  "#9098A4",
    "border":      "#3D404A",
}

from claude_client import ClaudeClient
from ecotador_client import AUDIT_FILE as ECONTADOR_AUDIT_FILE, EContadorAPI
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
        self.status_var = tk.StringVar(value="Parado")
        # Quando status muda, atualiza cor do dot da pill (verde/laranja/cinza)
        self.status_var.trace_add("write", lambda *a: self._atualizar_status_dot())
        self.ultima_var = tk.StringVar(value="—")
        self.proxima_var = tk.StringVar(value="—")
        self.auto_email_var = tk.BooleanVar(value=self.config.auto_email_pendencias)
        self.intervalo_var = tk.IntVar(value=self.config.intervalo)
        self.contador_proc_var = tk.StringVar(value="0")
        self.contador_pend_var = tk.StringVar(value="0")
        self.contador_velha_var = tk.StringVar(value="0")
        self.billing_var = tk.StringVar(value="US$ 0.00")
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
        self._aplicar_tema_crosara()
        self.configure(background=COR_BEIGE)

        # Layout: sidebar | (header + content)
        container = tk.Frame(self, bg=COR_BEIGE)
        container.pack(fill="both", expand=True)

        self._build_sidebar(container)

        # Área direita (header + conteúdo)
        right = tk.Frame(container, bg=COR_BEIGE)
        right.pack(side="left", fill="both", expand=True)
        self._build_header(right)

        # Holder do conteúdo (swap entre tabs)
        self.content_holder = tk.Frame(right, bg=COR_BEIGE)
        self.content_holder.pack(fill="both", expand=True, padx=20, pady=(0, 20))

        # Frames de cada "aba" (sem Notebook — sidebar nav controla)
        self.tab_main = tk.Frame(self.content_holder, bg=COR_BEIGE)
        self.tab_proc = tk.Frame(self.content_holder, bg=COR_BEIGE)
        self.tab_pend = tk.Frame(self.content_holder, bg=COR_BEIGE)
        self.tab_audit = tk.Frame(self.content_holder, bg=COR_BEIGE)
        self.tab_api = tk.Frame(self.content_holder, bg=COR_BEIGE)
        self.tab_stats = tk.Frame(self.content_holder, bg=COR_BEIGE)
        self.tab_regras = tk.Frame(self.content_holder, bg=COR_BEIGE)

        self._tab_frames = {
            "principal":   self.tab_main,
            "processadas": self.tab_proc,
            "pendentes":   self.tab_pend,
            "auditoria":   self.tab_audit,
            "api":         self.tab_api,
            "estatisticas": self.tab_stats,
            "regras":      self.tab_regras,
        }

        self._build_main()
        self._build_processadas()
        self._build_pendentes()
        self._build_auditoria()
        self._build_api_econtador()
        self._build_estatisticas()
        self._build_regras()

        # Mostra Principal por padrão
        self._switch_tab("principal")

    def _aplicar_tema_crosara(self):
        """Configura ttk.Style com a paleta da marca."""
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        # Frame, Label
        style.configure("TFrame", background=COR_BEIGE)
        style.configure("TLabel", background=COR_BEIGE, foreground=COR_TEXT_DARK)
        style.configure("TLabelframe", background=COR_BEIGE, foreground=COR_TEXT_DARK,
                        bordercolor=COR_BORDER, relief="solid")
        style.configure("TLabelframe.Label", background=COR_BEIGE, foreground=COR_TEXT_DARK)

        # Cards (frames brancos)
        style.configure("Card.TFrame", background=COR_WHITE, relief="solid", borderwidth=1)
        style.configure("Card.TLabel", background=COR_WHITE, foreground=COR_TEXT_DARK)

        # Buttons
        style.configure("TButton", background=COR_WHITE, foreground=COR_TEXT_DARK,
                        padding=8, borderwidth=1, focusthickness=0)
        style.map("TButton",
                  background=[("active", COR_BEIGE_DARK)],
                  foreground=[("active", COR_TEXT_DARK)])

        # Primary button (laranja)
        style.configure("Primary.TButton", background=COR_ORANGE, foreground=COR_WHITE,
                        padding=10, borderwidth=0, font=("Segoe UI", 10, "bold"))
        style.map("Primary.TButton",
                  background=[("active", COR_ORANGE_DARK), ("disabled", "#C9C9C9")],
                  foreground=[("disabled", "#888")])

        # Treeview
        style.configure("Treeview",
                        background=COR_WHITE, fieldbackground=COR_WHITE,
                        foreground=COR_TEXT_DARK, rowheight=26, bordercolor=COR_BORDER)
        style.configure("Treeview.Heading",
                        background=COR_NAVY, foreground=COR_WHITE,
                        font=("Segoe UI", 9, "bold"), padding=5, relief="flat")
        style.map("Treeview",
                  background=[("selected", COR_ORANGE)],
                  foreground=[("selected", COR_WHITE)])

        # Entry, Combobox, Spinbox
        for w in ("TEntry", "TCombobox", "TSpinbox"):
            style.configure(w, fieldbackground=COR_WHITE, foreground=COR_TEXT_DARK,
                            bordercolor=COR_BORDER, relief="solid")

        # Notebook (usado só no ResolverPendenciaDialog)
        style.configure("TNotebook", background=COR_BEIGE, borderwidth=0)
        style.configure("TNotebook.Tab", background=COR_BEIGE_DARK, foreground=COR_TEXT_DARK,
                        padding=(12, 6))
        style.map("TNotebook.Tab",
                  background=[("selected", COR_WHITE)],
                  foreground=[("selected", COR_TEXT_DARK)])

        # Checkbutton, Scrollbar
        style.configure("TCheckbutton", background=COR_BEIGE, foreground=COR_TEXT_DARK)
        style.configure("TScrollbar", background=COR_BEIGE, troughcolor=COR_BEIGE_DARK,
                        bordercolor=COR_BEIGE_DARK, arrowcolor=COR_NAVY)

    def _build_sidebar(self, parent: tk.Frame):
        side = tk.Frame(parent, bg=COR_NAVY, width=230)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)

        # Logo header
        header = tk.Frame(side, bg=COR_NAVY, height=90)
        header.pack(fill="x", pady=(20, 30), padx=15)
        header.pack_propagate(False)

        logo_img = self._carregar_logo_sidebar()
        if logo_img:
            self._logo_ref = logo_img  # evita garbage collect
            tk.Label(header, image=logo_img, bg=COR_NAVY).pack(side="left", padx=(0, 8))
            txt_frame = tk.Frame(header, bg=COR_NAVY)
            txt_frame.pack(side="left", anchor="center")
            tk.Label(txt_frame, text="CROSARA", bg=COR_NAVY, fg=COR_TEXT_LIGHT,
                     font=("Segoe UI", 14, "bold")).pack(anchor="w")
            tk.Label(txt_frame, text="CONTABILIDADE", bg=COR_NAVY, fg=COR_ORANGE,
                     font=("Segoe UI", 8, "bold")).pack(anchor="w")
        else:
            # Fallback texto
            tk.Label(header, text="CROSARA", bg=COR_NAVY, fg=COR_TEXT_LIGHT,
                     font=("Segoe UI", 18, "bold")).pack(anchor="w")
            tk.Label(header, text="CONTABILIDADE", bg=COR_NAVY, fg=COR_ORANGE,
                     font=("Segoe UI", 9, "bold")).pack(anchor="w")

        # Nav buttons
        self._nav_buttons: dict[str, tk.Button] = {}
        nav_items = [
            ("principal",   "Principal"),
            ("processadas", "Processadas"),
            ("pendentes",   "Pendentes"),
            ("auditoria",   "Auditoria"),
            ("api",         "API eContador"),
            ("estatisticas", "Estatísticas"),
            ("regras",      "Regras"),
        ]
        for key, label in nav_items:
            btn = tk.Button(
                side, text=f"   {label}", anchor="w",
                bg=COR_NAVY, fg=COR_TEXT_LIGHT,
                activebackground=COR_NAVY_HOVER, activeforeground=COR_WHITE,
                bd=0, relief="flat", padx=20, pady=10,
                font=("Segoe UI", 11),
                cursor="hand2",
                command=lambda k=key: self._switch_tab(k),
            )
            btn.pack(fill="x", padx=10, pady=2)
            self._nav_buttons[key] = btn

    def _carregar_logo_sidebar(self):
        if not _HAS_PIL or not LOGO_PATH.exists():
            return None
        try:
            img = Image.open(LOGO_PATH)
            w, h = img.size
            target_h = 50
            new_w = int(w * target_h / h)
            img = img.resize((new_w, target_h), Image.LANCZOS)
            return ImageTk.PhotoImage(img)
        except Exception:
            return None

    def _build_header(self, parent: tk.Frame):
        header = tk.Frame(parent, bg=COR_BEIGE, height=70)
        header.pack(fill="x", padx=20, pady=(20, 10))
        header.pack_propagate(False)

        self._titulo_var = tk.StringVar(value="Pipeline de Admissão")
        tk.Label(header, textvariable=self._titulo_var, bg=COR_BEIGE,
                 fg=COR_TEXT_DARK, font=("Segoe UI", 20, "bold")).pack(side="left")

        # Status pill à direita
        self._status_pill = tk.Frame(header, bg=COR_WHITE, padx=15, pady=6, bd=1, relief="solid")
        self._status_pill.pack(side="right", pady=10)
        self._status_dot = tk.Label(self._status_pill, text="●", bg=COR_WHITE,
                                    fg="#888", font=("Segoe UI", 14))
        self._status_dot.pack(side="left", padx=(0, 6))
        tk.Label(self._status_pill, textvariable=self.status_var, bg=COR_WHITE,
                 fg=COR_TEXT_DARK, font=("Segoe UI", 10, "bold")).pack(side="left")

    def _switch_tab(self, name: str):
        """Mostra o frame da tab `name`, esconde os outros, destaca o botão."""
        for n, f in self._tab_frames.items():
            if n == name:
                f.pack(fill="both", expand=True)
            else:
                f.pack_forget()

        # Update sidebar — botão ativo em laranja
        for k, btn in self._nav_buttons.items():
            if k == name:
                btn.configure(bg=COR_ORANGE, fg=COR_WHITE,
                              activebackground=COR_ORANGE_DARK,
                              activeforeground=COR_WHITE)
            else:
                btn.configure(bg=COR_NAVY, fg=COR_TEXT_LIGHT,
                              activebackground=COR_NAVY_HOVER,
                              activeforeground=COR_WHITE)

        # Update title
        titulos = {
            "principal": "Pipeline de Admissão",
            "processadas": "Admissões Processadas",
            "pendentes": "Admissões Pendentes",
            "auditoria": "Auditoria",
            "api": "API eContador — Chamadas HTTP",
            "estatisticas": "Estatísticas",
            "regras": "Regras de Negócio",
        }
        self._titulo_var.set(titulos.get(name, "Pipeline"))

    def _build_main(self):
        # Stats: 4 cards lado a lado
        stats_row = tk.Frame(self.tab_main, bg=COR_BEIGE)
        stats_row.pack(fill="x", pady=(10, 15))

        self._card_stat(stats_row, "Processadas", self.contador_proc_var, COR_SUCCESS).pack(
            side="left", fill="both", expand=True, padx=(0, 10))
        self._card_stat(stats_row, "Pendentes", self.contador_pend_var, COR_WARNING).pack(
            side="left", fill="both", expand=True, padx=10)
        self._card_stat(stats_row, f"Pendência >{DIAS_PENDENCIA_VELHA}d",
                        self.contador_velha_var, COR_DANGER).pack(
            side="left", fill="both", expand=True, padx=10)
        billing_card = self._card_stat(stats_row, "Custo Claude/mês",
                                       self.billing_var, COR_INFO, valor_font_size=18)
        billing_card.pack(side="left", fill="both", expand=True, padx=(10, 0))
        # Referência pro label de valor pra mudar cor (alerta de limite)
        self.billing_label = billing_card._valor_label  # set dentro de _card_stat

        # Controle (card branco)
        f_ctrl_card = tk.Frame(self.tab_main, bg=COR_WHITE, bd=1, relief="solid",
                                highlightbackground=COR_BORDER, highlightthickness=0)
        f_ctrl_card.pack(fill="x", pady=10)
        tk.Label(f_ctrl_card, text="Controle", bg=COR_WHITE, fg=COR_TEXT_DARK,
                 font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=15, pady=(12, 8))

        row1 = tk.Frame(f_ctrl_card, bg=COR_WHITE)
        row1.pack(fill="x", padx=15, pady=(0, 6))
        self.btn_iniciar = ttk.Button(row1, text="Iniciar polling",
                                       style="Primary.TButton",
                                       command=self._iniciar_polling)
        self.btn_iniciar.pack(side="left", padx=(0, 8))
        self.btn_parar = ttk.Button(row1, text="Parar",
                                     command=self._parar_polling, state="disabled")
        self.btn_parar.pack(side="left", padx=8)
        ttk.Button(row1, text="Rodar 1 passada", command=self._rodar_unica).pack(side="left", padx=8)

        row2 = tk.Frame(f_ctrl_card, bg=COR_WHITE)
        row2.pack(fill="x", padx=15, pady=(0, 12))
        ttk.Button(row2, text="Backup agora", command=self._backup_agora).pack(side="left", padx=(0, 8))
        ttk.Button(row2, text="Atualizar tabelas", command=self._refresh_tabelas).pack(side="left", padx=8)

        # Configurações (card)
        f_set = tk.Frame(self.tab_main, bg=COR_WHITE, bd=1, relief="solid")
        f_set.pack(fill="x", pady=10)
        tk.Label(f_set, text="Configurações", bg=COR_WHITE, fg=COR_TEXT_DARK,
                 font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=15, pady=(12, 8))

        ttk.Checkbutton(
            f_set,
            text="Enviar email de pendência automaticamente pro cliente "
                 "(APENAS pra campos externos — internas sempre manuais)",
            variable=self.auto_email_var,
            command=self._on_auto_email_change,
        ).pack(anchor="w", padx=15, pady=3)

        intv = tk.Frame(f_set, bg=COR_WHITE)
        intv.pack(anchor="w", padx=15, pady=(8, 0))
        tk.Label(intv, text="Intervalo de polling (segundos):",
                 bg=COR_WHITE, fg=COR_TEXT_DARK).pack(side="left")
        ttk.Spinbox(intv, from_=60, to=3600, increment=30,
                    textvariable=self.intervalo_var, width=8).pack(side="left", padx=8)
        tk.Label(intv, text="(default 300 = 5 minutos)",
                 bg=COR_WHITE, fg=COR_TEXT_MUTED, font=("Segoe UI", 8)).pack(side="left")

        ttk.Checkbutton(
            f_set, text="Modo escuro",
            variable=self.dark_mode_var,
            command=self._toggle_dark_mode,
        ).pack(anchor="w", padx=15, pady=(8, 12))

        # Atividade recente (log) — terminal style com brand colors
        f_log_card = tk.Frame(self.tab_main, bg=COR_NAVY_DARK, bd=0)
        f_log_card.pack(fill="both", expand=True, pady=(10, 0))
        tk.Label(f_log_card, text="Atividade recente",
                 bg=COR_NAVY_DARK, fg=COR_TEXT_MUTED,
                 font=("Segoe UI", 10)).pack(anchor="w", padx=15, pady=(10, 5))

        self.log_text = tk.Text(
            f_log_card, height=14, font=("Consolas", 10),
            bg=COR_NAVY_DARK, fg=COR_BEIGE, bd=0, padx=15, pady=10,
            insertbackground=COR_BEIGE, wrap="word",
            state="disabled",
        )
        self.log_text.pack(fill="both", expand=True, padx=0, pady=(0, 10))
        # Tag pra colorir timestamps em laranja
        self.log_text.tag_configure("ts", foreground=COR_ORANGE)

    def _card_stat(self, parent, titulo, var_valor, cor_valor, valor_font_size=28):
        """Cria um card branco com título cinza e valor grande colorido."""
        card = tk.Frame(parent, bg=COR_WHITE, bd=1, relief="solid",
                        highlightbackground=COR_BORDER, highlightthickness=0)
        tk.Label(card, text=titulo, bg=COR_WHITE, fg=COR_TEXT_MUTED,
                 font=("Segoe UI", 10)).pack(anchor="w", padx=18, pady=(15, 4))
        lbl = tk.Label(card, textvariable=var_valor, bg=COR_WHITE, fg=cor_valor,
                       font=("Segoe UI", valor_font_size, "bold"))
        lbl.pack(anchor="w", padx=18, pady=(0, 15))
        card._valor_label = lbl  # pra alterar cor depois (alerta billing)
        return card

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
        self.log_text.configure(state="normal")
        # Timestamp em laranja, resto em bege
        self.log_text.insert("end", f"[{ts}] ", ("ts",))
        self.log_text.insert("end", f"{msg}\n")
        self.log_text.see("end")
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
        self.gui_q.put(("status", "Parando..."))
        self.gui_q.put(("log", "Pedido de parada — vai parar após a passada atual"))

    def _rodar_unica(self):
        if self.polling:
            messagebox.showinfo("Polling ativo",
                                "O polling já está rodando passadas automaticamente.\nPara rodar uma extra, primeiro pare o polling.")
            return
        threading.Thread(target=self._executar_passada, daemon=True).start()

    def _executar_passada(self):
        self.gui_q.put(("status", "Executando..."))
        self.gui_q.put(("log", "Iniciando passada única..."))
        try:
            rodar_uma_passada(self.config, self.claude, self.planilha)
            self.gui_q.put(("log", "Passada única concluída"))
        except Exception as e:
            self.gui_q.put(("log", f"Erro na passada: {e}"))
        self.gui_q.put(("status", "Parado"))
        self.gui_q.put(("ultima", datetime.now().strftime("%d/%m/%Y %H:%M:%S")))
        self.gui_q.put(("refresh", None))

    def _loop_polling(self):
        while self.polling:
            self.gui_q.put(("status", "Executando passada..."))
            self.gui_q.put(("log", "Iniciando passada..."))
            try:
                rodar_uma_passada(self.config, self.claude, self.planilha)
                self.gui_q.put(("log", "Passada concluída"))
            except Exception as e:
                self.gui_q.put(("log", f"Erro na passada: {e}"))

            self.gui_q.put(("ultima", datetime.now().strftime("%d/%m/%Y %H:%M:%S")))
            self.gui_q.put(("refresh", None))

            t_end = time.time() + self.config.intervalo
            while time.time() < t_end and self.polling:
                t_left = int(t_end - time.time())
                self.gui_q.put(("status", f"Aguardando próxima ({t_left}s)"))
                self.gui_q.put(("proxima", datetime.fromtimestamp(t_end).strftime("%H:%M:%S")))
                time.sleep(1)

        self.gui_q.put(("status", "Parado"))
        self.gui_q.put(("proxima", "—"))
        self.gui_q.put(("log", "Polling parado"))

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
            if hasattr(self, "tree_api"):
                self._refresh_api_econtador()
            if hasattr(self, "stats_body"):
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
            # Compacto pro card (no painel Estatísticas mostra os detalhes)
            self.billing_var.set(f"US$ {custo:.2f}")
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

    # ---- Aba API eContador (auditoria das chamadas HTTP) ----------

    def _build_api_econtador(self):
        f = self.tab_api

        # Toolbar
        bar = tk.Frame(f, bg=COR_BEIGE)
        bar.pack(fill="x", pady=(10, 8))
        ttk.Button(bar, text="🔄  Atualizar", command=self._refresh_api_econtador).pack(side="left", padx=(0, 8))
        ttk.Button(bar, text="👁  Ver JSON completo (seleção)",
                   command=self._abrir_detalhe_api).pack(side="left", padx=8)
        ttk.Button(bar, text="📂  Abrir arquivo audit",
                   command=self._abrir_arquivo_audit).pack(side="left", padx=8)

        # Filtro
        self.filtro_api_var = tk.StringVar()
        self.filtro_api_var.trace_add("write", lambda *a: self._refresh_api_econtador())
        ttk.Label(bar, text="  Buscar:").pack(side="left", padx=(20, 4))
        ttk.Entry(bar, textvariable=self.filtro_api_var, width=30).pack(side="left")

        self.apenas_falhas_var = tk.BooleanVar(value=False)
        self.apenas_falhas_var.trace_add("write", lambda *a: self._refresh_api_econtador())
        ttk.Checkbutton(bar, text="Apenas falhas",
                        variable=self.apenas_falhas_var).pack(side="left", padx=15)

        # Contador resumido
        self.api_resumo_var = tk.StringVar(value="—")
        tk.Label(bar, textvariable=self.api_resumo_var,
                 bg=COR_BEIGE, fg=COR_TEXT_MUTED,
                 font=("Segoe UI", 9)).pack(side="right", padx=(0, 8))

        tk.Label(f, text="(Clique 2× na linha pra ver o JSON completo BEFORE+AFTER)",
                 bg=COR_BEIGE, fg=COR_TEXT_MUTED,
                 font=("Segoe UI", 9)).pack(anchor="w", pady=(0, 5))

        # Tabela
        cols = ("ts", "corr_id", "operation", "status", "duration", "resumo")
        self.tree_api = ttk.Treeview(f, columns=cols, show="headings", height=22)
        headers = {
            "ts":        ("Quando", 150),
            "corr_id":   ("Corr ID", 80),
            "operation": ("Operação", 150),
            "status":    ("Status", 80),
            "duration":  ("Duração", 80),
            "resumo":    ("Resumo", 480),
        }
        for c, (txt, w) in headers.items():
            self.tree_api.heading(c, text=txt)
            self.tree_api.column(c, width=w, anchor="w")

        sb = ttk.Scrollbar(f, orient="vertical", command=self.tree_api.yview)
        self.tree_api.configure(yscrollcommand=sb.set)
        self.tree_api.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.tree_api.bind("<Double-1>", lambda e: self._abrir_detalhe_api())

        # Tags por status
        self.tree_api.tag_configure("ok",       background="#E8F5E9", foreground=COR_TEXT_DARK)
        self.tree_api.tag_configure("fail",     background="#FFEBEE", foreground=COR_TEXT_DARK)
        self.tree_api.tag_configure("slow",     background="#FFF8E1")
        self.tree_api.tag_configure("orphan",   background="#F5F5F5")  # before sem after

        # Cache pareado: corr_id → {"before": entry, "after": entry}
        self._api_entries_cache: dict[str, dict] = {}

    def _refresh_api_econtador(self):
        if not hasattr(self, "tree_api"):
            return
        for item in self.tree_api.get_children():
            self.tree_api.delete(item)

        if not ECONTADOR_AUDIT_FILE.exists():
            self.api_resumo_var.set("(econtador_audit.ndjson ainda não existe)")
            self._api_entries_cache = {}
            return

        # Lê todas as linhas e agrupa por corr_id
        pares: dict[str, dict] = {}
        try:
            with open(ECONTADOR_AUDIT_FILE, "r", encoding="utf-8") as fh:
                for linha in fh:
                    linha = linha.strip()
                    if not linha:
                        continue
                    try:
                        e = json.loads(linha)
                    except json.JSONDecodeError:
                        continue
                    cid = e.get("corr_id") or ""
                    phase = e.get("phase") or ""
                    if not cid or phase not in ("before", "after"):
                        continue
                    pares.setdefault(cid, {})[phase] = e
        except OSError as exc:
            self.api_resumo_var.set(f"Erro lendo: {exc}")
            return

        self._api_entries_cache = pares

        # Conta totais
        total = len(pares)
        falhas = sum(
            1 for p in pares.values()
            if "after" in p and not p["after"].get("success", True)
        )
        slow = sum(
            1 for p in pares.values()
            if "after" in p and (p["after"].get("duration_ms") or 0) >= 5000
        )

        # Filtros
        filtro = (self.filtro_api_var.get() or "").strip().lower()
        apenas_falhas = bool(self.apenas_falhas_var.get())

        # Ordena por timestamp (do after se existir, senão before)
        def _ts(par):
            return (par.get("after") or par.get("before") or {}).get("timestamp", "")

        ordenados = sorted(pares.items(), key=lambda kv: _ts(kv[1]), reverse=True)

        mostrados = 0
        for cid, par in ordenados:
            before = par.get("before") or {}
            after = par.get("after")
            op = (before.get("operation")
                  or (after.get("operation") if after else "?"))
            ts_completo = _ts(par)
            ts_display = ts_completo[:19].replace("T", " ") if ts_completo else "?"

            if after is None:
                # Orphan — só BEFORE
                status_txt = "..."
                tag = "orphan"
                duration = "—"
                resumo = "BEFORE sem AFTER (processo crashou?)"
            else:
                ok = bool(after.get("success"))
                code = after.get("status_code", "?")
                if ok:
                    status_txt = f"✓ {code}"
                    tag = "ok"
                else:
                    status_txt = f"✗ {code}"
                    tag = "fail"
                dur_ms = after.get("duration_ms")
                duration = f"{dur_ms} ms" if dur_ms is not None else "—"
                if dur_ms and dur_ms >= 5000 and tag == "ok":
                    tag = "slow"
                resumo = self._resumo_after(op, after, before)

            if apenas_falhas and tag != "fail":
                continue
            hay = " ".join((ts_display, cid, op, status_txt, duration, resumo)).lower()
            if filtro and filtro not in hay:
                continue

            self.tree_api.insert(
                "", "end",
                iid=cid,
                values=(ts_display, cid, op, status_txt, duration, resumo[:200]),
                tags=(tag,),
            )
            mostrados += 1

        self.api_resumo_var.set(
            f"{mostrados} chamada(s) visível(eis) | "
            f"total {total} · falhas {falhas} · lentas (>5s) {slow}"
        )

    @staticmethod
    def _resumo_after(op: str, after: dict, before: dict) -> str:
        """Constrói o resumo da linha baseado na operação."""
        if "EXCEPTION" in (after.get("exception") or "") or after.get("exception"):
            return f"EXCEPTION: {after.get('exception')[:150]}"
        if op == "POST /candidatos":
            if after.get("success"):
                snap = (after.get("input_snapshot") or {})
                cid = after.get("candidato_id", "?")
                return f"candidato_id={cid} | nome={snap.get('nome', '?')}"
            body = after.get("body_preview") or ""
            snap = (after.get("input_snapshot") or {})
            return f"nome={snap.get('nome', '?')} | erro: {body[:120]}"
        if op == "GET /empresas":
            if after.get("success"):
                if after.get("empresa_id"):
                    return f"empresa_id={after['empresa_id']} | razão: {after.get('razao_social', '?')}"
                return "0 resultados"
            inp = before.get("input") or {}
            return f"cnpj={inp.get('cnpj', '?')} | erro {after.get('status_code')}"
        if op == "GET /departamentos":
            if after.get("success"):
                return f"{after.get('n_resultados', 0)} departamento(s)"
            inp = before.get("input") or {}
            return f"empresa_id={inp.get('empresa_id', '?')} | erro {after.get('status_code')}"
        # Default
        return str(after)[:120]

    def _abrir_detalhe_api(self):
        sel = self.tree_api.selection()
        if not sel:
            messagebox.showinfo("Selecione",
                                "Selecione uma linha na tabela primeiro.")
            return
        cid = sel[0]
        par = self._api_entries_cache.get(cid)
        if not par:
            messagebox.showinfo("Não encontrado", f"Sem dados pro corr_id {cid}")
            return

        # Modal com 2 painéis: BEFORE | AFTER
        win = tk.Toplevel(self)
        win.title(f"Auditoria eContador — corr_id {cid}")
        win.geometry("1100x650")
        win.transient(self)

        before = par.get("before") or {}
        after = par.get("after")

        head = tk.Frame(win, bg=COR_WHITE, padx=15, pady=12)
        head.pack(fill="x")
        tk.Label(head, text=f"Operação: {before.get('operation') or (after or {}).get('operation', '?')}",
                 font=("Segoe UI", 12, "bold"), bg=COR_WHITE).pack(anchor="w")
        tk.Label(head, text=f"corr_id: {cid}",
                 fg=COR_TEXT_MUTED, bg=COR_WHITE).pack(anchor="w")
        if after:
            ok = after.get("success")
            cor = COR_SUCCESS if ok else COR_DANGER
            tk.Label(head,
                     text=f"Resultado: {'✓ SUCESSO' if ok else '✗ FALHA'} "
                          f"(status {after.get('status_code', '?')}, "
                          f"{after.get('duration_ms', '?')} ms)",
                     fg=cor, bg=COR_WHITE,
                     font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(4, 0))
        else:
            tk.Label(head, text="(sem AFTER — processo crashou antes de receber resposta?)",
                     fg=COR_DANGER, bg=COR_WHITE).pack(anchor="w", pady=(4, 0))

        body = tk.Frame(win, bg=COR_BEIGE)
        body.pack(fill="both", expand=True, padx=10, pady=10)
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        # BEFORE panel
        f_b = tk.Frame(body, bg=COR_WHITE, bd=1, relief="solid")
        f_b.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        tk.Label(f_b, text="BEFORE", font=("Segoe UI", 11, "bold"),
                 bg=COR_WHITE, fg=COR_ORANGE).pack(anchor="w", padx=10, pady=(10, 5))
        txt_b = scrolledtext.ScrolledText(f_b, font=("Consolas", 9), wrap="none")
        txt_b.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        txt_b.insert("1.0", json.dumps(before, ensure_ascii=False, indent=2))
        txt_b.configure(state="disabled")

        # AFTER panel
        f_a = tk.Frame(body, bg=COR_WHITE, bd=1, relief="solid")
        f_a.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        tk.Label(f_a, text="AFTER", font=("Segoe UI", 11, "bold"),
                 bg=COR_WHITE, fg=COR_SUCCESS if (after and after.get("success")) else COR_DANGER
                 ).pack(anchor="w", padx=10, pady=(10, 5))
        txt_a = scrolledtext.ScrolledText(f_a, font=("Consolas", 9), wrap="none")
        txt_a.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        if after:
            txt_a.insert("1.0", json.dumps(after, ensure_ascii=False, indent=2))
        else:
            txt_a.insert("1.0", "(sem registro de AFTER)")
        txt_a.configure(state="disabled")

        ttk.Button(win, text="Fechar", command=win.destroy).pack(pady=10)

    def _abrir_arquivo_audit(self):
        if ECONTADOR_AUDIT_FILE.exists():
            import os
            os.startfile(ECONTADOR_AUDIT_FILE)
        else:
            messagebox.showinfo("Sem arquivo", "econtador_audit.ndjson ainda não foi criado (nenhuma chamada eContador feita ainda).")

    # ---- Aba Estatísticas (redesign com cards + barras) -----------

    # Cores adicionais usadas nas barras de status (paleta complementar
    # ao tema Crosara, similar à proposta enviada pelo usuário)
    _COR_TRILHA = "#F1EFE8"     # fundo cinza claro das barras
    _COR_VERDE_OK = "#4E8A6B"   # cadastrado
    _COR_AMBAR = "#E0A030"      # pendente interno
    _COR_BADGE_TXT = "#B25529"  # texto do badge de motivo

    def _build_estatisticas(self):
        f = self.tab_stats

        # Toolbar superior (botão Recalcular à direita)
        toolbar = tk.Frame(f, bg=COR_BEIGE)
        toolbar.pack(fill="x", pady=(10, 12))
        ttk.Button(toolbar, text="↻  Recalcular",
                   command=self._refresh_estatisticas).pack(side="right")

        # Container scrollável (conteúdo pode ser longo)
        wrap = tk.Frame(f, bg=COR_BEIGE)
        wrap.pack(fill="both", expand=True)

        canvas = tk.Canvas(wrap, bg=COR_BEIGE, highlightthickness=0)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        self.stats_body = tk.Frame(canvas, bg=COR_BEIGE)

        self.stats_body.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        _win_id = canvas.create_window((0, 0), window=self.stats_body, anchor="nw")
        canvas.bind(
            "<Configure>",
            lambda e: canvas.itemconfigure(_win_id, width=e.width),
        )
        canvas.configure(yscrollcommand=sb.set)

        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        # Scroll com mousewheel
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

    def _refresh_estatisticas(self):
        if not hasattr(self, "stats_body"):
            return

        # Limpa conteúdo anterior
        for w in self.stats_body.winfo_children():
            w.destroy()

        body = self.stats_body
        b = sum_billing_mes_atual()
        mes = datetime.now().strftime("%B / %Y").capitalize()

        # ===== BILLING SECTION =====
        tk.Label(body, text=f"BILLING — {mes.upper()}",
                 bg=COR_BEIGE, fg=COR_TEXT_MUTED,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(4, 8))

        # 4 cards de métrica
        metricas = tk.Frame(body, bg=COR_BEIGE)
        metricas.pack(fill="x", pady=(0, 10))
        for i in range(4):
            metricas.grid_columnconfigure(i, weight=1)

        def _metric(col, label, valor, cor_valor):
            c = tk.Frame(metricas, bg=COR_WHITE, bd=1, relief="solid",
                         highlightbackground=COR_BORDER, highlightthickness=0)
            c.grid(row=0, column=col, sticky="nsew",
                   padx=(0 if col == 0 else 6, 6 if col < 3 else 0))
            tk.Label(c, text=label, bg=COR_WHITE, fg=COR_TEXT_MUTED,
                     font=("Segoe UI", 10)).pack(anchor="w", padx=14, pady=(12, 2))
            tk.Label(c, text=valor, bg=COR_WHITE, fg=cor_valor,
                     font=("Segoe UI", 19, "bold")).pack(anchor="w", padx=14, pady=(0, 12))

        custo_total = b["custo_usd"]
        custo_med = (custo_total / b["n_passadas"]) if b["n_passadas"] else 0.0
        _metric(0, "Custo total", f"US$ {custo_total:.2f}", COR_ORANGE)
        _metric(1, "Chamadas API", str(b["n_calls"]), COR_TEXT_DARK)
        _metric(2, "Passadas", str(b["n_passadas"]), COR_TEXT_DARK)
        _metric(3, "Custo médio/adm.", f"US$ {custo_med:.3f}", COR_TEXT_DARK)

        # 3 kv-cards (tokens + limite)
        kv = tk.Frame(body, bg=COR_BEIGE)
        kv.pack(fill="x", pady=(0, 16))
        for i in range(3):
            kv.grid_columnconfigure(i, weight=1)

        def _kv(col, label, valor, valor_cor=COR_TEXT_DARK):
            c = tk.Frame(kv, bg=COR_WHITE, bd=1, relief="solid")
            c.grid(row=0, column=col, sticky="nsew",
                   padx=(0 if col == 0 else 6, 6 if col < 2 else 0))
            row = tk.Frame(c, bg=COR_WHITE)
            row.pack(fill="x", padx=14, pady=12)
            tk.Label(row, text=label, bg=COR_WHITE, fg=COR_TEXT_MUTED,
                     font=("Segoe UI", 10)).pack(side="left")
            tk.Label(row, text=valor, bg=COR_WHITE, fg=valor_cor,
                     font=("Segoe UI", 12, "bold")).pack(side="right")

        _kv(0, "Input tokens", f"{b['input_tokens']:,}".replace(",", "."))
        _kv(1, "Output tokens", f"{b['output_tokens']:,}".replace(",", "."))
        limite_cor = COR_DANGER if custo_total >= self.billing_limite_usd else COR_TEXT_DARK
        _kv(2, "Limite mensal", f"US$ {self.billing_limite_usd:.2f}", limite_cor)

        # ===== STATS DA PLANILHA =====
        if not PLANILHA_ADMISSOES.exists():
            tk.Label(body, text="(Planilha admissoes.xlsx ainda não existe.)",
                     bg=COR_BEIGE, fg=COR_TEXT_MUTED).pack(pady=20)
            return

        try:
            from collections import Counter
            from openpyxl import load_workbook
            wb = load_workbook(PLANILHA_ADMISSOES, read_only=True)
            ws = wb.active
            rows = [r for r in list(ws.iter_rows(values_only=True))[1:] if r and any(r)]

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
                empresa = str(empresa or "(sem identificação)").strip() or "(sem identificação)"
                procedencia = str(procedencia or "")
                pl = procedencia.lower()
                if pl.startswith("cadastrado"):
                    cont_status["Cadastrado"] += 1
                elif pl.startswith("dry-run"):
                    cont_status["Dry-run"] += 1
                elif "interno" in pl:
                    cont_status["Pendente interno"] += 1
                elif pl.startswith("pendente"):
                    cont_status["Pendente cliente"] += 1
                else:
                    cont_status["Falha"] += 1
                cont_empresa[empresa] += 1
                if "—" in procedencia and (pl.startswith("pendente") or pl.startswith("falha")):
                    motivo = procedencia.split("—", 1)[1].strip()[:100]
                    cont_motivo[motivo] += 1

            total = sum(cont_status.values())
            ok_count = cont_status.get("Cadastrado", 0) + cont_status.get("Dry-run", 0)
            taxa_sucesso = (ok_count / total * 100) if total else 0

            # ===== STATUS + EMPRESAS (lado a lado) =====
            meio = tk.Frame(body, bg=COR_BEIGE)
            meio.pack(fill="x", pady=(0, 14))
            meio.grid_columnconfigure(0, weight=11)
            meio.grid_columnconfigure(1, weight=9)

            # --- Card Status (com barras) ---
            card_st = tk.Frame(meio, bg=COR_WHITE, bd=1, relief="solid")
            card_st.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

            top_st = tk.Frame(card_st, bg=COR_WHITE)
            top_st.pack(fill="x", padx=18, pady=(16, 12))
            tk.Label(top_st, text="Admissões por status",
                     bg=COR_WHITE, fg=COR_TEXT_DARK,
                     font=("Segoe UI", 12, "bold")).pack(side="left")
            sufx = f"{total} no total · {taxa_sucesso:.1f}% sucesso".replace(".", ",")
            tk.Label(top_st, text=sufx,
                     bg=COR_WHITE, fg=COR_TEXT_MUTED,
                     font=("Segoe UI", 9)).pack(side="right")

            cores_status = {
                "Cadastrado": self._COR_VERDE_OK,
                "Pendente cliente": COR_ORANGE,
                "Pendente interno": self._COR_AMBAR,
                "Falha": COR_DANGER,
                "Dry-run": COR_INFO,
            }
            # Ordem fixa pra exibição consistente
            ordem = ["Cadastrado", "Pendente cliente", "Pendente interno", "Falha", "Dry-run"]
            for label in ordem:
                qtd = cont_status.get(label, 0)
                if qtd == 0 and label not in ("Cadastrado", "Pendente cliente", "Pendente interno", "Falha"):
                    continue
                pct = (qtd / total * 100) if total else 0
                self._stat_barra(card_st, label, qtd, pct, cores_status[label])
            tk.Frame(card_st, bg=COR_WHITE, height=6).pack()

            # --- Card Empresas ---
            card_emp = tk.Frame(meio, bg=COR_WHITE, bd=1, relief="solid")
            card_emp.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
            tk.Label(card_emp, text="Top empresas",
                     bg=COR_WHITE, fg=COR_TEXT_DARK,
                     font=("Segoe UI", 12, "bold")).pack(anchor="w", padx=18, pady=(16, 8))
            top_emp = cont_empresa.most_common(8)
            for i, (nome, qtd) in enumerate(top_emp):
                row = tk.Frame(card_emp, bg=COR_WHITE)
                row.pack(fill="x", padx=18, pady=3)
                cor_nome = COR_TEXT_MUTED if nome.startswith("(") else COR_TEXT_DARK
                cor_qtd = COR_ORANGE if i == 0 else COR_TEXT_DARK
                tk.Label(row, text=nome, bg=COR_WHITE, fg=cor_nome,
                         font=("Segoe UI", 10), anchor="w").pack(side="left", fill="x", expand=True)
                tk.Label(row, text=str(qtd), bg=COR_WHITE, fg=cor_qtd,
                         font=("Segoe UI", 11, "bold")).pack(side="right")
            tk.Frame(card_emp, bg=COR_WHITE, height=10).pack()

            # ===== MOTIVOS =====
            card_mot = tk.Frame(body, bg=COR_WHITE, bd=1, relief="solid")
            card_mot.pack(fill="x")
            tk.Label(card_mot, text="Top motivos de pendência / falha",
                     bg=COR_WHITE, fg=COR_TEXT_DARK,
                     font=("Segoe UI", 12, "bold")).pack(anchor="w", padx=18, pady=(16, 10))
            top_mot = cont_motivo.most_common(10)
            if not top_mot:
                tk.Label(card_mot, text="(nenhuma pendência registrada)",
                         bg=COR_WHITE, fg=COR_TEXT_MUTED,
                         font=("Segoe UI", 10)).pack(anchor="w", padx=18, pady=(0, 16))
            else:
                for qtd, motivo in [(q, m) for m, q in top_mot]:
                    row = tk.Frame(card_mot, bg=COR_WHITE)
                    row.pack(fill="x", padx=18, pady=4)
                    # Badge laranja claro com a quantidade
                    badge = tk.Label(
                        row, text=str(qtd),
                        bg=COR_BEIGE, fg=self._COR_BADGE_TXT,
                        font=("Segoe UI", 10, "bold"),
                        width=3, padx=4, pady=2,
                    )
                    badge.pack(side="left", padx=(0, 10))
                    tk.Label(row, text=motivo,
                             bg=COR_WHITE, fg=COR_TEXT_DARK,
                             font=("Segoe UI", 10), anchor="w",
                             wraplength=900, justify="left").pack(side="left", fill="x", expand=True)
                tk.Frame(card_mot, bg=COR_WHITE, height=10).pack()
        except Exception as e:
            tk.Label(body, text=f"⚠ Erro lendo planilha: {e}",
                     bg=COR_BEIGE, fg=COR_DANGER).pack(pady=20)

    def _stat_barra(self, parent, label, qtd, pct, cor):
        """Linha com label/qtd e barra horizontal (trilha + preenchimento)."""
        wrap = tk.Frame(parent, bg=COR_WHITE)
        wrap.pack(fill="x", padx=18, pady=(0, 10))

        cab = tk.Frame(wrap, bg=COR_WHITE)
        cab.pack(fill="x", pady=(0, 4))
        tk.Label(cab, text=label, bg=COR_WHITE, fg=COR_TEXT_DARK,
                 font=("Segoe UI", 10)).pack(side="left")
        valor_txt = f"{qtd} · {pct:.1f}%".replace(".", ",")
        tk.Label(cab, text=valor_txt, bg=COR_WHITE, fg=COR_TEXT_MUTED,
                 font=("Segoe UI", 10)).pack(side="right")

        # Trilha + preenchimento via place (relwidth = proporção)
        trilha = tk.Frame(wrap, bg=self._COR_TRILHA, height=9)
        trilha.pack(fill="x")
        trilha.pack_propagate(False)
        if pct > 0:
            fill = tk.Frame(trilha, bg=cor)
            fill.place(relx=0, rely=0, relwidth=max(pct / 100, 0.02), relheight=1)

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

    def _atualizar_status_dot(self):
        """Cor do dot da pill: verde rodando, laranja aguardando, cinza parado."""
        if not hasattr(self, "_status_dot"):
            return
        s = (self.status_var.get() or "").lower()
        if "executando" in s or "rodando" in s:
            self._status_dot.configure(fg=COR_SUCCESS)
        elif "aguard" in s or "próxima" in s or "parando" in s:
            self._status_dot.configure(fg=COR_ORANGE)
        else:
            self._status_dot.configure(fg="#888")

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
        """Aplica paleta dark/light varrendo TODO o widget tree (tk) +
        ttk.Style. Mapeia bg/fg por correspondência slot-a-slot entre
        _PALETA_LIGHT e _PALETA_DARK.

        Limitação conhecida: widgets criados DEPOIS do toggle (ex: dialog
        Resolver Pendência aberto depois de ativar dark) começam em light.
        Pra contornar, abrir e fechar dialogs sob o tema escolhido.
        """
        is_dark = bool(self.dark_mode_var.get())
        pal_atual = _PALETA_DARK if is_dark else _PALETA_LIGHT
        pal_outro = _PALETA_LIGHT if is_dark else _PALETA_DARK

        # Constrói mapa: cor_atual_no_outro_palette → cor_no_palette_alvo
        # (mais cores adicionais que aparecem em ambos como "acentos" puros
        # — ex: COR_ORANGE — não entram no map, ficam intactos)
        bg_map: dict[str, str] = {}
        fg_map: dict[str, str] = {}
        for slot, novo in pal_atual.items():
            antigo = pal_outro[slot]
            if antigo == novo:
                continue
            # bg slots
            if slot in ("main_bg", "card_bg", "sidebar_bg", "log_bg", "sub_bg"):
                bg_map[antigo] = novo
            # fg slots
            elif slot in ("text_dark", "text_light", "text_muted"):
                fg_map[antigo] = novo

        # Caso especial: foreground do log_text que era COR_BEIGE
        # (cor "text_light" no contexto do log)
        if is_dark:
            fg_map[COR_BEIGE] = pal_atual["text_light"]
        else:
            fg_map[_PALETA_DARK["text_light"]] = COR_BEIGE

        # Caminha recursivamente, trocando bg/fg
        def remapear(w):
            for attr, mapa in (("bg", bg_map), ("background", bg_map),
                                ("fg", fg_map), ("foreground", fg_map)):
                try:
                    cur = str(w.cget(attr))
                except tk.TclError:
                    continue
                if cur in mapa:
                    try:
                        w.configure({attr: mapa[cur]})
                    except tk.TclError:
                        pass
            for child in w.winfo_children():
                remapear(child)

        remapear(self)

        # ttk styles — reconfigura cores baseadas na paleta atual
        style = ttk.Style()
        p = pal_atual
        style.configure("TFrame", background=p["main_bg"])
        style.configure("TLabel", background=p["main_bg"], foreground=p["text_dark"])
        style.configure("TLabelframe", background=p["main_bg"], foreground=p["text_dark"])
        style.configure("TLabelframe.Label", background=p["main_bg"], foreground=p["text_dark"])
        style.configure("Card.TFrame", background=p["card_bg"])
        style.configure("Card.TLabel", background=p["card_bg"], foreground=p["text_dark"])
        style.configure("TCheckbutton", background=p["main_bg"], foreground=p["text_dark"])
        style.map("TCheckbutton",
                  background=[("active", p["main_bg"])],
                  foreground=[("active", p["text_dark"])])
        style.configure("TButton", background=p["card_bg"], foreground=p["text_dark"])
        style.map("TButton",
                  background=[("active", p["sub_bg"])],
                  foreground=[("active", p["text_dark"])])
        style.configure("Treeview",
                        background=p["card_bg"], fieldbackground=p["card_bg"],
                        foreground=p["text_dark"])
        style.configure("Treeview.Heading",
                        background=p["sidebar_bg"], foreground=COR_WHITE)
        style.configure("TEntry",
                        fieldbackground=p["card_bg"], foreground=p["text_dark"])
        style.configure("TSpinbox",
                        fieldbackground=p["card_bg"], foreground=p["text_dark"])
        style.configure("TCombobox",
                        fieldbackground=p["card_bg"], foreground=p["text_dark"])

        # Atualiza nav buttons do sidebar (cores explícitas — não vêm via ttk)
        if hasattr(self, "_nav_buttons"):
            sidebar_bg = p["sidebar_bg"]
            for k, btn in self._nav_buttons.items():
                # Botão ativo (laranja) mantém. Inativos seguem a paleta.
                if str(btn.cget("bg")) != COR_ORANGE:
                    btn.configure(bg=sidebar_bg, fg=p["text_light"],
                                  activebackground=COR_NAVY_HOVER if not is_dark else "#2A2D34",
                                  activeforeground=COR_WHITE)

        self.gui_q.put(("log", f"Modo {'escuro' if is_dark else 'claro'} ativado"))

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
