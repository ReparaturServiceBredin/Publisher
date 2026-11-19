"""Fenster für den Twitch-Giveaway-Bot: starten, mit Twitch anmelden, Kanäle eintragen, los.

Keine Konfigurationsdatei nötig – alles wird im Fenster eingestellt und unter
%APPDATA%\\GiveawayBot\\settings.json gespeichert (Token bleibt auf deinem PC).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import secrets
import sys
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import giveaway_bot as bot

try:
    from bot_identity import CLIENT_ID as BUILTIN_CLIENT_ID
except ImportError:  # pragma: no cover
    BUILTIN_CLIENT_ID = ""
try:
    from bot_version import VERSION
except ImportError:  # pragma: no cover
    VERSION = "0.0.0"

UPDATE_BASE = "https://github.com/ReparaturServiceBredin/Publisher/releases/download/giveaway-bot/"
RESUME_FLAG = "--nach-update"

APP_NAME = "Twitch Giveaway Bot"
BG = "#0F0F14"
CARD = "#1C1C26"
FG = "#ECECF2"
MUTED = "#9A9AB0"
ACCENT = "#9146FF"  # Twitch-Lila
OK = "#22C55E"
BAD = "#EF4444"
NTFY_ANDROID = "https://play.google.com/store/apps/details?id=io.heckel.ntfy"
NTFY_IOS = "https://apps.apple.com/app/ntfy/id1625396347"


def settings_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    d = Path(base) / "GiveawayBot"
    d.mkdir(parents=True, exist_ok=True)
    return d


class Settings:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                self.data = {}
        self.data.setdefault("ntfy_topic", f"giveaway-{secrets.token_hex(6)}")  # geheimer, zufälliger Name
        self.data.setdefault("channels", [])
        self.data.setdefault("dry_run", True)
        self.data.setdefault("notify_on_entry", True)
        self.data.setdefault("autostart", False)
        self.data.setdefault("keep_display_on", True)
        self.data.setdefault("entry_cooldown_sec", 300)
        self.save()

    def __getitem__(self, key):
        return self.data.get(key)

    def __setitem__(self, key, value):
        self.data[key] = value

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)


# ---------------------------------------------------------------- Selbst-Update

def parse_version(text: str) -> tuple:
    return tuple(int("".join(ch for ch in p if ch.isdigit()) or 0) for p in text.strip().lstrip("vV").split("."))


def can_self_update() -> bool:
    return bool(getattr(sys, "frozen", False)) and os.name == "nt"


def check_update() -> str | None:
    """Neuere Version auf GitHub? Gibt die Versionsnummer zurück oder None."""
    req = bot.urllib.request.Request(UPDATE_BASE + "version.txt", headers={"User-Agent": bot.USER_AGENT})
    with bot.urllib.request.urlopen(req, timeout=15) as resp:
        latest = resp.read().decode("utf-8", "replace").strip()
    return latest if latest and parse_version(latest) > parse_version(VERSION) else None


def download_update(progress=None) -> Path:
    exe = Path(sys.executable)
    target = exe.with_name(exe.stem + ".new.exe")
    req = bot.urllib.request.Request(UPDATE_BASE + "GiveawayBot.exe", headers={"User-Agent": bot.USER_AGENT})
    with bot.urllib.request.urlopen(req, timeout=60) as resp, target.open("wb") as fh:
        total = int(resp.headers.get("Content-Length", 0))
        done = 0
        while True:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            if progress and total:
                progress(done / total)
    if total and target.stat().st_size != total:
        target.unlink(missing_ok=True)
        raise RuntimeError("Download unvollständig – bitte erneut versuchen.")
    return target


def apply_update_and_restart(new_exe: Path, resume: bool) -> None:
    """Kleines Skript ersetzt die .exe, sobald sie beendet ist, und startet sie neu."""
    import subprocess
    import tempfile

    exe, pid = Path(sys.executable), os.getpid()
    args = f" {RESUME_FLAG}" if resume else ""
    script = Path(tempfile.gettempdir()) / f"giveawaybot_update_{pid}.cmd"
    script.write_text(
        "@echo off\r\n:wait\r\n"
        f'tasklist /FI "PID eq {pid}" 2>nul | "%SystemRoot%\\System32\\find.exe" "{pid}" >nul && '
        "(ping -n 2 127.0.0.1 >nul & goto wait)\r\n"
        "set n=0\r\n:move\r\n"
        f'move /y "{new_exe}" "{exe}" >nul 2>&1 && goto start\r\n'
        "set /a n+=1\r\nif %n% GEQ 30 goto start\r\nping -n 2 127.0.0.1 >nul\r\ngoto move\r\n"
        f':start\r\nstart "" "{exe}"{args}\r\ndel "%~f0"\r\n',
        encoding="mbcs",
    )
    subprocess.Popen(["cmd", "/c", str(script)], creationflags=0x00000008 | 0x00000200, close_fds=True)


class QueueLogHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q
        self.setFormatter(bot.GermanFormatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        self.q.put(("log", self.format(record), record.levelno))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.settings = Settings(settings_dir() / "settings.json")
        self.events: queue.Queue = queue.Queue()
        self.bot_thread: threading.Thread | None = None
        self.bot_loop: asyncio.AbstractEventLoop | None = None
        self.bot_task: asyncio.Task | None = None
        self.login_cancel = False

        bot.log.setLevel(logging.INFO)
        bot.log.propagate = False
        bot.log.addHandler(QueueLogHandler(self.events))

        self.title(APP_NAME)
        self.geometry("780x940")
        self.minsize(640, 640)
        self.configure(bg=BG)
        self._style()
        self._build()
        self._refresh_account()
        self.after(100, self._poll)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bad_channels: list = []
        resume = RESUME_FLAG in sys.argv and self.settings["resume_after_update"]
        if resume:
            self.settings["resume_after_update"] = False
            self.settings.save()
            self.after(300, lambda: self.write(f"Update auf Version {VERSION} installiert ✔"))
        if (resume or self.settings["autostart"]) and self.settings["access_token"] and self.settings["channels"]:
            self.after(600, self.start)
        if can_self_update():
            self.after(1500, self._startup_update_check)

    # ------------------------------------------------------------ Aussehen
    def _style(self) -> None:
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure(".", background=BG, foreground=FG, font=("Segoe UI", 10))
        st.configure("Card.TFrame", background=CARD)
        st.configure("Card.TLabel", background=CARD, foreground=FG)
        st.configure("Muted.TLabel", background=CARD, foreground=MUTED, font=("Segoe UI", 9))
        st.configure("H.TLabel", background=CARD, foreground=FG, font=("Segoe UI", 12, "bold"))
        st.configure("Title.TLabel", background=BG, foreground=FG, font=("Segoe UI", 18, "bold"))
        st.configure("Code.TLabel", background=CARD, foreground=ACCENT, font=("Consolas", 22, "bold"))
        st.configure("Accent.TButton", background=ACCENT, foreground="white", borderwidth=0, padding=(14, 8),
                     font=("Segoe UI", 10, "bold"))
        st.map("Accent.TButton", background=[("active", "#772CE8"), ("disabled", "#3A3550")])
        st.configure("Ghost.TButton", background=CARD, foreground=FG, borderwidth=1, padding=(10, 6))
        st.map("Ghost.TButton", background=[("active", "#2A2A38")])
        st.configure("Card.TCheckbutton", background=CARD, foreground=FG)
        st.map("Card.TCheckbutton", background=[("active", CARD)])
        st.configure("TEntry", fieldbackground="#14141C", foreground=FG, insertcolor=FG)
        st.configure("TCombobox", fieldbackground="#14141C", background=CARD, foreground=FG, arrowcolor=FG)
        st.map("TCombobox", fieldbackground=[("readonly", "#14141C")], foreground=[("readonly", FG)],
               selectbackground=[("readonly", "#14141C")], selectforeground=[("readonly", FG)])
        self.option_add("*TCombobox*Listbox.background", "#14141C")
        self.option_add("*TCombobox*Listbox.foreground", FG)
        st.configure("Status.TLabel", background=CARD, foreground=FG, font=("Segoe UI", 10, "bold"), padding=(12, 8))
        st.configure("Treeview", background="#14141C", fieldbackground="#14141C", foreground=FG, rowheight=24,
                     borderwidth=0)
        st.configure("Treeview.Heading", background=CARD, foreground=MUTED, relief="flat", font=("Segoe UI", 9, "bold"))
        st.map("Treeview", background=[("selected", "#2A2A40")])
        st.configure("TNotebook", background=BG, borderwidth=0)
        st.configure("TNotebook.Tab", background=CARD, foreground=MUTED, padding=(10, 5))
        st.map("TNotebook.Tab", background=[("selected", ACCENT)], foreground=[("selected", "white")])

    def _card(self, title: str) -> ttk.Frame:
        outer = ttk.Frame(self.body, style="Card.TFrame", padding=14)
        outer.pack(fill="x", pady=6)
        ttk.Label(outer, text=title, style="H.TLabel").pack(anchor="w", pady=(0, 6))
        return outer

    def _build(self) -> None:
        head = ttk.Frame(self)
        head.pack(fill="x", padx=18, pady=(14, 4))
        ttk.Label(head, text="🎁  " + APP_NAME, style="Title.TLabel").pack(side="left")
        self.update_btn = ttk.Button(head, text="⟳  Update", style="Ghost.TButton", command=self.check_updates_clicked)
        self.update_btn.pack(side="right")
        self.version_label = ttk.Label(head, text=f"Version {VERSION}", foreground=MUTED, background=BG)
        self.version_label.pack(side="right", padx=10)
        self.body = ttk.Frame(self, padding=(16, 0))
        self.body.pack(fill="both", expand=True)

        # --- Konto
        acc = self._card("1  Twitch-Konto")
        self.acc_card = acc
        self.acc_status = ttk.Label(acc, text="", style="Card.TLabel")
        self.acc_status.pack(anchor="w")
        self.client_row = ttk.Frame(acc, style="Card.TFrame")
        ttk.Label(self.client_row, text="Twitch-App-ID (einmalig):", style="Card.TLabel").pack(side="left")
        self.client_var = tk.StringVar(value=self.settings["client_id"] or "")
        ttk.Entry(self.client_row, textvariable=self.client_var, width=36).pack(side="left", padx=6)
        self.code_frame = ttk.Frame(acc, style="Card.TFrame")
        ttk.Label(self.code_frame, text="Im Browser bestätigen – dein Code:", style="Card.TLabel").pack(anchor="w")
        self.code_label = ttk.Label(self.code_frame, text="", style="Code.TLabel")
        self.code_label.pack(anchor="w")
        btns = ttk.Frame(acc, style="Card.TFrame")
        btns.pack(anchor="w", pady=(8, 0))
        self.login_btn = ttk.Button(btns, text="Mit Twitch anmelden", style="Accent.TButton", command=self.login)
        self.login_btn.pack(side="left")
        self.logout_btn = ttk.Button(btns, text="Abmelden", style="Ghost.TButton", command=self.logout)

        # --- Kanäle
        ch = self._card("2  Kanäle (einer pro Zeile)")
        self.ch_card = ch
        self.channels = tk.Text(ch, height=4, bg="#14141C", fg=FG, insertbackground=FG, relief="flat",
                                font=("Segoe UI", 10))
        self.channels.insert("1.0", "\n".join(self.settings["channels"] or []))
        self.channels.pack(fill="x")
        ttk.Label(ch, text="Nur wenige Kanäle eintragen – viele Streamer bannen Accounts, die automatisch mitmachen.",
                  style="Muted.TLabel").pack(anchor="w", pady=(4, 0))

        # --- Handy
        ph = self._card("3  Benachrichtigung aufs Handy")
        self.ph_card = ph
        ttk.Label(ph, text="App „ntfy“ installieren und dort dieses Thema abonnieren (geheim halten):",
                  style="Card.TLabel").pack(anchor="w")
        row = ttk.Frame(ph, style="Card.TFrame")
        row.pack(fill="x", pady=4)
        self.topic_var = tk.StringVar(value=self.settings["ntfy_topic"])
        ttk.Entry(row, textvariable=self.topic_var, width=34, font=("Consolas", 11)).pack(side="left")
        ttk.Button(row, text="Kopieren", style="Ghost.TButton", command=self._copy_topic).pack(side="left", padx=6)
        ttk.Button(row, text="Test senden", style="Ghost.TButton", command=self._test_push).pack(side="left")
        links = ttk.Frame(ph, style="Card.TFrame")
        links.pack(anchor="w")
        ttk.Button(links, text="ntfy für Android", style="Ghost.TButton",
                   command=lambda: webbrowser.open(NTFY_ANDROID)).pack(side="left")
        ttk.Button(links, text="ntfy für iPhone", style="Ghost.TButton",
                   command=lambda: webbrowser.open(NTFY_IOS)).pack(side="left", padx=6)
        drow = ttk.Frame(ph, style="Card.TFrame")
        drow.pack(fill="x", pady=(8, 0))
        ttk.Label(drow, text="Discord-Webhook (optional):", style="Muted.TLabel").pack(side="left")
        self.discord_var = tk.StringVar(value=self.settings["discord_webhook"] or "")
        ttk.Entry(drow, textvariable=self.discord_var).pack(side="left", fill="x", expand=True, padx=6)

        # --- Optionen + Start
        op = self._card("Los geht's")
        self.dry_var = tk.BooleanVar(value=bool(self.settings["dry_run"]))
        self.entry_push_var = tk.BooleanVar(value=bool(self.settings["notify_on_entry"]))
        self.auto_var = tk.BooleanVar(value=bool(self.settings["autostart"]))
        ttk.Checkbutton(op, text="Testmodus – nichts in den Chat schreiben, nur melden (zum Ausprobieren)",
                        variable=self.dry_var, style="Card.TCheckbutton").pack(anchor="w")
        ttk.Checkbutton(op, text="Bei jedem Eintrag eine Nachricht aufs Handy", variable=self.entry_push_var,
                        style="Card.TCheckbutton").pack(anchor="w")
        ttk.Checkbutton(op, text="Beim Öffnen automatisch starten", variable=self.auto_var,
                        style="Card.TCheckbutton").pack(anchor="w")
        self.awake_var = tk.BooleanVar(value=bool(self.settings["keep_display_on"]))
        ttk.Checkbutton(op, text="Auch bei gesperrtem PC (Win + L) weiterlaufen – Bildschirm bleibt dafür an",
                        variable=self.awake_var,
                        style="Card.TCheckbutton").pack(anchor="w")
        crow = ttk.Frame(op, style="Card.TFrame")
        crow.pack(anchor="w", pady=(4, 0))
        ttk.Label(crow, text="Pause zwischen zwei Einträgen im selben Kanal:", style="Card.TLabel").pack(side="left")
        self.cooldown_choices = {"2 Minuten": 120, "5 Minuten": 300, "10 Minuten": 600}
        current = next((k for k, v in self.cooldown_choices.items() if v == self.settings["entry_cooldown_sec"]),
                       "5 Minuten")
        self.cooldown_var = tk.StringVar(value=current)
        ttk.Combobox(crow, textvariable=self.cooldown_var, values=list(self.cooldown_choices), width=11,
                     state="readonly").pack(side="left", padx=6)
        srow = ttk.Frame(op, style="Card.TFrame")
        srow.pack(anchor="w", pady=(8, 0))
        self.start_btn = ttk.Button(srow, text="▶  Start", style="Accent.TButton", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(srow, text="■  Stopp", style="Ghost.TButton", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        self.run_label = ttk.Label(srow, text="gestoppt", style="Muted.TLabel")
        self.run_label.pack(side="left", padx=8)

        # --- Rückmeldung: Statuszeile + Einträge / Kanäle / Protokoll
        self.status_var = tk.StringVar(value="Bot gestoppt.")
        ttk.Label(self.body, textvariable=self.status_var, style="Status.TLabel").pack(fill="x", pady=(6, 4))
        tabs = ttk.Notebook(self.body)
        tabs.pack(fill="both", expand=True, pady=(0, 12))
        self.tabs = tabs

        ef = ttk.Frame(tabs, style="Card.TFrame")
        self.entries = ttk.Treeview(ef, columns=("zeit", "kanal", "befehl", "status"), show="headings", height=8)
        for col, text, width in (("zeit", "Zeit", 70), ("kanal", "Kanal", 130), ("befehl", "Befehl", 110),
                                 ("status", "Status", 420)):
            self.entries.heading(col, text=text, anchor="w")
            self.entries.column(col, width=width, anchor="w", stretch=col == "status")
        for tag, color in (("erkannt", "#C8C8D8"), ("gesendet", "#93C5FD"), ("angenommen", OK), ("bestaetigt", OK),
                           ("testmodus", "#F59E0B"), ("abgelehnt", BAD), ("fehler", BAD), ("uebersprungen", MUTED),
                           ("gewinn", "#FACC15"), ("whisper", "#F472B6")):
            self.entries.tag_configure(tag, foreground=color)
        self.entries.pack(fill="both", expand=True)
        self.entries_empty = ttk.Label(ef, text="Noch keine Giveaways erkannt – der Bot liest mit und meldet sich hier.",
                                       style="Muted.TLabel")
        self.entries_empty.place(relx=0.5, rely=0.55, anchor="center")
        tabs.add(ef, text="  Einträge  ")

        cf = ttk.Frame(tabs, style="Card.TFrame")
        self.chan_table = ttk.Treeview(cf, columns=("kanal", "status", "nachrichten", "eintrag"), show="headings", height=8)
        for col, text, width in (("kanal", "Kanal", 150), ("status", "Status", 140), ("nachrichten", "Chat gelesen", 110),
                                 ("eintrag", "Letzter Eintrag", 330)):
            self.chan_table.heading(col, text=text, anchor="w")
            self.chan_table.column(col, width=width, anchor="w", stretch=col == "eintrag")
        self.chan_table.pack(fill="both", expand=True)
        tabs.add(cf, text="  Kanäle  ")

        lf = ttk.Frame(tabs, style="Card.TFrame")
        self.log = ScrolledText(lf, height=8, bg="#0B0B10", fg="#C8C8D8", relief="flat",
                                font=("Consolas", 9), state="disabled")
        self.log.pack(fill="both", expand=True)
        self.log.tag_config("warn", foreground="#F59E0B")
        self.log.tag_config("err", foreground=BAD)
        tabs.add(lf, text="  Protokoll  ")
        self._reset_stats()

    # ------------------------------------------------------------ Hilfen
    def client_id(self) -> str:
        return (BUILTIN_CLIENT_ID or self.client_var.get() or self.settings["client_id"] or "").strip()

    def write(self, text: str, level: int = logging.INFO) -> None:
        self.log.configure(state="normal")
        tag = "err" if level >= logging.ERROR else "warn" if level >= logging.WARNING else None
        self.log.insert("end", text + "\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _poll(self) -> None:
        try:
            while True:
                ev = self.events.get_nowait()
                if ev[0] == "log":
                    self.write(ev[1], ev[2])
                elif ev[0] == "call":
                    ev[1]()
                elif ev[0] == "bot":
                    self._on_bot_event(ev[1], ev[2])
        except queue.Empty:
            pass
        self.after(120, self._poll)

    # ------------------------------------------------------------ Updates
    def _startup_update_check(self) -> None:
        def job():
            try:
                latest = check_update()
            except Exception as exc:  # noqa: BLE001 – Update-Probleme dürfen den Bot nie stören
                bot.log.info("Update-Prüfung nicht möglich: %s", exc)
                return
            if latest:
                self._later(lambda: self._offer_update(latest))

        threading.Thread(target=job, daemon=True).start()

    def check_updates_clicked(self) -> None:
        self.update_btn.configure(state="disabled", text="Suche …")

        def job():
            try:
                latest = check_update()
                self._later(lambda: self._checked(latest))
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                self._later(lambda: (self._reset_update_btn(),
                                     messagebox.showerror(APP_NAME, f"Update-Prüfung fehlgeschlagen:\n{msg}")))

        threading.Thread(target=job, daemon=True).start()

    def _reset_update_btn(self) -> None:
        self.update_btn.configure(state="normal", text="⟳  Update")

    def _checked(self, latest) -> None:
        self._reset_update_btn()
        if latest:
            self._offer_update(latest)
        else:
            messagebox.showinfo(APP_NAME, f"Du bist auf dem neuesten Stand (Version {VERSION}).")

    def _offer_update(self, latest: str) -> None:
        self.update_btn.configure(text=f"⬆  Update {latest}")
        if not messagebox.askyesno(APP_NAME, f"Update verfügbar: Version {latest} (du hast {VERSION}).\n\n"
                                             "Jetzt installieren? Der Bot startet danach von selbst neu"
                                             + (" und läuft weiter." if self.bot_thread else ".")):
            return
        if not can_self_update():
            webbrowser.open(UPDATE_BASE + "GiveawayBot.exe")
            return
        self._save_form()
        resume = bool(self.bot_thread)
        self.settings["resume_after_update"] = resume
        self.settings.save()
        self.update_btn.configure(state="disabled", text="Lade 0 %")
        self.write(f"Lade Update {latest} …")

        def job():
            try:
                path = download_update(lambda f: self._later(lambda: self.update_btn.configure(text=f"Lade {f:.0%}")))
                self._later(lambda: self._install(path, resume))
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                self._later(lambda: (self._reset_update_btn(),
                                     messagebox.showerror(APP_NAME, f"Update fehlgeschlagen:\n{msg}\n\n"
                                                                    "Der Bot läuft unverändert weiter.")))

        threading.Thread(target=job, daemon=True).start()

    def _install(self, path: Path, resume: bool) -> None:
        self.write("Installiere Update – der Bot startet gleich neu …")
        self.stop()
        apply_update_and_restart(path, resume)
        self.after(1200, self.destroy)

    # ------------------------------------------------------------ Rückmeldungen vom Bot
    def _reset_stats(self) -> None:
        self.stat = {"chat": "–", "whispers": "–", "joined": set(), "messages": 0, "entries": 0, "accepted": 0}
        self.chan_rows: dict = {}
        self.entry_rows: dict = {}
        self.msg_counts: dict = {}
        for table in (self.entries, self.chan_table):
            table.delete(*table.get_children())
        self.entries_empty.place(relx=0.5, rely=0.55, anchor="center")
        for c in self.settings["channels"] or []:
            self._chan(c, status="wartet …")

    def _chan(self, channel: str, **values) -> None:
        if channel not in self.chan_rows:
            self.chan_rows[channel] = self.chan_table.insert("", "end", values=(f"#{channel}", "", "0", "–"))
        row = self.chan_rows[channel]
        current = list(self.chan_table.item(row, "values"))
        for key, idx in (("status", 1), ("messages", 2), ("entry", 3)):
            if key in values:
                current[idx] = values[key]
        self.chan_table.item(row, values=current)

    def _entry_row(self, key, channel: str, command: str, status_text: str, tag: str) -> None:
        self.entries_empty.place_forget()
        now = time.strftime("%H:%M:%S")
        if key in self.entry_rows:
            row = self.entry_rows[key]
            vals = list(self.entries.item(row, "values"))
            vals[3] = status_text
            if command:
                vals[2] = command
            self.entries.item(row, values=vals, tags=(tag,))
        else:
            shown = channel if channel.startswith("✉") else f"#{channel}"
            self.entry_rows[key] = self.entries.insert("", 0, values=(now, shown, command, status_text), tags=(tag,))

    def _update_status(self) -> None:
        st = self.stat
        if not self.bot_thread:
            self.status_var.set("Bot gestoppt.")
            return
        total = len(self.settings["channels"] or [])
        self.status_var.set(
            f"Chat: {st['chat']}     Flüstern: {st['whispers']}     Kanäle betreten: {len(st['joined'])}/{total}\n"
            + f"{st['messages']:,} Chatnachrichten gelesen     Giveaways erkannt: {st['entries']}     "
              f"davon eingetragen: {st['accepted']}".replace(",", ".")
        )

    def _on_bot_event(self, kind: str, d: dict) -> None:
        st = self.stat
        if kind == "chat":
            st["chat"] = {"verbunden": "verbunden ✔", "getrennt": "getrennt – verbinde neu …"}.get(d["state"], d["state"])
            if d["state"] == "getrennt":
                st["joined"].clear()
        elif kind == "whispers":
            st["whispers"] = "aktiv ✔" if d["state"] == "aktiv" else d["state"]
        elif kind == "joined":
            st["joined"].add(d["channel"])
            self._chan(d["channel"], status="betreten ✔")
        elif kind == "join_failed":
            self._chan(d["channel"], status="❌ nicht betreten – Name prüfen")
            self.write(f"Kanal #{d['channel']} konnte nicht betreten werden – gibt es den Namen?", logging.WARNING)
        elif kind == "message":
            st["messages"] += 1
            c = d["channel"]
            self.msg_counts[c] = self.msg_counts.get(c, 0) + 1
            self._chan(c, messages=f"{self.msg_counts[c]:,}".replace(",", "."))
        elif kind == "entry":
            key, status = d["id"], d["status"]
            labels = {
                "erkannt": ("🎁 Giveaway erkannt – " + d.get("detail", ""), "erkannt"),
                "testmodus": ("🧪 Testmodus – nicht gesendet (Haken bei Testmodus entfernen, um mitzumachen)", "testmodus"),
                "gesendet": ("📨 " + d.get("detail", "gesendet"), "gesendet"),
                "angenommen": ("✅ Eingetragen – von Twitch angenommen", "angenommen"),
                "abgelehnt": ("❌ Abgelehnt: " + d.get("detail", ""), "abgelehnt"),
                "fehler": ("⚠ " + d.get("detail", "Fehler"), "fehler"),
            }
            text, tag = labels.get(status, (status, "erkannt"))
            self._entry_row(key, d["channel"], d.get("command", ""), text, tag)
            if status == "erkannt":
                st["entries"] += 1
            if status == "angenommen":
                st["accepted"] += 1
            self._chan(d["channel"], entry=f"{time.strftime('%H:%M')}  {text}")
            if status in ("angenommen", "abgelehnt"):
                self.tabs.select(0)
        elif kind == "confirmed":
            key = next((k for k in reversed(list(self.entry_rows)) if isinstance(k, int)
                        and self.entries.item(self.entry_rows[k], "values")[1] == f"#{d['channel']}"), None)
            text = f"✅ Vom Giveaway-Bot bestätigt ({d['sender']})"
            self._entry_row(key if key is not None else ("c", d["channel"], time.time()), d["channel"], "", text, "bestaetigt")
            self._chan(d["channel"], entry=f"{time.strftime('%H:%M')}  {text}")
        elif kind == "skipped":
            self._entry_row(("s", d["channel"], time.time()), d["channel"], d["command"],
                            "⏸ Erkannt, aber schon eingetragen (Pause bis zum nächsten Eintrag)", "uebersprungen")
        elif kind == "win":
            self._entry_row(("w", time.time()), d["channel"], "", f"🎉 GEWINN? {d['sender']}: {d['text']}", "gewinn")
            self._alert(f"🎉 Möglicher Gewinn in #{d['channel']}!\n\n{d['sender']}: {d['text']}")
        elif kind == "whisper":
            self._entry_row(("f", time.time()), "✉ Flüstern", "", f"✉ {d['sender']}: {d['text']}", "whisper")
            self._alert(f"✉ Flüsternachricht von {d['sender']}:\n\n{d['text']}")
        self._update_status()

    def _alert(self, text: str) -> None:
        """Fenster nach vorne holen + Hinweis – auch wenn das Handy gerade nicht in der Nähe ist."""
        self.tabs.select(0)
        self.deiconify()
        self.lift()
        self.attributes("-topmost", True)
        self.after(1500, lambda: self.attributes("-topmost", False))
        self.bell()
        messagebox.showinfo(APP_NAME, text)

    def _later(self, fn) -> None:
        self.events.put(("call", fn))

    def _save_form(self) -> None:
        raw = self.channels.get("1.0", "end").replace(",", "\n").replace(" ", "\n").split("\n")
        good, bad = bot.clean_channels(raw)
        self.settings["channels"] = good
        self.bad_channels = bad
        self.channels.delete("1.0", "end")  # bereinigte Namen anzeigen (aus Links wird der Kanalname)
        self.channels.insert("1.0", "\n".join(good + bad))
        self.settings["ntfy_topic"] = self.topic_var.get().strip()
        self.settings["discord_webhook"] = self.discord_var.get().strip()
        self.settings["dry_run"] = self.dry_var.get()
        self.settings["notify_on_entry"] = self.entry_push_var.get()
        self.settings["autostart"] = self.auto_var.get()
        self.settings["keep_display_on"] = self.awake_var.get()
        self.settings["entry_cooldown_sec"] = self.cooldown_choices.get(self.cooldown_var.get(), 300)
        if not BUILTIN_CLIENT_ID:
            self.settings["client_id"] = self.client_var.get().strip()
        self.settings.save()

    def _copy_topic(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.topic_var.get())
        self.write("Thema in die Zwischenablage kopiert.")

    def _test_push(self) -> None:
        self._save_form()
        notifier = bot.Notifier({"ntfy_topic": self.settings["ntfy_topic"], "discord_webhook": self.settings["discord_webhook"]})
        self.write("Sende Test-Nachricht …")
        threading.Thread(target=lambda: asyncio.run(notifier.send(
            "Test vom Giveaway-Bot", "Wenn du das siehst, klappen die Benachrichtigungen 🎉", urgent=True)),
            daemon=True).start()

    # ------------------------------------------------------------ Konto
    def _refresh_account(self) -> None:
        name = self.settings["username"]
        logged_in = bool(self.settings["access_token"] and name)
        if logged_in:
            self.acc_status.configure(text=f"Angemeldet als  {name}  ✔", foreground=OK)
            self.login_btn.pack_forget()
            self.logout_btn.pack(side="left")
            self.client_row.pack_forget()
        else:
            self.acc_status.configure(text="Nicht angemeldet – klicke auf „Mit Twitch anmelden“.", foreground=MUTED)
            self.logout_btn.pack_forget()
            self.login_btn.pack(side="left")
            if not BUILTIN_CLIENT_ID:
                self.client_row.pack(anchor="w", pady=(6, 0))
        self.code_frame.pack_forget()

    def login(self) -> None:
        client_id = self.client_id()
        if not client_id:
            messagebox.showinfo(APP_NAME, "Bitte einmalig die Twitch-App-ID eintragen (dev.twitch.tv → Anwendung "
                                          "registrieren, Client-Typ „Öffentlich“). Danach nur noch „Mit Twitch anmelden“.")
            webbrowser.open("https://dev.twitch.tv/console/apps/create")
            return
        self.login_cancel = False
        self.login_btn.configure(state="disabled", text="Warte auf Bestätigung …")
        self.write("Starte Twitch-Anmeldung …")

        def job():
            try:
                device = bot.start_device_login(client_id)
                self._later(lambda: self._show_code(device))
                webbrowser.open(device.get("verification_uri") or "https://www.twitch.tv/activate")
                token = bot.poll_device_login(client_id, device, cancelled=lambda: self.login_cancel)
                info = bot.http_json(bot.VALIDATE_URL, headers={"Authorization": f"OAuth {token['access_token']}"})[1]
                self._later(lambda: self._login_done(client_id, token, info))
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                self._later(lambda: self._login_failed(msg))

        threading.Thread(target=job, daemon=True).start()

    def _show_code(self, device: dict) -> None:
        self.code_label.configure(text=device.get("user_code", ""))
        self.code_frame.pack(anchor="w", pady=(6, 0))
        self.write("Browser geöffnet – bei Twitch anmelden und „Autorisieren“ klicken.")

    def _login_done(self, client_id: str, token: dict, info: dict) -> None:
        self.settings["client_id"] = client_id
        self.settings["access_token"] = token["access_token"]
        self.settings["refresh_token"] = token.get("refresh_token", "")
        self.settings["username"] = (info.get("login") or "").lower()
        self.settings.save()
        self.login_btn.configure(state="normal", text="Mit Twitch anmelden")
        self._refresh_account()
        self.write(f"Angemeldet als {self.settings['username']} ✔")

    def _login_failed(self, msg: str) -> None:
        self.login_btn.configure(state="normal", text="Mit Twitch anmelden")
        self._refresh_account()
        self.write(f"Anmeldung fehlgeschlagen: {msg}", logging.ERROR)
        messagebox.showerror(APP_NAME, f"Anmeldung fehlgeschlagen:\n{msg}")

    def logout(self) -> None:
        if self.bot_thread:
            self.stop()
        for key in ("access_token", "refresh_token", "username"):
            self.settings[key] = ""
        self.settings.save()
        self._refresh_account()
        self.write("Abgemeldet.")

    # ------------------------------------------------------------ Bot starten/stoppen
    def _token_updated(self, access: str, refresh: str) -> None:
        self.settings["access_token"], self.settings["refresh_token"] = access, refresh
        self.settings.save()

    def start(self) -> None:
        if self.bot_thread:
            return
        self._save_form()
        if not self.settings["access_token"]:
            messagebox.showinfo(APP_NAME, "Bitte zuerst mit Twitch anmelden.")
            return
        if self.bad_channels:
            messagebox.showerror(APP_NAME, "Diese Einträge sind keine gültigen Twitch-Kanäle:\n\n"
                                 + "\n".join(self.bad_channels)
                                 + "\n\nBitte nur den Kanalnamen oder den Twitch-Link eintragen, z. B. montanablack88.")
            return
        if not self.settings["channels"]:
            messagebox.showinfo(APP_NAME, "Bitte mindestens einen Kanal eintragen.")
            return
        try:
            cfg = bot.normalize_config({
                "username": self.settings["username"], "client_id": self.settings["client_id"],
                "access_token": self.settings["access_token"], "channels": self.settings["channels"],
                "ntfy_topic": self.settings["ntfy_topic"], "discord_webhook": self.settings["discord_webhook"],
                "dry_run": self.settings["dry_run"], "notify_on_entry": self.settings["notify_on_entry"],
                **{k: v for k, v in self.settings.data.items() if k in bot.DEFAULTS and k not in (
                    "channels", "dry_run", "notify_on_entry", "ntfy_topic", "discord_webhook")},
            })
        except bot.FatalError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        cfg["refresh_token"] = self.settings["refresh_token"] or ""
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self._reset_stats()
        self.ch_card.pack_forget()  # Platz für die Rückmeldungen – Einstellungen kommen beim Stoppen zurück
        self.ph_card.pack_forget()
        self.run_label.configure(text="läuft" + (" (Testmodus)" if cfg["dry_run"] else " – SCHARF"), foreground=OK)

        def runner():
            loop = asyncio.new_event_loop()
            self.bot_loop = loop
            asyncio.set_event_loop(loop)
            code = 0
            try:
                self.bot_task = loop.create_task(bot.run_bot(
                    cfg, on_token_update=self._token_updated,
                    on_event=lambda kind, **d: self.events.put(("bot", kind, d))))
                code = loop.run_until_complete(self.bot_task)
            except asyncio.CancelledError:
                bot.log.info("Bot gestoppt.")
            except Exception as exc:  # noqa: BLE001
                bot.log.error("Bot abgestürzt: %s", exc)
                code = 1
            finally:
                loop.close()
                self.bot_loop = self.bot_task = None
                self._later(lambda: self._stopped(code))

        self.bot_thread = threading.Thread(target=runner, daemon=True)
        self.bot_thread.start()

    def stop(self) -> None:
        if self.bot_loop and self.bot_task:
            self.run_label.configure(text="stoppe …", foreground=MUTED)
            self.bot_loop.call_soon_threadsafe(self.bot_task.cancel)

    def _stopped(self, code: int) -> None:
        self.bot_thread = None
        self.ch_card.pack(fill="x", pady=6, after=self.acc_card)
        self.ph_card.pack(fill="x", pady=6, after=self.ch_card)
        self._update_status()
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.run_label.configure(text="gestoppt", foreground=MUTED)
        if code == bot.EXIT_LOGIN:
            for key in ("access_token", "refresh_token"):
                self.settings[key] = ""
            self.settings.save()
            self._refresh_account()
            messagebox.showwarning(APP_NAME, "Die Twitch-Anmeldung ist abgelaufen. Bitte neu anmelden und erneut starten.")

    def _close(self) -> None:
        self._save_form()
        self.stop()
        self.after(300, self.destroy)


def main() -> None:
    if "--selftest" in sys.argv:
        sys.exit(bot.selftest())
    App().mainloop()


if __name__ == "__main__":
    main()
