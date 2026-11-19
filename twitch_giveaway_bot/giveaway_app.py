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
        self.save()

    def __getitem__(self, key):
        return self.data.get(key)

    def __setitem__(self, key, value):
        self.data[key] = value

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)


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
        if self.settings["autostart"] and self.settings["access_token"] and self.settings["channels"]:
            self.after(600, self.start)

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

    def _card(self, title: str) -> ttk.Frame:
        outer = ttk.Frame(self.body, style="Card.TFrame", padding=14)
        outer.pack(fill="x", pady=6)
        ttk.Label(outer, text=title, style="H.TLabel").pack(anchor="w", pady=(0, 6))
        return outer

    def _build(self) -> None:
        ttk.Label(self, text="🎁  " + APP_NAME, style="Title.TLabel").pack(anchor="w", padx=18, pady=(14, 4))
        self.body = ttk.Frame(self, padding=(16, 0))
        self.body.pack(fill="both", expand=True)

        # --- Konto
        acc = self._card("1  Twitch-Konto")
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
        self.channels = tk.Text(ch, height=4, bg="#14141C", fg=FG, insertbackground=FG, relief="flat",
                                font=("Segoe UI", 10))
        self.channels.insert("1.0", "\n".join(self.settings["channels"] or []))
        self.channels.pack(fill="x")
        ttk.Label(ch, text="Nur wenige Kanäle eintragen – viele Streamer bannen Accounts, die automatisch mitmachen.",
                  style="Muted.TLabel").pack(anchor="w", pady=(4, 0))

        # --- Handy
        ph = self._card("3  Benachrichtigung aufs Handy")
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
        op = self._card("4  Los geht's")
        self.dry_var = tk.BooleanVar(value=bool(self.settings["dry_run"]))
        self.entry_push_var = tk.BooleanVar(value=bool(self.settings["notify_on_entry"]))
        self.auto_var = tk.BooleanVar(value=bool(self.settings["autostart"]))
        ttk.Checkbutton(op, text="Testmodus – nichts in den Chat schreiben, nur melden (zum Ausprobieren)",
                        variable=self.dry_var, style="Card.TCheckbutton").pack(anchor="w")
        ttk.Checkbutton(op, text="Bei jedem Eintrag eine Nachricht aufs Handy", variable=self.entry_push_var,
                        style="Card.TCheckbutton").pack(anchor="w")
        ttk.Checkbutton(op, text="Beim Öffnen automatisch starten", variable=self.auto_var,
                        style="Card.TCheckbutton").pack(anchor="w")
        srow = ttk.Frame(op, style="Card.TFrame")
        srow.pack(anchor="w", pady=(8, 0))
        self.start_btn = ttk.Button(srow, text="▶  Start", style="Accent.TButton", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(srow, text="■  Stopp", style="Ghost.TButton", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        self.run_label = ttk.Label(srow, text="gestoppt", style="Muted.TLabel")
        self.run_label.pack(side="left", padx=8)

        self.log = ScrolledText(self.body, height=10, bg="#0B0B10", fg="#C8C8D8", relief="flat",
                                font=("Consolas", 9), state="disabled")
        self.log.pack(fill="both", expand=True, pady=(6, 12))
        self.log.tag_config("warn", foreground="#F59E0B")
        self.log.tag_config("err", foreground=BAD)

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
        except queue.Empty:
            pass
        self.after(120, self._poll)

    def _later(self, fn) -> None:
        self.events.put(("call", fn))

    def _save_form(self) -> None:
        names = [c.strip().lstrip("#").lower() for c in self.channels.get("1.0", "end").replace(",", "\n").split("\n")]
        self.settings["channels"] = [c for c in dict.fromkeys(names) if c]
        self.settings["ntfy_topic"] = self.topic_var.get().strip()
        self.settings["discord_webhook"] = self.discord_var.get().strip()
        self.settings["dry_run"] = self.dry_var.get()
        self.settings["notify_on_entry"] = self.entry_push_var.get()
        self.settings["autostart"] = self.auto_var.get()
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
        self.run_label.configure(text="läuft" + (" (Testmodus)" if cfg["dry_run"] else " – SCHARF"), foreground=OK)

        def runner():
            loop = asyncio.new_event_loop()
            self.bot_loop = loop
            asyncio.set_event_loop(loop)
            code = 0
            try:
                self.bot_task = loop.create_task(bot.run_bot(cfg, on_token_update=self._token_updated))
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
