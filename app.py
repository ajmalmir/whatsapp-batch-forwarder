"""WhatsApp Batch Forwarder - modern Windows GUI (CustomTkinter)."""
import json
import os
import queue
import sys
import threading
import time
from tkinter import messagebox

import customtkinter as ctk

from tkinter import filedialog

from extractor import Extractor, NumberStore
from forwarder import Forwarder, find_adb

BASE = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE, "settings.json")
LOG_PATH = os.path.join(BASE, "sent.txt")
NUMBERS_PATH = os.path.join(BASE, "numbers.csv")

DEFAULTS = {
    "adb": find_adb(),
    "device": "",
    "source_chat": "",
    "anchor": "",
    "batch_size": 5,
    "delay": 5,
    "skip_groups": True,
    "app_package": "com.whatsapp.w4b",
    "exclude": [],
    "theme": "Dark",
    "export_group": "",
}

# palette: (light, dark)
GREEN = ("#1DAA61", "#25D366")
GREEN_HOVER = ("#178A4F", "#1EB857")
RED = ("#D93F3F", "#F05252")
RED_HOVER = ("#B83434", "#D94545")
BG = ("#F3F5F7", "#0B141A")
CARD = ("#FFFFFF", "#111B21")
FIELD = ("#F0F2F5", "#202C33")
BORDER = ("#E2E6EA", "#222E35")
TEXT = ("#111B21", "#E9EDEF")
MUTED = ("#667781", "#8696A0")
AMBER = ("#C58A00", "#F5B83D")


def load_cfg():
    cfg = dict(DEFAULTS)
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return cfg


def count_sent():
    try:
        with open(LOG_PATH, encoding="utf-8-sig") as f:
            return sum(1 for l in f if l.strip())
    except FileNotFoundError:
        return 0


class Card(ctk.CTkFrame):
    def __init__(self, master, title=None, **kw):
        super().__init__(master, fg_color=CARD, corner_radius=16, border_width=1, border_color=BORDER, **kw)
        if title:
            ctk.CTkLabel(self, text=title.upper(), text_color=MUTED,
                         font=ctk.CTkFont(size=11, weight="bold")).pack(anchor="w", padx=20, pady=(16, 4))


class Stat(ctk.CTkFrame):
    def __init__(self, master, label, color=TEXT):
        super().__init__(master, fg_color=CARD, corner_radius=16, border_width=1, border_color=BORDER)
        ctk.CTkLabel(self, text=label, text_color=MUTED, font=ctk.CTkFont(size=12)).pack(anchor="w", padx=18, pady=(14, 0))
        self.value = ctk.CTkLabel(self, text="0", text_color=color, font=ctk.CTkFont(size=30, weight="bold"))
        self.value.pack(anchor="w", padx=18, pady=(0, 12))

    def set(self, v):
        self.value.configure(text=str(v))


class App(ctk.CTk):
    def __init__(self):
        self.cfg = load_cfg()
        ctk.set_appearance_mode(self.cfg.get("theme", "Dark"))
        ctk.set_default_color_theme("green")
        super().__init__(fg_color=BG)
        self.title("WhatsApp Batch Forwarder")
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(1100, sw - 40), min(720, sh - 90)
        self.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h - 40) // 2)}")
        self.minsize(900, 560)
        self.q = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.session_sent = 0
        self.mode = "forward"
        self.ext_new = 0
        self.batches = 0
        self.build()
        self.refresh_stats()
        self.after(200, self.poll)
        self.after(400, self.on_connect)

    # ================= layout =================
    def build(self):
        self.grid_columnconfigure(0, weight=0, minsize=400)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self.header()
        self.settings_panel()
        self.dashboard_panel()

    def header(self):
        h = ctk.CTkFrame(self, fg_color="transparent")
        h.grid(row=0, column=0, columnspan=2, sticky="ew", padx=24, pady=(20, 8))
        logo = ctk.CTkLabel(h, text="➜", width=44, height=44, corner_radius=12, fg_color=GREEN,
                            text_color="#FFFFFF", font=ctk.CTkFont(size=22, weight="bold"))
        logo.pack(side="left")
        t = ctk.CTkFrame(h, fg_color="transparent")
        t.pack(side="left", padx=12)
        ctk.CTkLabel(t, text="Batch Forwarder", text_color=TEXT,
                     font=ctk.CTkFont(size=22, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(t, text="Send your offer to every customer, five chats at a time",
                     text_color=MUTED, font=ctk.CTkFont(size=12)).pack(anchor="w")

        self.theme_btn = ctk.CTkSegmentedButton(h, values=["Light", "Dark"], command=self.set_theme, width=120)
        self.theme_btn.set(self.cfg.get("theme", "Dark"))
        self.theme_btn.pack(side="right")
        self.pill = ctk.CTkLabel(h, text="●  Checking phone…", corner_radius=14, height=30, fg_color=FIELD,
                                 text_color=MUTED, font=ctk.CTkFont(size=12, weight="bold"))
        self.pill.pack(side="right", padx=12, ipadx=10)

    def field(self, parent, label, value, hint=None):
        ctk.CTkLabel(parent, text=label, text_color=TEXT, font=ctk.CTkFont(size=13, weight="bold")).pack(
            anchor="w", padx=20, pady=(10, 4))
        e = ctk.CTkEntry(parent, height=38, corner_radius=10, border_width=1, border_color=BORDER,
                         fg_color=FIELD, text_color=TEXT)
        e.insert(0, str(value))
        e.pack(fill="x", padx=20)
        if hint:
            ctk.CTkLabel(parent, text=hint, text_color=MUTED, font=ctk.CTkFont(size=11),
                         wraplength=330, justify="left").pack(anchor="w", padx=20, pady=(3, 0))
        return e

    def settings_panel(self):
        col = ctk.CTkScrollableFrame(self, fg_color="transparent", width=380)
        col.grid(row=1, column=0, sticky="nsew", padx=(24, 8), pady=(8, 24))

        # --- phone
        c = Card(col, "Phone")
        c.pack(fill="x", pady=(0, 14))
        row = ctk.CTkFrame(c, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=(6, 18))
        self.device = ctk.CTkEntry(row, height=38, corner_radius=10, border_width=1, border_color=BORDER,
                                   fg_color=FIELD, text_color=TEXT, placeholder_text="IP:port")
        self.device.insert(0, self.cfg["device"])
        self.device.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(row, text="Connect", width=96, height=38, corner_radius=10, fg_color=FIELD,
                      hover_color=BORDER, text_color=TEXT, command=self.on_connect).pack(side="left", padx=(8, 0))

        # --- message & audience
        c = Card(col, "Message & audience")
        c.pack(fill="x", pady=(0, 14))
        self.source = self.field(c, "Chat with the offer", self.cfg["source_chat"],
                                 "The latest image in this chat gets forwarded.")
        self.anchor = self.field(c, "Start below this contact", self.cfg["anchor"],
                                 "Only contacts under this one are picked. It is never selected itself. Leave empty to start from the top.")
        ctk.CTkLabel(c, text="Never send to", text_color=TEXT, font=ctk.CTkFont(size=13, weight="bold")).pack(
            anchor="w", padx=20, pady=(12, 4))
        self.exclude = ctk.CTkTextbox(c, height=86, corner_radius=10, border_width=1, border_color=BORDER,
                                      fg_color=FIELD, text_color=TEXT)
        self.exclude.insert("1.0", "\n".join(self.cfg["exclude"]))
        self.exclude.pack(fill="x", padx=20)
        ctk.CTkLabel(c, text="One name per line.", text_color=MUTED, font=ctk.CTkFont(size=11)).pack(
            anchor="w", padx=20, pady=(3, 0))
        self.skip_groups = ctk.CTkSwitch(c, text="Skip group chats", text_color=TEXT, progress_color=GREEN)
        if self.cfg["skip_groups"]:
            self.skip_groups.select()
        self.skip_groups.pack(anchor="w", padx=20, pady=(14, 18))

        # --- pace
        c = Card(col, "Pace")
        c.pack(fill="x", pady=(0, 14))
        ctk.CTkLabel(c, text="Contacts per batch", text_color=TEXT,
                     font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w", padx=20, pady=(8, 6))
        self.batch = ctk.CTkSegmentedButton(c, values=["1", "2", "3", "4", "5"], height=34,
                                            selected_color=GREEN, selected_hover_color=GREEN_HOVER)
        self.batch.set(str(self.cfg["batch_size"]))
        self.batch.pack(fill="x", padx=20)
        dl = ctk.CTkFrame(c, fg_color="transparent")
        dl.pack(fill="x", padx=20, pady=(16, 4))
        ctk.CTkLabel(dl, text="Wait between batches", text_color=TEXT,
                     font=ctk.CTkFont(size=13, weight="bold")).pack(side="left")
        self.delay_lbl = ctk.CTkLabel(dl, text="", text_color=GREEN, font=ctk.CTkFont(size=13, weight="bold"))
        self.delay_lbl.pack(side="right")
        self.delay = ctk.CTkSlider(c, from_=0, to=120, number_of_steps=24, command=self.on_delay,
                                   button_color=GREEN, button_hover_color=GREEN_HOVER, progress_color=GREEN)
        self.delay.set(float(self.cfg["delay"]))
        self.delay.pack(fill="x", padx=20, pady=(0, 4))
        self.on_delay(self.delay.get())
        ctk.CTkLabel(c, text="Longer waits look more natural to WhatsApp.", text_color=MUTED,
                     font=ctk.CTkFont(size=11)).pack(anchor="w", padx=20, pady=(0, 18))

        # --- advanced
        c = Card(col, "Advanced")
        c.pack(fill="x")
        self.adb_path = self.field(c, "ADB path", self.cfg["adb"])
        self.pkg = self.field(c, "WhatsApp app", self.cfg["app_package"],
                              "com.whatsapp.w4b = Business, com.whatsapp = normal WhatsApp")
        ctk.CTkFrame(c, height=16, fg_color="transparent").pack()

    def dashboard_panel(self):
        d = ctk.CTkFrame(self, fg_color="transparent")
        d.grid(row=1, column=1, sticky="nsew", padx=(8, 24), pady=(0, 24))
        d.grid_columnconfigure(0, weight=1)
        d.grid_rowconfigure(1, weight=1)

        self.tabs = tabs = ctk.CTkTabview(d, fg_color="transparent", height=250, anchor="nw",
                              segmented_button_selected_color=GREEN,
                              segmented_button_selected_hover_color=GREEN_HOVER)
        tabs.grid(row=0, column=0, sticky="ew")
        self.forward_tab(tabs.add("  Forward offer  "))
        self.numbers_tab(tabs.add("  Group numbers  "))

        # shared status + live log
        log = Card(d)
        log.grid(row=1, column=0, sticky="nsew", pady=(4, 0))
        top = ctk.CTkFrame(log, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(14, 4))
        st = ctk.CTkFrame(top, fg_color="transparent")
        st.pack(side="left", fill="x", expand=True)
        self.status = ctk.CTkLabel(st, text="Ready", text_color=TEXT, font=ctk.CTkFont(size=16, weight="bold"))
        self.status.pack(anchor="w")
        self.substatus = ctk.CTkLabel(st, text="Every step shows up in the live log below.", text_color=MUTED,
                                      font=ctk.CTkFont(size=12))
        self.substatus.pack(anchor="w")
        self.stop_btn = ctk.CTkButton(top, text="■  Stop", width=100, height=38, corner_radius=10,
                                      fg_color="transparent", border_width=2, border_color=BORDER,
                                      text_color=RED, text_color_disabled=MUTED, hover_color=FIELD, state="disabled",
                                      font=ctk.CTkFont(size=13, weight="bold"), command=self.on_stop)
        self.stop_btn.pack(side="right")
        ctk.CTkButton(top, text="Clear log", width=86, height=38, corner_radius=10, fg_color=FIELD,
                      hover_color=BORDER, text_color=TEXT, command=self.clear_log).pack(side="right", padx=8)
        self.progress = ctk.CTkProgressBar(log, height=5, corner_radius=3, progress_color=GREEN, fg_color=FIELD,
                                           mode="determinate")
        self.progress.pack(fill="x", padx=20, pady=(4, 10))
        self.progress.set(0)
        self.out = ctk.CTkTextbox(log, corner_radius=10, fg_color=FIELD, text_color=TEXT, wrap="word",
                                  font=ctk.CTkFont(family="Consolas", size=12), border_width=0)
        self.out.pack(fill="both", expand=True, padx=20, pady=(0, 18))
        self.out.configure(state="disabled")
        self.log("Live log: every step the app does on your phone is shown here.")

    def forward_tab(self, t):
        t.grid_columnconfigure((0, 1, 2), weight=1, uniform="s")
        self.stat_total = Stat(t, "Total sent", GREEN)
        self.stat_total.grid(row=0, column=0, sticky="ew", padx=(0, 7))
        self.stat_session = Stat(t, "This session")
        self.stat_session.grid(row=0, column=1, sticky="ew", padx=7)
        self.stat_batches = Stat(t, "Batches")
        self.stat_batches.grid(row=0, column=2, sticky="ew", padx=(7, 0))
        c = Card(t)
        c.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        r = ctk.CTkFrame(c, fg_color="transparent")
        r.pack(fill="x", padx=20, pady=18)
        self.start_btn = ctk.CTkButton(r, text="▶  Start forwarding", height=44, corner_radius=12,
                                       fg_color=GREEN, hover_color=GREEN_HOVER, text_color="#FFFFFF",
                                       font=ctk.CTkFont(size=14, weight="bold"), command=self.on_start)
        self.start_btn.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(r, text="Open sent list", width=120, height=44, corner_radius=12, fg_color=FIELD,
                      hover_color=BORDER, text_color=TEXT, command=self.open_log).pack(side="left", padx=8)
        ctk.CTkButton(r, text="Reset sent list", width=120, height=44, corner_radius=12, fg_color=FIELD,
                      hover_color=BORDER, text_color=TEXT, command=self.on_reset).pack(side="left")

    def numbers_tab(self, t):
        t.grid_columnconfigure((0, 1, 2), weight=1, uniform="s")
        self.stat_numbers = Stat(t, "Numbers saved", GREEN)
        self.stat_numbers.grid(row=0, column=0, sticky="ew", padx=(0, 7))
        self.stat_groups = Stat(t, "Groups read")
        self.stat_groups.grid(row=0, column=1, sticky="ew", padx=7)
        self.stat_new = Stat(t, "New this run")
        self.stat_new.grid(row=0, column=2, sticky="ew", padx=(7, 0))
        c = Card(t)
        c.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        r = ctk.CTkFrame(c, fg_color="transparent")
        r.pack(fill="x", padx=20, pady=(18, 8))
        self.group = ctk.CTkEntry(r, height=40, corner_radius=10, border_width=1, border_color=BORDER,
                                  fg_color=FIELD, text_color=TEXT, placeholder_text="Exact group name")
        if self.cfg.get("export_group"):
            self.group.insert(0, self.cfg["export_group"])
        self.group.pack(side="left", fill="x", expand=True)
        self.export_btn = ctk.CTkButton(r, text="⤓  Read numbers", width=150, height=40, corner_radius=10,
                                        fg_color=GREEN, hover_color=GREEN_HOVER, text_color="#FFFFFF",
                                        font=ctk.CTkFont(size=13, weight="bold"), command=self.on_extract)
        self.export_btn.pack(side="left", padx=(8, 0))
        r2 = ctk.CTkFrame(c, fg_color="transparent")
        r2.pack(fill="x", padx=20, pady=(0, 16))
        ctk.CTkLabel(r2, text="Only numbers not saved in your phone · kept inside the app",
                     text_color=MUTED, font=ctk.CTkFont(size=11)).pack(side="left")
        ctk.CTkButton(r2, text="Clear", width=70, height=30, corner_radius=8, fg_color=FIELD,
                      hover_color=BORDER, text_color=RED, command=self.on_clear_numbers).pack(side="right")
        ctk.CTkButton(r2, text="View", width=70, height=30, corner_radius=8, fg_color=FIELD,
                      hover_color=BORDER, text_color=TEXT, command=self.view_numbers).pack(side="right", padx=8)
        ctk.CTkButton(r2, text="Export CSV…", width=110, height=30, corner_radius=8, fg_color=FIELD,
                      hover_color=BORDER, text_color=TEXT, command=self.on_export_csv).pack(side="right")

    # ================= helpers =================
    def clear_log(self):
        self.out.configure(state="normal"); self.out.delete("1.0", "end"); self.out.configure(state="disabled")

    def log(self, msg):
        self.out.configure(state="normal")
        self.out.insert("end", time.strftime("%H:%M:%S  ") + msg + "\n")
        self.out.see("end")
        self.out.configure(state="disabled")

    def on_delay(self, v):
        self.delay_lbl.configure(text=f"{int(v)} sec")

    def set_theme(self, mode):
        ctk.set_appearance_mode(mode)
        self.cfg["theme"] = mode
        self.save_cfg(self.cfg)

    def set_pill(self, text, color):
        self.pill.configure(text="●  " + text, text_color=color)

    def refresh_stats(self):
        self.stat_total.set(count_sent())
        self.stat_session.set(self.session_sent)
        self.stat_batches.set(self.batches)
        if time.time() - getattr(self, "_numbers_at", 0) > 2:
            self._numbers_at = time.time()
            store = NumberStore(NUMBERS_PATH)
            self.stat_numbers.set(len(store.known)); self.stat_groups.set(len(store.groups()))
        self.stat_new.set(self.ext_new)

    def set_running(self, running):
        self.start_btn.configure(state="disabled" if running else "normal",
                                 fg_color=FIELD if running else GREEN)
        self.export_btn.configure(state="disabled" if running else "normal",
                                  fg_color=FIELD if running else GREEN)
        self.stop_btn.configure(state="normal" if running else "disabled",
                                border_color=RED if running else BORDER)
        self._numbers_at = 0
        if running:
            self.progress.configure(mode="indeterminate"); self.progress.start()
        else:
            self.progress.stop(); self.progress.configure(mode="determinate"); self.progress.set(0)

    def save_cfg(self, cfg):
        try:
            with open(CFG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=1)
        except OSError:
            pass

    def read_cfg(self):
        src = self.source.get().strip()
        if not src:
            messagebox.showerror("Settings", "Enter the chat that has the offer message.")
            return None
        cfg = dict(self.cfg)
        cfg.update({
            "device": self.device.get().strip(),
            "source_chat": src,
            "anchor": self.anchor.get().strip(),
            "batch_size": int(self.batch.get() or 5),
            "delay": int(self.delay.get()),
            "skip_groups": bool(self.skip_groups.get()),
            "exclude": [l.strip() for l in self.exclude.get("1.0", "end").splitlines() if l.strip()],
            "adb": self.adb_path.get().strip(),
            "app_package": self.pkg.get().strip() or "com.whatsapp.w4b",
            "export_group": self.group.get().strip(),
        })
        self.save_cfg(cfg)
        self.cfg = cfg
        return cfg

    # ================= actions =================
    def say(self, msg, sent=0, **_):
        self.q.put(("msg", msg, sent))

    def on_connect(self):
        cfg = self.read_cfg()
        if not cfg:
            return
        self.set_pill("Connecting…", AMBER)

        def job():
            try:
                devs = Forwarder(cfg, LOG_PATH, lambda *a, **k: None, threading.Event()).connect()
                self.q.put(("conn", devs[0].split()[0] if devs else None, 0))
            except Exception as e:  # noqa: BLE001
                self.q.put(("conn", None, 0)); self.say(f"⚠ {e}")
        threading.Thread(target=job, daemon=True).start()

    def on_start(self):
        cfg = self.read_cfg()
        if not cfg or (self.worker and self.worker.is_alive()):
            return
        self.stop_event.clear()
        self.mode = "forward"
        self.set_running(True)
        below = f'below "{cfg["anchor"]}"' if cfg["anchor"] else "from the top"
        self.status.configure(text="Forwarding…")
        self.substatus.configure(text=f'From "{cfg["source_chat"]}" · contacts {below} · {cfg["batch_size"]} per batch')
        self.log(f"Started · {cfg['batch_size']} per batch · {cfg['delay']}s wait · {below}")
        fw = Forwarder(cfg, LOG_PATH, self.say, self.stop_event)
        self.worker = threading.Thread(target=lambda: (fw.run(), self.q.put(("done", None, 0))), daemon=True)
        self.worker.start()

    def on_extract(self):
        cfg = self.read_cfg()
        if not cfg or (self.worker and self.worker.is_alive()):
            return
        group = cfg["export_group"]
        if not group:
            messagebox.showerror("Group numbers", "Type the exact group name first.")
            return
        self.stop_event.clear()
        self.mode, self.ext_new = "extract", 0
        self.set_running(True)
        self.status.configure(text="Reading group numbers…")
        self.substatus.configure(text=f'"{group}" · each number is saved the moment it is found')
        self.log(f'── Reading unsaved numbers from "{group}" ──')
        ex = Extractor(cfg, LOG_PATH, self.say, self.stop_event)
        store = NumberStore(NUMBERS_PATH)
        self.worker = threading.Thread(target=lambda: (ex.run_extract(group, store),
                                                       self.q.put(("done", None, 0))), daemon=True)
        self.worker.start()

    def on_export_csv(self):
        store = NumberStore(NUMBERS_PATH)
        groups = store.groups()
        if not groups:
            messagebox.showinfo("Export", "No numbers saved yet. Read a group first.")
            return
        group = None
        if len(groups) > 1:
            listing = "\n".join(f"  • {g} ({n})" for g, n in groups.items())
            ans = messagebox.askyesnocancel("Export", f"Export ALL {len(store.known)} numbers?\n\n{listing}\n\n"
                                            "Yes = all groups   ·   No = only the group typed in the box")
            if ans is None:
                return
            if ans is False:
                group = self.group.get().strip()
                if group not in groups:
                    messagebox.showerror("Export", f'No saved numbers for "{group}".')
                    return
        name = (group or "all groups") + time.strftime(" numbers %Y-%m-%d.csv")
        dest = filedialog.asksaveasfilename(title="Export numbers", defaultextension=".csv", initialfile=name,
                                            filetypes=[("CSV (Excel)", "*.csv")])
        if dest:
            n = store.export(dest, group)
            self.log(f"Exported {n} numbers to {dest}")

    def view_numbers(self):
        if not os.path.exists(NUMBERS_PATH):
            messagebox.showinfo("Numbers", "No numbers saved yet.")
            return
        os.startfile(NUMBERS_PATH)

    def on_clear_numbers(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Clear", "Stop first.")
            return
        store = NumberStore(NUMBERS_PATH)
        if store.known and messagebox.askyesno("Clear numbers",
                                               f"Remove all {len(store.known)} saved numbers from the app?\n"
                                               "A backup file is kept."):
            backup = os.path.join(BASE, time.strftime("numbers-backup-%Y-%m-%d-%H%M%S.csv"))
            store.clear(backup)
            self._numbers_at = 0
            self.refresh_stats()
            self.log(f"Numbers cleared (backup: {os.path.basename(backup)}).")

    def on_stop(self):
        self.stop_event.set()
        self.status.configure(text="Stopping…")
        self.substatus.configure(text="Finishing the current step…")

    def open_log(self):
        if not os.path.exists(LOG_PATH):
            open(LOG_PATH, "a", encoding="utf-8").close()
        os.startfile(LOG_PATH)

    def on_reset(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Reset", "Stop forwarding first.")
            return
        n = count_sent()
        if messagebox.askyesno("Reset sent list",
                               f"Forget all {n} contacts already sent?\n\nDo this only when you post a NEW offer."):
            backup = os.path.join(BASE, time.strftime("sent-backup-%Y-%m-%d-%H%M%S.txt"))
            if os.path.exists(LOG_PATH):
                os.replace(LOG_PATH, backup)
            self.session_sent = self.batches = 0
            self.refresh_stats()
            self.log(f"Sent list cleared (backup: {os.path.basename(backup)}).")

    def poll(self):
        try:
            while True:
                kind, msg, sent = self.q.get_nowait()
                if kind == "conn":
                    if msg:
                        self.set_pill(f"Connected · {msg}", GREEN)
                    else:
                        self.set_pill("Phone not found", RED)
                elif kind == "done":
                    self.set_running(False)
                    if self.status.cget("text") not in ("All done", "Numbers saved"):
                        self.status.configure(text="Ready")
                        self.substatus.configure(text="Press Start to continue where it stopped.")
                else:
                    if sent and self.mode == "extract":
                        self.ext_new += sent
                    elif sent:
                        self.session_sent += sent; self.batches += 1
                    if msg.startswith("✅ Finished"):
                        self.status.configure(text="Numbers saved")
                        self.substatus.configure(text="They are kept in the app. Use Export CSV… to save a copy.")
                    elif msg.startswith("✅"):
                        self.status.configure(text="All done")
                        self.substatus.configure(text="Everyone below the start contact has the offer.")
                    elif msg.startswith("⚠"):
                        self.status.configure(text="Paused")
                        self.substatus.configure(text=msg[2:][:110])
                    self.log(msg)
                self.refresh_stats()
        except queue.Empty:
            pass
        self.after(300, self.poll)


if __name__ == "__main__":
    App().mainloop()
