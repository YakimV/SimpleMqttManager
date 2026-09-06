import json
import queue
import time
import uuid
from pathlib import Path
from typing import Optional

import customtkinter as ctk
import tkinter as tk
from tkinter import messagebox

try:
    import paho.mqtt.client as mqtt
except ImportError as exc:
    raise SystemExit("Missing dependency: paho-mqtt. Install it with: pip install paho-mqtt") from exc

APP_TITLE = "Simple MQTT Manager"
APP_VERSION = "1.3"
DEFAULT_WORKSPACES = 8

CONFIG_DIR = Path(__file__).parent / "config"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

SETTINGS_FILE = CONFIG_DIR / "settings.json"


# --- Tooltip Class ---
class Tooltip:
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tooltip_window = None
        self.widget.bind("<Enter>", self.show_tooltip)
        self.widget.bind("<Leave>", self.hide_tooltip)

    def show_tooltip(self, event=None):
        if self.tooltip_window or not self.text:
            return
        x = event.x_root + 15 if event else self.widget.winfo_rootx() + 20
        y = event.y_root + 15 if event else self.widget.winfo_rooty() + 20

        self.tooltip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")

        label = tk.Label(
            tw,
            text=self.text,
            justify="left",
            background="#1e293b",
            foreground="white",
            relief="solid",
            borderwidth=1,
            font=("Arial", 11, "normal"),
            padx=8,
            pady=4,
        )
        label.pack()

    def hide_tooltip(self, event=None):
        if self.tooltip_window:
            self.tooltip_window.destroy()
            self.tooltip_window = None


# --- MQTT Workspace Class ---
class MQTTWorkspace(ctk.CTkFrame):
    RECONNECT_DELAY_MS = 5000

    def __init__(self, master, app_instance, workspace_id: int, **kwargs):
        super().__init__(master, **kwargs)
        self.app = app_instance
        self.workspace_id = workspace_id

        # MQTT & State
        self.event_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self.client: Optional[mqtt.Client] = None
        self.connected = False
        self.manual_disconnect = False
        self.reconnect_job = None
        self.subscriptions: set[str] = set()
        self.published_history: list[str] = []
        self.subscribed_history: list[str] = []
        self.quick_buttons: list[dict] = []
        self.all_log_lines: list[str] = []

        # Tracks which inner tabs have had their widgets built (lazy build)
        self._built_tabs: set[str] = set()
        # Pending saved data, applied to a tab's widgets once that tab is built
        self._pending_data: Optional[dict] = None

        # Local variables
        self.name_var = tk.StringVar(value=f"Workspace {workspace_id}")
        self.host_var = tk.StringVar(value="")
        self.port_var = tk.StringVar(value="1883")
        self.username_var = tk.StringVar(value="")
        self.password_var = tk.StringVar(value="")
        self.client_id_var = tk.StringVar(value=f"mqttcc-{uuid.uuid4().hex[:8]}")
        self.keepalive_var = tk.StringVar(value="60")
        self.use_tls_var = tk.BooleanVar(value=False)
        self.auto_reconnect_var = tk.BooleanVar(value=False)
        self.show_password_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Disconnected")
        self.status_detail_var = tk.StringVar(value="Ready")

        self.pub_topic_var = tk.StringVar(value="device/command")
        self.pub_qos_var = tk.StringVar(value="0")
        self.pub_retain_var = tk.BooleanVar(value=False)
        self.sub_topic_var = tk.StringVar(value="device/telemetry")
        self.sub_qos_var = tk.StringVar(value="0")
        self.auto_scroll_var = tk.BooleanVar(value=True)
        self.log_filter_var = tk.StringVar(value="")
        self.separate_logs_var = tk.BooleanVar(value=False)   # <-- НОВА ГАЛОЧКА

        # Color palettes
        self.CARD_BG = ("#ffffff", "#1e293b")
        self.CARD_BORDER = ("#cbd5e1", "#334155")
        self.TEXT_BG = ("#ffffff", "#0f172a")
        self.TITLE_COLOR = ("#334155", "#cbd5e1")
        self.ITEM_BG = ("#f1f5f9", "#334155")

        self.sb_color = ("#cbd5e1", "#475569")
        self.sb_hover = ("#94a3b8", "#64748b")

        self.name_var.trace_add("write", lambda *a: self._on_name_change())
        self.log_filter_var.trace_add("write", lambda *a: self._render_log())

        self._build_ui()
        self.after(200, self._process_event_queue)

    def _on_name_change(self):
        name = self.name_var.get().strip() or f"Workspace {self.workspace_id}"
        self.app.update_workspace_title(self.workspace_id, name)

    def _truncate(self, text: str, max_len: int = 32) -> str:
        if len(text) > max_len:
            return text[: max_len - 3] + "..."
        return text

    def _build_ui(self):
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Top status bar
        self.top_bar = ctk.CTkFrame(self, height=36, corner_radius=0, fg_color=("#e2e8f0", "#1e293b"))
        self.top_bar.grid(row=0, column=0, sticky="ew")

        status_frame = ctk.CTkFrame(self.top_bar, fg_color="transparent")
        status_frame.pack(side="left", padx=10, pady=2)

        self.indicator_lamp = ctk.CTkFrame(status_frame, width=12, height=12, corner_radius=6, fg_color="#ef4444")
        self.indicator_lamp.pack(side="left", padx=(0, 8))

        ctk.CTkLabel(status_frame, textvariable=self.status_var, font=self.app.font_bold, text_color=("#1e40af", "#60a5fa")).pack(side="left")

        initial_theme_text = "☀️ Light" if ctk.get_appearance_mode().lower() == "dark" else "🌙 Dark"
        self.theme_btn = ctk.CTkButton(
            self.top_bar,
            text=initial_theme_text,
            width=85,
            height=26,
            corner_radius=6,
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color=("#cbd5e1", "#334155"),
            hover_color=("#94a3b8", "#475569"),
            text_color=("black", "white"),
            command=self.app.toggle_theme,
        )
        self.theme_btn.pack(side="right", padx=8, pady=3)
        Tooltip(self.theme_btn, "Toggle Light/Dark Theme")

        paned_bg = "#cbd5e1" if ctk.get_appearance_mode().lower() == "light" else "#1e293b"
        self.paned_window = tk.PanedWindow(
            self,
            orient="horizontal",
            bd=0,
            sashwidth=10,
            bg=paned_bg,
            sashcursor="sb_h_double_arrow",
        )
        self.paned_window.grid(row=1, column=0, sticky="nsew", padx=0, pady=0)

        panel_fg = ("#f8fafc", "#0f172a")
        self.left_panel = ctk.CTkFrame(self.paned_window, fg_color=panel_fg, corner_radius=0)
        self.right_panel = ctk.CTkFrame(self.paned_window, fg_color=panel_fg, corner_radius=0)

        self.paned_window.add(self.left_panel, minsize=350, width=420)
        self.paned_window.add(self.right_panel, minsize=300, width=680)

        # NOTE: command= fires whenever the visible inner tab changes, so we can
        # build that tab's widgets lazily instead of building all four up front.
        self.tabs = ctk.CTkTabview(
            self.left_panel,
            corner_radius=0,
            fg_color=("#f8fafc", "#1e293b"),
            command=self._on_inner_tab_changed,
        )
        self.tabs.pack(fill="both", expand=True, padx=2, pady=2)

        try:
            self.tabs._segmented_button.configure(font=self.app.font_normal)
        except AttributeError:
            pass

        # Only create the empty tab containers here - contents are built lazily.
        self.tabs.add("Connection")
        self.tabs.add("Publish")
        self.tabs.add("Subscribe")
        self.tabs.add("Buttons")

        self.logs_container = ctk.CTkFrame(self.right_panel, corner_radius=0, fg_color=panel_fg)
        self.logs_container.pack(fill="both", expand=True, padx=2, pady=2)
        self._build_logs_section()

        self._set_connected_ui(False)

        # Build only the tab that's visible right now (Connection, by default).
        self._ensure_tab_built(self.tabs.get())

    def _on_inner_tab_changed(self):
        self._ensure_tab_built(self.tabs.get())

    def _ensure_tab_built(self, tab_name: str):
        if tab_name in self._built_tabs:
            return
        self._built_tabs.add(tab_name)

        if tab_name == "Connection":
            self._build_connection_tab()
        elif tab_name == "Publish":
            self._build_publish_tab()
            self._refresh_pub_history_ui()
        elif tab_name == "Subscribe":
            self._build_subscribe_tab()
            self._refresh_sub_history_ui()
            self._refresh_subscriptions_ui()
        elif tab_name == "Buttons":
            self._build_buttons_tab()
            self._refresh_quick_buttons_ui()

    def update_theme_button_icon(self, mode: str):
        self.theme_btn.configure(text="☀️ Light" if mode.lower() == "dark" else "🌙 Dark")
        bg_color = "#cbd5e1" if mode.lower() == "light" else "#1e293b"
        if hasattr(self, "paned_window"):
            self.paned_window.configure(bg=bg_color)

    def _build_connection_tab(self):
        tab = self.tabs.tab("Connection")
        tab.grid_columnconfigure(1, weight=1)

        row = 0
        ctk.CTkLabel(tab, text="Name", font=self.app.font_normal, text_color=("black", "white")).grid(row=row, column=0, padx=15, pady=8, sticky="w")
        ctk.CTkEntry(tab, textvariable=self.name_var, font=self.app.font_normal, placeholder_text=f"Workspace {self.workspace_id}").grid(row=row, column=1, padx=15, pady=8, sticky="ew")
        row += 1

        ctk.CTkLabel(tab, text="Host", font=self.app.font_normal, text_color=("black", "white")).grid(row=row, column=0, padx=15, pady=8, sticky="w")
        ctk.CTkEntry(tab, textvariable=self.host_var, font=self.app.font_normal).grid(row=row, column=1, padx=15, pady=8, sticky="ew")
        row += 1

        ctk.CTkLabel(tab, text="Port", font=self.app.font_normal, text_color=("black", "white")).grid(row=row, column=0, padx=15, pady=8, sticky="w")
        ctk.CTkEntry(tab, textvariable=self.port_var, font=self.app.font_normal).grid(row=row, column=1, padx=15, pady=8, sticky="ew")
        row += 1

        ctk.CTkLabel(tab, text="User", font=self.app.font_normal, text_color=("black", "white")).grid(row=row, column=0, padx=15, pady=8, sticky="w")
        ctk.CTkEntry(tab, textvariable=self.username_var, font=self.app.font_normal).grid(row=row, column=1, padx=15, pady=8, sticky="ew")
        row += 1

        ctk.CTkLabel(tab, text="Pass", font=self.app.font_normal, text_color=("black", "white")).grid(row=row, column=0, padx=15, pady=8, sticky="w")
        pass_frame = ctk.CTkFrame(tab, fg_color="transparent")
        pass_frame.grid(row=row, column=1, padx=15, pady=8, sticky="ew")
        pass_frame.grid_columnconfigure(0, weight=1)
        self.password_entry = ctk.CTkEntry(pass_frame, textvariable=self.password_var, font=self.app.font_normal, show="*")
        self.password_entry.grid(row=0, column=0, sticky="ew")

        self.show_pass_btn = ctk.CTkButton(pass_frame, text="👁", width=32, font=self.app.font_normal, command=self._toggle_password_visibility)
        self.show_pass_btn.grid(row=0, column=1, padx=(5, 0))
        row += 1

        ctk.CTkLabel(tab, text="Client ID", font=self.app.font_normal, text_color=("black", "white")).grid(row=row, column=0, padx=15, pady=8, sticky="w")
        ctk.CTkEntry(tab, textvariable=self.client_id_var, font=self.app.font_normal).grid(row=row, column=1, padx=15, pady=8, sticky="ew")
        row += 1

        ctk.CTkLabel(tab, text="Keepalive (s)", font=self.app.font_normal, text_color=("black", "white")).grid(row=row, column=0, padx=15, pady=8, sticky="w")
        ctk.CTkEntry(tab, textvariable=self.keepalive_var, font=self.app.font_normal).grid(row=row, column=1, padx=15, pady=8, sticky="ew")
        row += 1

        opts_frame = ctk.CTkFrame(tab, fg_color="transparent")
        opts_frame.grid(row=row, column=0, columnspan=2, padx=15, pady=4, sticky="ew")
        ctk.CTkCheckBox(opts_frame, text="Use TLS", variable=self.use_tls_var, font=self.app.font_normal, text_color=("black", "white")).pack(side="left", padx=(0, 20))
        ctk.CTkCheckBox(opts_frame, text="Auto-reconnect", variable=self.auto_reconnect_var, font=self.app.font_normal, text_color=("black", "white")).pack(side="left")
        row += 1

        btn_frame = ctk.CTkFrame(tab, fg_color="transparent")
        btn_frame.grid(row=row, column=0, columnspan=2, padx=15, pady=15, sticky="ew")

        self.btn_connect = ctk.CTkButton(btn_frame, text="Connect", height=38, font=self.app.font_bold, command=self.connect)
        self.btn_connect.pack(side="left", fill="x", expand=True, padx=(0, 5))

        self.btn_disconnect = ctk.CTkButton(btn_frame, text="Disconnect", height=38, font=self.app.font_bold, fg_color="#64748b", command=self.disconnect)
        self.btn_disconnect.pack(side="left", fill="x", expand=True, padx=(5, 0))
        row += 1

        detail_frame = ctk.CTkFrame(tab, fg_color=self.CARD_BG, border_width=1, border_color=self.CARD_BORDER, corner_radius=6)
        detail_frame.grid(row=row, column=0, columnspan=2, padx=15, pady=5, sticky="ew")
        ctk.CTkLabel(detail_frame, text="Details:", font=self.app.font_bold, text_color=("#475569", "#94a3b8")).pack(side="left", padx=10, pady=5)
        ctk.CTkLabel(detail_frame, textvariable=self.status_detail_var, font=self.app.font_normal, text_color=("#0f172a", "#f8fafc")).pack(side="left", padx=5, pady=5)

        self._set_connected_ui(self.connected)

    def _toggle_password_visibility(self):
        self.show_password_var.set(not self.show_password_var.get())
        self.password_entry.configure(show="" if self.show_password_var.get() else "*")

    def _build_publish_tab(self):
        tab = self.tabs.tab("Publish")
        tab.grid_columnconfigure(0, weight=1)

        top_frame = ctk.CTkFrame(tab, fg_color="transparent")
        top_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=5)
        ctk.CTkLabel(top_frame, text="Topic", font=self.app.font_normal, text_color=("black", "white")).pack(side="left", padx=(0, 10))
        ctk.CTkEntry(top_frame, textvariable=self.pub_topic_var, font=self.app.font_normal).pack(side="left", fill="x", expand=True)

        opts_frame = ctk.CTkFrame(tab, fg_color="transparent")
        opts_frame.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 5))
        ctk.CTkLabel(opts_frame, text="QoS", font=self.app.font_normal, text_color=("black", "white")).pack(side="left", padx=(0, 5))
        ctk.CTkOptionMenu(opts_frame, values=["0", "1", "2"], variable=self.pub_qos_var, width=60).pack(side="left", padx=(0, 20))
        ctk.CTkCheckBox(opts_frame, text="Retain", variable=self.pub_retain_var, font=self.app.font_normal, text_color=("black", "white")).pack(side="left")

        saved_block = ctk.CTkFrame(tab, fg_color=self.CARD_BG, border_width=1, border_color=self.CARD_BORDER, corner_radius=6)
        saved_block.grid(row=2, column=0, padx=10, pady=5, sticky="ew")
        ctk.CTkLabel(saved_block, text="Recent Topics", font=self.app.font_bold, text_color=self.TITLE_COLOR).pack(anchor="w", padx=10, pady=(5, 0))

        self.pub_history_frame = ctk.CTkScrollableFrame(
            saved_block,
            height=100,
            fg_color="transparent",
            scrollbar_button_color=self.sb_color,
            scrollbar_button_hover_color=self.sb_hover,
        )
        self.pub_history_frame.pack(fill="both", expand=True, padx=5, pady=5)

        payload_header = ctk.CTkFrame(tab, fg_color="transparent")
        payload_header.grid(row=3, column=0, padx=10, pady=(5, 2), sticky="ew")
        ctk.CTkLabel(payload_header, text="Payload", font=self.app.font_bold, text_color=("black", "white")).pack(side="left")
        ctk.CTkButton(payload_header, text="Format JSON", width=100, height=24, font=ctk.CTkFont(size=12), command=self.format_payload_json).pack(side="right")

        self.payload_text = ctk.CTkTextbox(
            tab,
            font=self.app.font_normal,
            border_width=1,
            border_color=self.CARD_BORDER,
            fg_color=self.TEXT_BG,
            corner_radius=6,
            scrollbar_button_color=self.sb_color,
            scrollbar_button_hover_color=self.sb_hover,
        )
        self.payload_text.grid(row=4, column=0, padx=10, pady=(0, 5), sticky="nsew")
        tab.grid_rowconfigure(4, weight=1)

        ctk.CTkButton(tab, text="Publish", height=38, font=self.app.font_bold, command=self.publish_message).grid(row=5, column=0, padx=10, pady=10, sticky="ew")

    def format_payload_json(self):
        raw = self.payload_text.get("1.0", "end").strip()
        if not raw:
            return
        try:
            parsed = json.loads(raw)
            pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
            self.payload_text.delete("1.0", "end")
            self.payload_text.insert("1.0", pretty)
        except json.JSONDecodeError as e:
            messagebox.showerror("Invalid JSON", f"Could not parse payload as JSON:\n{e}")

    def _refresh_pub_history_ui(self):
        if not hasattr(self, "pub_history_frame"):
            return
        for w in self.pub_history_frame.winfo_children():
            w.destroy()
        for t in self.published_history:
            tag_frame = ctk.CTkFrame(self.pub_history_frame, fg_color=self.ITEM_BG, corner_radius=4)
            tag_frame.pack(fill="x", pady=2)

            del_btn = ctk.CTkButton(
                tag_frame,
                text="X",
                width=28,
                height=28,
                font=ctk.CTkFont(size=12, weight="bold"),
                fg_color="transparent",
                text_color=("#b91c1c", "#f87171"),
                hover_color=("#f87171", "#991b1b"),
                command=lambda t=t: self.remove_pub_history(t),
            )
            del_btn.pack(side="right", padx=(0, 2))

            btn = ctk.CTkButton(
                tag_frame,
                text=self._truncate(t, 35),
                height=28,
                font=ctk.CTkFont(size=12),
                fg_color="transparent",
                text_color=("black", "white"),
                hover_color=("#cbd5e1", "#475569"),
                anchor="w",
                command=lambda t=t: self.pub_topic_var.set(t),
            )
            btn.pack(side="left", fill="x", expand=True, padx=(2, 0))
            Tooltip(btn, text=t)

    def remove_pub_history(self, topic):
        if topic in self.published_history:
            self.published_history.remove(topic)
            self.app.save_settings()
            self._refresh_pub_history_ui()

    def _build_subscribe_tab(self):
        tab = self.tabs.tab("Subscribe")
        tab.grid_columnconfigure(0, weight=1)

        top_frame = ctk.CTkFrame(tab, fg_color="transparent")
        top_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=10)
        ctk.CTkEntry(top_frame, textvariable=self.sub_topic_var, font=self.app.font_normal, placeholder_text="Enter topic...").pack(side="left", fill="x", expand=True, padx=(0, 10))
        ctk.CTkOptionMenu(top_frame, values=["0", "1", "2"], variable=self.sub_qos_var, width=55).pack(side="left", padx=(0, 10))
        ctk.CTkButton(top_frame, text="Subscribe", width=90, font=self.app.font_normal, command=self.add_subscription).pack(side="right")

        saved_block = ctk.CTkFrame(tab, fg_color=self.CARD_BG, border_width=1, border_color=self.CARD_BORDER, corner_radius=6)
        saved_block.grid(row=1, column=0, padx=10, pady=5, sticky="ew")
        ctk.CTkLabel(saved_block, text="Recent Topics", font=self.app.font_bold, text_color=self.TITLE_COLOR).pack(anchor="w", padx=10, pady=(5, 0))

        self.sub_history_frame = ctk.CTkScrollableFrame(
            saved_block,
            height=100,
            fg_color="transparent",
            scrollbar_button_color=self.sb_color,
            scrollbar_button_hover_color=self.sb_hover,
        )
        self.sub_history_frame.pack(fill="both", expand=True, padx=5, pady=5)

        tab.grid_rowconfigure(2, weight=1)

        active_block = ctk.CTkFrame(tab, fg_color=self.CARD_BG, border_width=1, border_color=self.CARD_BORDER, corner_radius=6)
        active_block.grid(row=2, column=0, padx=10, pady=5, sticky="nsew")

        header_row = ctk.CTkFrame(active_block, fg_color="transparent")
        header_row.pack(fill="x", padx=10, pady=(5, 0))
        ctk.CTkLabel(header_row, text="Active Subscriptions", font=self.app.font_bold, text_color=self.TITLE_COLOR).pack(side="left")
        ctk.CTkButton(header_row, text="Unsub All", width=80, height=22, font=ctk.CTkFont(size=11), fg_color="#64748b", hover_color="#475569", command=self.unsubscribe_all).pack(side="right")

        self.subscriptions_list = ctk.CTkScrollableFrame(
            active_block,
            fg_color="transparent",
            scrollbar_button_color=self.sb_color,
            scrollbar_button_hover_color=self.sb_hover,
        )
        self.subscriptions_list.pack(fill="both", expand=True, padx=5, pady=5)

    def _refresh_sub_history_ui(self):
        if not hasattr(self, "sub_history_frame"):
            return
        for w in self.sub_history_frame.winfo_children():
            w.destroy()
        for t in self.subscribed_history:
            tag_frame = ctk.CTkFrame(self.sub_history_frame, fg_color=self.ITEM_BG, corner_radius=4)
            tag_frame.pack(fill="x", pady=2)

            del_btn = ctk.CTkButton(
                tag_frame,
                text="X",
                width=28,
                height=28,
                font=ctk.CTkFont(size=12, weight="bold"),
                fg_color="transparent",
                text_color=("#b91c1c", "#f87171"),
                hover_color=("#f87171", "#991b1b"),
                command=lambda t=t: self.remove_sub_history(t),
            )
            del_btn.pack(side="right", padx=(0, 2))

            btn = ctk.CTkButton(
                tag_frame,
                text=self._truncate(t, 35),
                height=28,
                font=ctk.CTkFont(size=12),
                fg_color="transparent",
                text_color=("black", "white"),
                hover_color=("#cbd5e1", "#475569"),
                anchor="w",
                command=lambda t=t: self.sub_topic_var.set(t),
            )
            btn.pack(side="left", fill="x", expand=True, padx=(2, 0))
            Tooltip(btn, text=t)

    def remove_sub_history(self, topic):
        if topic in self.subscribed_history:
            self.subscribed_history.remove(topic)
            self.app.save_settings()
            self._refresh_sub_history_ui()

    def _refresh_subscriptions_ui(self):
        if not hasattr(self, "subscriptions_list"):
            return
        for w in self.subscriptions_list.winfo_children():
            w.destroy()
        for t in sorted(self.subscriptions):
            f = ctk.CTkFrame(self.subscriptions_list, fg_color=self.ITEM_BG, corner_radius=4)
            f.pack(fill="x", pady=2)

            del_btn = ctk.CTkButton(
                f,
                text="X",
                width=28,
                height=28,
                font=ctk.CTkFont(size=12, weight="bold"),
                fg_color="transparent",
                text_color=("#b91c1c", "#f87171"),
                hover_color=("#f87171", "#991b1b"),
                command=lambda t=t: self.unsubscribe(t),
            )
            del_btn.pack(side="right", padx=(0, 2))

            lbl = ctk.CTkLabel(f, text=self._truncate(t, 35), font=self.app.font_normal, text_color=("black", "white"), anchor="w")
            lbl.pack(side="left", fill="x", expand=True, padx=10)
            Tooltip(lbl, text=t)

    def _build_buttons_tab(self):
        tab = self.tabs.tab("Buttons")
        tab.grid_columnconfigure(0, weight=1)

        input_frame = ctk.CTkFrame(tab, fg_color="transparent")
        input_frame.grid(row=0, column=0, padx=10, pady=5, sticky="ew")
        input_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(input_frame, text="Name", font=self.app.font_normal, text_color=("black", "white")).grid(row=0, column=0, padx=(0, 10), pady=4, sticky="w")
        self.macro_name_var = tk.StringVar()
        ctk.CTkEntry(input_frame, textvariable=self.macro_name_var, font=self.app.font_normal).grid(row=0, column=1, pady=4, sticky="ew")

        ctk.CTkLabel(input_frame, text="Topic", font=self.app.font_normal, text_color=("black", "white")).grid(row=1, column=0, padx=(0, 10), pady=4, sticky="w")
        self.macro_topic_var = tk.StringVar()
        ctk.CTkEntry(input_frame, textvariable=self.macro_topic_var, font=self.app.font_normal).grid(row=1, column=1, pady=4, sticky="ew")

        ctk.CTkLabel(tab, text="Payload", font=self.app.font_bold, text_color=("black", "white")).grid(row=1, column=0, padx=10, pady=(5, 0), sticky="w")

        self.macro_payload_text = ctk.CTkTextbox(
            tab,
            font=self.app.font_log,
            height=60,
            border_width=1,
            border_color=self.CARD_BORDER,
            fg_color=self.TEXT_BG,
            corner_radius=6,
            scrollbar_button_color=self.sb_color,
            scrollbar_button_hover_color=self.sb_hover,
        )
        self.macro_payload_text.grid(row=2, column=0, padx=10, pady=(0, 5), sticky="ew")

        ctk.CTkButton(tab, text="Save Quick Command", height=38, font=self.app.font_bold, command=self.add_quick_button).grid(row=3, column=0, padx=10, pady=10, sticky="ew")

        dash_block = ctk.CTkFrame(tab, fg_color=self.CARD_BG, border_width=1, border_color=self.CARD_BORDER, corner_radius=6)
        dash_block.grid(row=4, column=0, padx=10, pady=5, sticky="nsew")
        tab.grid_rowconfigure(4, weight=1)

        ctk.CTkLabel(dash_block, text="Command Dashboard", font=self.app.font_bold, text_color=self.TITLE_COLOR).pack(anchor="w", padx=10, pady=(5, 0))

        self.macro_scroll_zone = ctk.CTkScrollableFrame(
            dash_block,
            fg_color="transparent",
            scrollbar_button_color=self.sb_color,
            scrollbar_button_hover_color=self.sb_hover,
        )
        self.macro_scroll_zone.pack(fill="both", expand=True, padx=5, pady=5)

    def add_quick_button(self):
        name = self.macro_name_var.get().strip()
        topic = self.macro_topic_var.get().strip()
        payload = self.macro_payload_text.get("1.0", "end").strip()

        if not name or not topic:
            messagebox.showerror("Validation Error", "Name and Topic are required fields.")
            return

        self.quick_buttons.append({"label": name, "topic": topic, "payload": payload})
        self.macro_name_var.set("")
        self.macro_payload_text.delete("1.0", "end")

        self.app.save_settings()
        self._refresh_quick_buttons_ui()

    def delete_quick_button(self, index):
        if 0 <= index < len(self.quick_buttons):
            label = self.quick_buttons[index].get("label", "this command")
            if not messagebox.askyesno("Confirm Delete", f"Delete quick command '{label}'?"):
                return
            self.quick_buttons.pop(index)
            self.app.save_settings()
            self._refresh_quick_buttons_ui()

    def execute_quick_button(self, item):
        if not self.connected or not self.client:
            self.log(f"Action blocked: MQTT Client Disconnected. Could not send: {item['label']}")
            return

        topic = item.get("topic", "").strip()
        payload = item.get("payload", "").strip()
        if not topic:
            return

        try:
            qos = int(self.pub_qos_var.get())
        except ValueError:
            qos = 0

        result = self.client.publish(topic, payload, qos=qos, retain=self.pub_retain_var.get())
        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            self.log(f"QUICK PUB [{item['label']}]: {topic} -> {self._truncate(payload, 40)}")
        else:
            self.log(f"QUICK PUB Failed for [{item['label']}], code={result.rc}")

    def _refresh_quick_buttons_ui(self):
        if not hasattr(self, "macro_scroll_zone"):
            return
        for w in self.macro_scroll_zone.winfo_children():
            w.destroy()
        for idx, item in enumerate(self.quick_buttons):
            f = ctk.CTkFrame(self.macro_scroll_zone, fg_color="transparent")
            f.pack(fill="x", pady=4)

            action_btn = ctk.CTkButton(
                f,
                text=item["label"],
                height=45,
                corner_radius=6,
                font=ctk.CTkFont(size=15, weight="bold"),
                anchor="center",
                command=lambda i=item: self.execute_quick_button(i),
            )
            action_btn.pack(side="left", fill="x", expand=True, padx=(0, 5))

            del_btn = ctk.CTkButton(
                f,
                text="X",
                width=45,
                height=45,
                corner_radius=6,
                font=ctk.CTkFont(size=15, weight="bold"),
                fg_color="#ef4444",
                hover_color="#dc2626",
                text_color="white",
                command=lambda index=idx: self.delete_quick_button(index),
            )
            del_btn.pack(side="right")

            Tooltip(action_btn, text=f"Topic: {item['topic']}\nPayload: {item['payload']}")


    def _build_logs_section(self):
        self.logs_container.grid_rowconfigure(1, weight=1)
        self.logs_container.grid_columnconfigure(0, weight=1)

        top = ctk.CTkFrame(self.logs_container, fg_color=("#e2e8f0", "#1e293b"), corner_radius=0)
        top.grid(row=0, column=0, sticky="ew")

        ctk.CTkLabel(top, text="Logs", font=self.app.font_bold, text_color=("black", "white")).pack(side="left", padx=12, pady=5)

        # Auto-scroll
        self.scroll_check = ctk.CTkCheckBox(
            top,
            text="Auto-scroll",
            variable=self.auto_scroll_var,
            font=ctk.CTkFont(size=12),
            text_color=("black", "white"),
            checkbox_width=16,
            checkbox_height=16,
        )
        self.scroll_check.pack(side="left", padx=8, pady=5)

        # НОВА ГАЛОЧКА: розділяти рядком
        self.separate_check = ctk.CTkCheckBox(
            top,
            text="Separate by line",
            variable=self.separate_logs_var,
            font=ctk.CTkFont(size=12),
            text_color=("black", "white"),
            checkbox_width=16,
            checkbox_height=16,
        )
        self.separate_check.pack(side="left", padx=8, pady=5)
        Tooltip(self.separate_check, "Insert a blank line between different entries in the log")

        # Фільтр
        filter_box = ctk.CTkFrame(top, fg_color="transparent")
        filter_box.pack(side="left", padx=10, pady=5)
        ctk.CTkLabel(filter_box, text="Filter:", font=self.app.font_bold, text_color=("black", "white")).pack(side="left", padx=(0, 5))
        ctk.CTkEntry(filter_box, textvariable=self.log_filter_var, placeholder_text="Search...", width=140, font=ctk.CTkFont(size=12)).pack(side="left")

        ctk.CTkButton(top, text="Clear", width=70, height=26, font=ctk.CTkFont(size=12), fg_color="#64748b", command=self.clear_log).pack(side="right", padx=12, pady=5)

        self.log_text = ctk.CTkTextbox(
            self.logs_container,
            font=self.app.font_log,
            fg_color=self.TEXT_BG,
            text_color=("#000000", "#f8fafc"),
            border_width=1,
            border_color=self.CARD_BORDER,
            corner_radius=6,
            scrollbar_button_color=self.sb_color,
            scrollbar_button_hover_color=self.sb_hover,
        )
        self.log_text.grid(row=1, column=0, sticky="nsew", padx=5, pady=5)
        self.log_text.configure(state="disabled")

    def log(self, msg: str):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"

      
        if self.separate_logs_var.get() and self.all_log_lines:
            last_line = self.all_log_lines[-1]
            if last_line.strip() != "":
                self.all_log_lines.append("")
                self.log_text.configure(state="normal")
                self.log_text.insert("end", "\n")
                if self.auto_scroll_var.get():
                    self.log_text.see("end")
                self.log_text.configure(state="disabled")

        self.all_log_lines.append(line)
        if len(self.all_log_lines) > 5000:
            self.all_log_lines.pop(0)

        filt = self.log_filter_var.get().strip().lower()
        if not filt or filt in line.lower():
            self.log_text.configure(state="normal")
            self.log_text.insert("end", line + "\n")
            if self.auto_scroll_var.get():
                self.log_text.see("end")
            self.log_text.configure(state="disabled")

    def _render_log(self):
        filt = self.log_filter_var.get().strip().lower()
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        for line in self.all_log_lines:
            if not filt or filt in line.lower():
                self.log_text.insert("end", line + "\n")
        if self.auto_scroll_var.get():
            self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def clear_log(self):
        self.all_log_lines.clear()
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    # ------------------------------------------------------------
    #  MQTT Client Logic
    # ------------------------------------------------------------
    def load_workspace_data(self, data: dict):
        saved_name = data.get("name", f"Workspace {self.workspace_id}")
        self.name_var.set(saved_name)
        self.app.update_workspace_title(self.workspace_id, saved_name)

        self.host_var.set(data.get("host", ""))
        self.port_var.set(str(data.get("port", "1883")))
        self.username_var.set(data.get("username", ""))
        self.password_var.set(data.get("password", ""))
        self.client_id_var.set(data.get("client_id", self.client_id_var.get()))
        self.keepalive_var.set(str(data.get("keepalive", "60")))
        self.use_tls_var.set(data.get("use_tls", False))
        self.auto_reconnect_var.set(data.get("auto_reconnect", False))
        self.subscriptions = set(data.get("subscriptions", []))
        self.published_history = data.get("published_history", [])
        self.subscribed_history = data.get("subscribed_history", [])
        self.auto_scroll_var.set(data.get("auto_scroll", True))
        self.quick_buttons = data.get("quick_buttons", [])
        # Завантажуємо стан галочки "Розділяти рядком"
        self.separate_logs_var.set(data.get("separate_logs", False))

        left_width = data.get("left_panel_width")
        if left_width and isinstance(left_width, int) and left_width > 10:
            try:
                self.paned_window.paneconfigure(self.left_panel, width=left_width)
            except Exception:
                pass

        self._refresh_subscriptions_ui()
        self._refresh_pub_history_ui()
        self._refresh_sub_history_ui()
        self._refresh_quick_buttons_ui()

    def get_workspace_data(self) -> dict:
        return {
            "name": self.name_var.get().strip() or f"Workspace {self.workspace_id}",
            "host": self.host_var.get(),
            "port": self.port_var.get(),
            "username": self.username_var.get(),
            "password": self.password_var.get(),
            "client_id": self.client_id_var.get(),
            "keepalive": self.keepalive_var.get(),
            "use_tls": self.use_tls_var.get(),
            "auto_reconnect": self.auto_reconnect_var.get(),
            "subscriptions": list(self.subscriptions),
            "published_history": self.published_history,
            "subscribed_history": self.subscribed_history,
            "auto_scroll": self.auto_scroll_var.get(),
            "quick_buttons": self.quick_buttons,
            "left_panel_width": self.left_panel.winfo_width(),
            "separate_logs": self.separate_logs_var.get(), 
        }

    def connect(self):
        if self.connected:
            return
        self._cancel_reconnect()
        self.manual_disconnect = False

        host = self.host_var.get().strip()
        if not host:
            messagebox.showerror("Error", "Host field cannot be empty.")
            return
        try:
            port = int(self.port_var.get().strip())
            keepalive = int(self.keepalive_var.get().strip() or "60")
        except ValueError:
            messagebox.showerror("Error", "Port and keepalive must be valid numbers")
            return

        client_id = self.client_id_var.get().strip() or f"mqttcc-{uuid.uuid4().hex[:8]}"

        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=mqtt.MQTTv311,
        )

        username = self.username_var.get().strip()
        if username:
            self.client.username_pw_set(username, self.password_var.get())

        if self.use_tls_var.get():
            self.client.tls_set()

        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_message = self.on_message

        try:
            self.status_var.set("Connecting...")
            self.status_detail_var.set(f"Connecting to {host}:{port}...")
            self._set_connected_ui(True, pending=True)
            self.client.connect(host, port, keepalive)
            self.client.loop_start()
            self.app.save_settings()
        except Exception as e:
            self._set_connected_ui(False)
            self.status_var.set("Disconnected")
            self.status_detail_var.set(f"Failed: {str(e)}")
            messagebox.showerror("Connection Error", str(e))
            self._maybe_schedule_reconnect()

    def disconnect(self):
        self.manual_disconnect = True
        self._cancel_reconnect()
        if self.client:
            self.client.disconnect()
            self.client.loop_stop()

    def _cancel_reconnect(self):
        if self.reconnect_job is not None:
            try:
                self.after_cancel(self.reconnect_job)
            except Exception:
                pass
            self.reconnect_job = None

    def _maybe_schedule_reconnect(self):
        if self.manual_disconnect or not self.auto_reconnect_var.get():
            return
        self._cancel_reconnect()
        self.event_queue.put(("log", f"Auto-reconnect scheduled in {self.RECONNECT_DELAY_MS // 1000}s..."))
        self.reconnect_job = self.after(self.RECONNECT_DELAY_MS, self.connect)

    def on_connect(self, client, userdata, flags, reason_code, properties):
        self.event_queue.put(("connected", str(reason_code)))
        if reason_code == 0:
            for topic in sorted(self.subscriptions):
                client.subscribe(topic)
                self.event_queue.put(("log", f"Auto-subscribed to: {topic}"))
        else:
            self.event_queue.put(("log", f"Connect failed with code: {reason_code}"))

    def on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties):
        self.event_queue.put(("disconnected", str(reason_code)))

    def on_message(self, client, userdata, msg):
        payload = msg.payload.decode(errors="ignore")
        self.event_queue.put(("log", f"MSG {msg.topic}: {payload}"))

    def publish_message(self):
        if not self.connected or not self.client:
            return
        topic = self.pub_topic_var.get().strip()
        if not topic:
            return

        try:
            qos = int(self.pub_qos_var.get())
        except ValueError:
            qos = 0

        payload = self.payload_text.get("1.0", "end").strip()
        result = self.client.publish(topic, payload, qos=qos, retain=self.pub_retain_var.get())

        if result.rc == mqtt.MQTT_ERR_SUCCESS:
            if topic in self.published_history:
                self.published_history.remove(topic)
            self.published_history.insert(0, topic)
            if len(self.published_history) > 30:
                self.published_history.pop()

            self.app.save_settings()
            self._refresh_pub_history_ui()
            self.log(f"PUB: {topic} -> {self._truncate(payload, 20)}")
        else:
            self.log(f"PUB failed: {topic}, rc={result.rc}")

    def add_subscription(self):
        topic = self.sub_topic_var.get().strip()
        if not topic:
            return

        try:
            qos = int(self.sub_qos_var.get())
        except ValueError:
            qos = 0

        if self.connected and self.client:
            self.client.subscribe(topic, qos=qos)
            self.log(f"SUB: {topic} (QoS {qos})")

        self.subscriptions.add(topic)
        if topic in self.subscribed_history:
            self.subscribed_history.remove(topic)
        self.subscribed_history.insert(0, topic)
        if len(self.subscribed_history) > 30:
            self.subscribed_history.pop()

        self.app.save_settings()
        self._refresh_sub_history_ui()
        self._refresh_subscriptions_ui()

    def unsubscribe(self, topic):
        self.subscriptions.discard(topic)
        if self.connected and self.client:
            self.client.unsubscribe(topic)
            self.log(f"UNSUB: {topic}")
        self.app.save_settings()
        self._refresh_subscriptions_ui()

    def unsubscribe_all(self):
        if not self.subscriptions:
            return
        if not messagebox.askyesno("Confirm", "Unsubscribe from all topics?"):
            return
        for topic in list(self.subscriptions):
            self.unsubscribe(topic)

    def _process_event_queue(self):
        processed_count = 0
        while processed_count < 30:
            try:
                ev, data = self.event_queue.get_nowait()
                processed_count += 1
                if ev == "log":
                    self.log(str(data))
                elif ev == "connected":
                    self.connected = True
                    self.status_var.set("Connected")
                    self.status_detail_var.set("Success")
                    self._set_connected_ui(True)
                    self.log(f"Connected to broker. Status code: {data}")
                elif ev == "disconnected":
                    self.connected = False
                    self.status_var.set("Disconnected")
                    self.status_detail_var.set(f"Code {data}")
                    self._set_connected_ui(False)
                    self.log(f"Disconnected from broker (code {data})")
                    self._maybe_schedule_reconnect()
            except queue.Empty:
                break

        next_delay = 80 if (self.connected or not self.event_queue.empty()) else 300
        self.after(next_delay, self._process_event_queue)

    def _set_connected_ui(self, connected, pending=False):
        if not hasattr(self, "btn_connect"):
            return
        self.btn_connect.configure(state="disabled" if (connected or pending) else "normal")
        self.btn_disconnect.configure(state="normal" if (connected or pending) else "disabled")
        if connected:
            self.indicator_lamp.configure(fg_color="#22c55e")
        elif pending:
            self.indicator_lamp.configure(fg_color="#eab308")
        else:
            self.indicator_lamp.configure(fg_color="#ef4444")


# --- Main Application Window ---
class MQTTControlCenter(ctk.CTk):
    def __init__(self):
        super().__init__()
        # Hide window while building heavy widget tree to eliminate visual stutter and speed up load
        self.withdraw()

        self.appearance_mode = "light"
        ctk.set_appearance_mode(self.appearance_mode)
        ctk.set_default_color_theme("blue")

        self.font_normal = ctk.CTkFont(size=14)
        self.font_bold = ctk.CTkFont(size=14, weight="bold")
        self.font_header = ctk.CTkFont(size=13, weight="bold")
        self.font_log = ctk.CTkFont(family="Consolas", size=13)

        self.title(f"{APP_TITLE} v{APP_VERSION}")
        self.geometry("1260x760")
        self.minsize(1050, 620)

        self.protocol("WM_DELETE_WINDOW", self.on_close)

        # command= fires on tab switch so we can lazily build the workspace
        # that just became visible, instead of building all of them upfront.
        self.workspace_tabs = ctk.CTkTabview(self, corner_radius=4, command=self._on_workspace_tab_changed)
        self.workspace_tabs.pack(fill="both", expand=True, padx=5, pady=5)

        # workspaces[i] is None until that workspace's tab has actually been opened.
        self.workspaces: list[Optional[MQTTWorkspace]] = []
        self._workspace_tab_objs: dict[int, "ctk.CTkFrame"] = {}
        self._saved_workspace_data: list[dict] = []

        # Load raw saved data first (cheap - just JSON parsing) so we know how
        # many workspaces existed and can hand data to each workspace as it's built.
        num_workspaces = DEFAULT_WORKSPACES
        if SETTINGS_FILE.exists():
            try:
                root_data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                self._saved_workspace_data = root_data.get("workspaces", [])
                num_workspaces = max(DEFAULT_WORKSPACES, len(self._saved_workspace_data))

                saved_theme = root_data.get("appearance_mode", "light")
                if saved_theme in ["light", "dark"]:
                    self.appearance_mode = saved_theme
                    ctk.set_appearance_mode(self.appearance_mode)
            except Exception:
                pass

        # Only create empty tab containers here - actual workspace widgets are
        # built lazily (see _build_workspace / _on_workspace_tab_changed).
        for i in range(1, num_workspaces + 1):
            tab_title = f"Workspace {i}"
            tab_obj = self.workspace_tabs.add(tab_title)
            self._workspace_tab_objs[i] = tab_obj
            self.workspaces.append(None)

        # Stretch tabs evenly across available top width
        try:
            sb = self.workspace_tabs._segmented_button
            sb.configure(font=self.font_header, height=36)
            sb.grid(sticky="ew", padx=8, pady=(4, 0))
            for col in range(num_workspaces):
                sb.grid_columnconfigure(col, weight=1)
        except AttributeError:
            pass

        # Apply saved custom names to every tab label right away - this is just
        # cheap text on the tab button, so it's fine to do for all workspaces
        # even before their full UI is built.
        for i in range(1, num_workspaces + 1):
            idx = i - 1
            if idx < len(self._saved_workspace_data):
                saved_name = self._saved_workspace_data[idx].get("name")
                if saved_name:
                    self.update_workspace_title(i, saved_name)

        # Only build the workspace that's actually visible at startup.
        self._build_workspace(1)

        # Render layout in memory, then display window instantly
        self.update_idletasks()
        self.deiconify()

    def _on_workspace_tab_changed(self):
        name = self.workspace_tabs.get()
        try:
            workspace_id = int(name.rsplit(" ", 1)[-1])
        except (ValueError, IndexError):
            return
        if 1 <= workspace_id <= len(self.workspaces) and self.workspaces[workspace_id - 1] is None:
            self._build_workspace(workspace_id)

    def _build_workspace(self, workspace_id: int):
        idx = workspace_id - 1
        tab_obj = self._workspace_tab_objs[workspace_id]
        ws = MQTTWorkspace(tab_obj, app_instance=self, workspace_id=workspace_id)
        ws.pack(fill="both", expand=True)
        self.workspaces[idx] = ws

        if idx < len(self._saved_workspace_data):
            ws.load_workspace_data(self._saved_workspace_data[idx])

        # Theme may have been switched to "dark" before this workspace existed.
        ws.update_theme_button_icon(self.appearance_mode)
        return ws

    def update_workspace_title(self, workspace_id: int, title: str):
        tab_key = f"Workspace {workspace_id}"
        display_title = title.strip() or tab_key
        if len(display_title) > 16:
            display_title = display_title[:13] + "..."
        try:
            btn = self.workspace_tabs._segmented_button._buttons_dict.get(tab_key)
            if btn:
                btn.configure(text=display_title)
        except Exception:
            pass

    def toggle_theme(self):
        self.appearance_mode = "dark" if self.appearance_mode == "light" else "light"
        ctk.set_appearance_mode(self.appearance_mode)

        for ws in self.workspaces:
            if ws is not None:
                ws.update_theme_button_icon(self.appearance_mode)

        self.save_settings()

    def save_settings(self):
        workspaces_data = []
        for idx, ws in enumerate(self.workspaces):
            if ws is not None:
                workspaces_data.append(ws.get_workspace_data())
            elif idx < len(self._saved_workspace_data):
                # Never-opened workspace: keep whatever was already on disk.
                workspaces_data.append(self._saved_workspace_data[idx])
            else:
                workspaces_data.append({})

        root_data = {
            "appearance_mode": self.appearance_mode,
            "workspaces": workspaces_data,
        }
        try:
            SETTINGS_FILE.write_text(json.dumps(root_data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def on_close(self):
        self.save_settings()
        for ws in self.workspaces:
            if ws is None:
                continue
            ws.manual_disconnect = True
            ws._cancel_reconnect()
            if ws.client:
                try:
                    ws.client.disconnect()
                    ws.client.loop_stop()
                except Exception:
                    pass
        self.destroy()


if __name__ == "__main__":
    app = MQTTControlCenter()
    app.mainloop()