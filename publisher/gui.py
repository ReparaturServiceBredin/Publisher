"""Moderne Oberfläche (customtkinter): Erstellen · Videobox · Verbindungen · Freigaben."""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from . import updater
from ._version import __version__
from .config import load_app_settings

ACCENT = "#7C3AED"
ACCENT_HOVER = "#6D28D9"
BG = "#0F0F14"
PANEL = "#17171F"
CARD = "#1F1F2A"
MUTED = "#9A9AB0"
OK = "#22C55E"
WARN = "#F59E0B"
BAD = "#EF4444"

VIDEO_TYPES = [("Videos", "*.mp4 *.mkv *.mov *.flv *.ts *.webm *.avi *.m4v"), ("Alle Dateien", "*.*")]
YT_PRIVACY = {"Privat": "private", "Nicht gelistet": "unlisted", "Öffentlich": "public"}
TT_PRIVACY = {
    "SELF_ONLY": "Nur ich", "MUTUAL_FOLLOW_FRIENDS": "Freunde",
    "FOLLOWER_OF_CREATOR": "Follower", "PUBLIC_TO_EVERYONE": "Öffentlich",
}


def open_path(path: Path) -> None:
    if os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def font(size: int = 13, weight: str = "normal") -> ctk.CTkFont:
    return ctk.CTkFont(family="Segoe UI", size=size, weight=weight)


def card(master, **kw) -> ctk.CTkFrame:
    return ctk.CTkFrame(master, fg_color=CARD, corner_radius=14, **kw)


def accent_button(master, text, command, **kw) -> ctk.CTkButton:
    return ctk.CTkButton(master, text=text, command=command, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                         corner_radius=10, font=font(13, "bold"), **kw)


def ghost_button(master, text, command, **kw) -> ctk.CTkButton:
    return ctk.CTkButton(master, text=text, command=command, fg_color="transparent", border_width=1,
                         border_color="#3A3A4A", hover_color="#2A2A38", corner_radius=10, font=font(12), **kw)


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.settings = load_app_settings()
        self.output_root = self.settings.base_dir / "Fertig"
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.events: queue.Queue = queue.Queue()
        self.busy = False
        self.pending_update: Path | None = None
        logging.basicConfig(filename=self.settings.base_dir / "publisher.log", level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(message)s")

        self.title(f"Publisher {__version__}")
        self.geometry("1100x720")
        self.minsize(900, 600)
        self.configure(fg_color=BG)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_sidebar()
        self.pages: dict[str, ctk.CTkFrame] = {}
        for name, builder in (
            ("erstellen", self._page_create), ("videobox", self._page_videobox),
            ("verbindungen", self._page_connections), ("freigaben", self._page_permissions),
        ):
            frame = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
            frame.grid(row=0, column=1, sticky="nsew")
            builder(frame)
            self.pages[name] = frame
        self.show("erstellen")
        self.after(120, self._poll)
        if updater.can_self_update():
            threading.Thread(target=self._update_worker, daemon=True).start()

    # ------------------------------------------------------------ Grundgerüst
    def _build_sidebar(self) -> None:
        bar = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=0, width=220)
        bar.grid(row=0, column=0, sticky="nsw")
        bar.grid_propagate(False)
        ctk.CTkLabel(bar, text="◆ Publisher", font=font(22, "bold"), text_color="white").pack(anchor="w", padx=22, pady=(26, 2))
        ctk.CTkLabel(bar, text="Highlights aus deinen Streams", font=font(12), text_color=MUTED).pack(anchor="w", padx=22)
        self.nav: dict[str, ctk.CTkButton] = {}
        ctk.CTkFrame(bar, height=18, fg_color="transparent").pack()
        for key, label in (("erstellen", "✦   Erstellen"), ("videobox", "▣   Videobox"),
                           ("verbindungen", "⇄   Verbindungen"), ("freigaben", "✓   Freigaben")):
            b = ctk.CTkButton(bar, text=label, anchor="w", height=42, corner_radius=10, font=font(14),
                              fg_color="transparent", hover_color="#262633", text_color="#DADAE8",
                              command=lambda k=key: self.show(k))
            b.pack(fill="x", padx=12, pady=3)
            self.nav[key] = b
        bottom = ctk.CTkFrame(bar, fg_color="transparent")
        bottom.pack(side="bottom", fill="x", padx=14, pady=16)
        self.update_label = ctk.CTkLabel(bottom, text=f"Version {__version__}", font=font(11), text_color=MUTED)
        self.update_label.pack(anchor="w")
        ghost_button(bottom, "Ausgabeordner öffnen", lambda: open_path(self.output_root)).pack(fill="x", pady=(8, 0))

    def show(self, key: str) -> None:
        for k, b in self.nav.items():
            b.configure(fg_color=ACCENT if k == key else "transparent",
                        hover_color=ACCENT_HOVER if k == key else "#262633")
        self.pages[key].tkraise()
        if key == "videobox":
            self.refresh_videobox()
        elif key == "freigaben":
            self.refresh_permissions()
        elif key == "verbindungen":
            self.refresh_connections()

    def header(self, master, title: str, subtitle: str) -> None:
        ctk.CTkLabel(master, text=title, font=font(26, "bold"), text_color="white").pack(anchor="w", padx=32, pady=(28, 0))
        ctk.CTkLabel(master, text=subtitle, font=font(13), text_color=MUTED).pack(anchor="w", padx=32, pady=(2, 16))

    # ------------------------------------------------------------ Hintergrundaufgaben
    def run_bg(self, fn, on_done=None, on_error=None) -> None:
        def worker():
            try:
                res = fn()
                self.events.put(("call", on_done, res))
            except Exception as exc:  # noqa: BLE001
                logging.error(traceback.format_exc())
                self.events.put(("call", on_error or self._show_error, exc))

        threading.Thread(target=worker, daemon=True).start()

    def _show_error(self, exc: Exception) -> None:
        messagebox.showerror("Publisher", str(exc)[:1500])

    def _poll(self) -> None:
        try:
            while True:
                ev = self.events.get_nowait()
                if ev[0] == "call" and ev[1]:
                    ev[1](ev[2])
                elif ev[0] == "progress":
                    self._on_progress(ev[1], ev[2])
                elif ev[0] == "update_ready":
                    self.pending_update = ev[1]
                    self.update_label.configure(text=f"Update {ev[2]} bereit", text_color=OK)
                    self._maybe_apply_update()
                elif ev[0] == "update_info":
                    self.update_label.configure(text=ev[1])
        except queue.Empty:
            pass
        self.after(120, self._poll)

    # ------------------------------------------------------------ Seite: Erstellen
    def _page_create(self, page) -> None:
        self.header(page, "Neue Highlights", "VOD-Links oder Videodateien rein – fertiges Video und Clips raus.")
        body = ctk.CTkScrollableFrame(page, fg_color=BG)
        body.pack(fill="both", expand=True, padx=20, pady=(0, 16))

        src_card = card(body)
        src_card.pack(fill="x", padx=12, pady=8)
        ctk.CTkLabel(src_card, text="Quellen", font=font(16, "bold")).pack(anchor="w", padx=20, pady=(16, 0))
        ctk.CTkLabel(src_card, text="Ein VOD = normale Highlights.  Mehrere VODs = gemeinsamer Stream, "
                     "die Reaktionen aller Streamer werden im Flow gezeigt.", font=font(12), text_color=MUTED,
                     wraplength=760, justify="left").pack(anchor="w", padx=20)
        self.src_list = ctk.CTkFrame(src_card, fg_color="transparent")
        self.src_list.pack(fill="x", padx=16, pady=8)
        self.src_rows: list[tuple[ctk.CTkFrame, ctk.CTkEntry]] = []
        self._add_source_row()
        row = ctk.CTkFrame(src_card, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=(0, 16))
        ghost_button(row, "+ Weiterer Link", lambda: self._add_source_row(), width=150).pack(side="left")
        ghost_button(row, "+ Datei(en) wählen", self._pick_files, width=150).pack(side="left", padx=8)

        opt = card(body)
        opt.pack(fill="x", padx=12, pady=8)
        ctk.CTkLabel(opt, text="Einstellungen", font=font(16, "bold")).pack(anchor="w", padx=20, pady=(16, 6))
        grid = ctk.CTkFrame(opt, fg_color="transparent")
        grid.pack(fill="x", padx=20, pady=(0, 16))
        grid.grid_columnconfigure(1, weight=1)

        def seg(r, label, values, default):
            ctk.CTkLabel(grid, text=label, font=font(13), text_color="#DADAE8").grid(row=r, column=0, sticky="w", pady=6, padx=(0, 20))
            var = ctk.StringVar(value=default)
            ctk.CTkSegmentedButton(grid, values=values, variable=var, selected_color=ACCENT,
                                   selected_hover_color=ACCENT_HOVER, font=font(12)).grid(row=r, column=1, sticky="w")
            return var

        self.opt_minutes = seg(0, "Länge Highlight-Video", ["5 Min", "10 Min", "12 Min", "15 Min"], "12 Min")
        self.opt_focus = seg(1, "Worauf achten?", ["Lachen", "Alles Laute"], "Lachen")
        self.opt_layout = seg(2, "Bei mehreren VODs", ["POV-Wechsel", "Splitscreen"], "POV-Wechsel")
        self.opt_transitions = ctk.BooleanVar(value=True)
        self.opt_vertical = ctk.BooleanVar(value=True)
        ctk.CTkSwitch(grid, text="Weiche Übergänge zwischen den Clips", variable=self.opt_transitions,
                      progress_color=ACCENT, font=font(13)).grid(row=3, column=0, columnspan=2, sticky="w", pady=6)
        ctk.CTkSwitch(grid, text="Hochformat-Clips für TikTok & Shorts", variable=self.opt_vertical,
                      progress_color=ACCENT, font=font(13)).grid(row=4, column=0, columnspan=2, sticky="w", pady=6)

        self.start_btn = accent_button(body, "▶   Highlights erstellen", self.start, height=52)
        self.start_btn.configure(font=font(16, "bold"))
        self.start_btn.pack(fill="x", padx=12, pady=10)

        prog = card(body)
        prog.pack(fill="x", padx=12, pady=8)
        self.status = ctk.CTkLabel(prog, text="Bereit.", font=font(13), anchor="w")
        self.status.pack(fill="x", padx=20, pady=(14, 6))
        self.bar = ctk.CTkProgressBar(prog, progress_color=ACCENT, height=10)
        self.bar.set(0)
        self.bar.pack(fill="x", padx=20)
        self.log = ctk.CTkTextbox(prog, height=110, font=ctk.CTkFont(family="Consolas", size=11),
                                  fg_color="#15151C", text_color="#B8B8CC")
        self.log.pack(fill="x", padx=20, pady=14)
        self.log.configure(state="disabled")

    def _add_source_row(self, value: str = "") -> None:
        if len(self.src_rows) == 1 and not self.src_rows[0][1].get().strip() and value:
            self.src_rows[0][1].insert(0, value)
            return
        row = ctk.CTkFrame(self.src_list, fg_color="transparent")
        row.pack(fill="x", pady=3)
        # ohne textvariable, sonst zeigt customtkinter den Platzhalter nicht an
        entry = ctk.CTkEntry(row, height=40, corner_radius=10, font=font(13), border_color="#34344A",
                             placeholder_text="https://www.twitch.tv/videos/…  oder  https://youtu.be/…  oder Datei")
        entry.pack(side="left", fill="x", expand=True, padx=4)
        if value:
            entry.insert(0, value)
        ctk.CTkButton(row, text="✕", width=40, height=40, corner_radius=10, fg_color="#2A2A38", hover_color="#3A2A38",
                      command=lambda r=row: self._remove_source_row(r)).pack(side="left", padx=4)
        self.src_rows.append((row, entry))

    def _remove_source_row(self, row) -> None:
        if len(self.src_rows) == 1:
            self.src_rows[0][1].delete(0, "end")
            return
        self.src_rows = [(r, v) for r, v in self.src_rows if r is not row]
        row.destroy()

    def _pick_files(self) -> None:
        for path in filedialog.askopenfilenames(title="Videos auswählen", filetypes=VIDEO_TYPES):
            self._add_source_row(path)

    def write(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")
        logging.info(text)

    def start(self) -> None:
        from .auto import LAYOUT_POV, LAYOUT_SPLIT, Options
        from .detect import FOCUS_LAUGH, FOCUS_LOUD

        if self.busy:
            return
        sources = [v.get().strip() for _, v in self.src_rows if v.get().strip()]
        if not sources:
            messagebox.showinfo("Publisher", "Bitte mindestens einen VOD-Link oder eine Datei angeben.")
            return
        opts = Options(
            minutes=float(self.opt_minutes.get().split()[0]),
            focus=FOCUS_LAUGH if self.opt_focus.get() == "Lachen" else FOCUS_LOUD,
            layout=LAYOUT_SPLIT if self.opt_layout.get() == "Splitscreen" else LAYOUT_POV,
            transitions=self.opt_transitions.get(),
            vertical=self.opt_vertical.get(),
        )
        self.busy = True
        self.start_btn.configure(state="disabled", text="Läuft …")
        self.bar.set(0)
        self.write(f"Start mit {len(sources)} Quelle(n)")

        def job():
            from .auto import make_highlights

            return make_highlights(sources, self.settings, options=opts, output_root=self.output_root,
                                   progress=lambda m, f: self.events.put(("progress", m, f)))

        self.run_bg(job, self._finished, self._failed)

    def _on_progress(self, msg: str, frac: float) -> None:
        if msg != self.status.cget("text") and not msg.startswith(("Analysiere", "Lade", "VOD")):
            self.write(msg)
        self.status.configure(text=msg)
        self.bar.set(max(0.0, min(1.0, frac)))

    def _finished(self, result) -> None:
        self.busy = False
        self.start_btn.configure(state="normal", text="▶   Highlights erstellen")
        self.bar.set(1)
        self.status.configure(text=f"Fertig: {len(result.clips)} Clips + Highlight-Video", text_color=OK)
        for w in result.warnings:
            if "Versatz" in w:
                self.write(w)
        self.write(f"Gespeichert in {result.folder}")
        self._maybe_apply_update()
        if messagebox.askyesno("Fertig", f"{len(result.clips)} Highlights erstellt.\n\nJetzt in der Videobox ansehen?"):
            self.show("videobox")

    def _failed(self, exc: Exception) -> None:
        self.busy = False
        self.start_btn.configure(state="normal", text="▶   Highlights erstellen")
        self.status.configure(text="Fehler – Details im Protokoll.", text_color=BAD)
        self.write(f"FEHLER: {exc}")
        self._maybe_apply_update()
        self._show_error(exc)

    # ------------------------------------------------------------ Seite: Videobox
    def _page_videobox(self, page) -> None:
        self.header(page, "Videobox", "Alle fertigen Videos. Direkt zu YouTube oder TikTok hochladen.")
        self.box = ctk.CTkScrollableFrame(page, fg_color=BG)
        self.box.pack(fill="both", expand=True, padx=20, pady=(0, 16))
        self._thumbs: list = []

    def refresh_videobox(self) -> None:
        from PIL import Image

        from . import videobox

        for w in self.box.winfo_children():
            w.destroy()
        self._thumbs.clear()
        items = videobox.list_items(self.output_root)
        if not items:
            ctk.CTkLabel(self.box, text="Noch keine Videos – leg unter „Erstellen“ los.", font=font(14),
                         text_color=MUTED).pack(pady=40)
            return
        for item in items:
            c = card(self.box)
            c.pack(fill="x", padx=12, pady=8)
            c.grid_columnconfigure(1, weight=1)
            try:
                img = Image.open(videobox.thumbnail(item.video.path))
                thumb = ctk.CTkImage(light_image=img, dark_image=img, size=(208, int(208 * img.height / img.width)))
                self._thumbs.append(thumb)
                ctk.CTkLabel(c, image=thumb, text="").grid(row=0, column=0, rowspan=3, padx=16, pady=16, sticky="n")
            except Exception:  # noqa: BLE001 – ohne Vorschaubild weitermachen
                ctk.CTkLabel(c, text="▶", width=208, font=font(40)).grid(row=0, column=0, rowspan=3, padx=16, pady=16)
            ctk.CTkLabel(c, text=item.title, font=font(16, "bold"), anchor="w", wraplength=560,
                         justify="left").grid(row=0, column=1, sticky="w", pady=(16, 0))
            n_clips = sum(1 for f in item.files if f.kind == "clip")
            n_vert = sum(1 for f in item.files if f.kind == "vertical")
            minutes = int(item.info.get("duration", 0) // 60)
            meta = f"{item.created}  ·  {minutes} Min  ·  {n_clips} Clips" + (f"  ·  {n_vert} Hochformat" if n_vert else "")
            ctk.CTkLabel(c, text=meta, font=font(12), text_color=MUTED).grid(row=1, column=1, sticky="w")
            badges = ctk.CTkFrame(c, fg_color="transparent")
            badges.grid(row=2, column=1, sticky="w", pady=(4, 0))
            for u in item.info.get("uploads", [])[-4:]:
                txt = f"{'YouTube' if u['platform'] == 'youtube' else 'TikTok'} · {u.get('privacy', '')} · {u.get('kind', '')}"
                ctk.CTkLabel(badges, text=txt, font=font(11), fg_color="#24243A", corner_radius=8,
                             text_color="#C9C9F5").pack(side="left", padx=(0, 6), ipadx=6)
            actions = ctk.CTkFrame(c, fg_color="transparent")
            actions.grid(row=0, column=2, rowspan=3, padx=16)
            accent_button(actions, "⇪  Hochladen", lambda it=item: UploadDialog(self, it), width=150).pack(pady=3)
            ghost_button(actions, "▶  Abspielen", lambda it=item: open_path(it.video.path), width=150).pack(pady=3)
            ghost_button(actions, "Ordner", lambda it=item: open_path(it.folder), width=150).pack(pady=3)

    # ------------------------------------------------------------ Seite: Verbindungen
    def _page_connections(self, page) -> None:
        from . import tiktok

        self.header(page, "Verbindungen", "Einmal verbinden – danach lädst du direkt aus der Videobox hoch.")
        body = ctk.CTkScrollableFrame(page, fg_color=BG)
        body.pack(fill="both", expand=True, padx=20, pady=(0, 16))

        yt = card(body)
        yt.pack(fill="x", padx=12, pady=8)
        top = ctk.CTkFrame(yt, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(16, 4))
        ctk.CTkLabel(top, text="▶  YouTube", font=font(18, "bold"), text_color="#FF4E45").pack(side="left")
        self.yt_status = ctk.CTkLabel(top, text="", font=font(12))
        self.yt_status.pack(side="left", padx=14)
        ctk.CTkLabel(yt, text=(
            "Einmalig: In der Google Cloud Console ein Projekt anlegen, „YouTube Data API v3“ aktivieren und "
            "einen OAuth-Client vom Typ „Desktop-App“ erstellen. Die heruntergeladene JSON-Datei hier auswählen, "
            "dann im Browser mit deinem Kanal anmelden."), font=font(12), text_color=MUTED, wraplength=780,
            justify="left").pack(anchor="w", padx=20)
        btns = ctk.CTkFrame(yt, fg_color="transparent")
        btns.pack(fill="x", padx=20, pady=14)
        accent_button(btns, "Verbinden", self._connect_youtube, width=140).pack(side="left")
        ghost_button(btns, "Trennen", self._disconnect_youtube, width=110).pack(side="left", padx=8)

        tt = card(body)
        tt.pack(fill="x", padx=12, pady=8)
        top = ctk.CTkFrame(tt, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(16, 4))
        ctk.CTkLabel(top, text="♪  TikTok", font=font(18, "bold"), text_color="#25F4EE").pack(side="left")
        self.tt_status = ctk.CTkLabel(top, text="", font=font(12))
        self.tt_status.pack(side="left", padx=14)
        redirect = tiktok.redirect_uri(self.settings)
        ctk.CTkLabel(tt, text=(
            "Einmalig: Auf developers.tiktok.com eine App anlegen, „Login Kit“ und „Content Posting API“ "
            f"(Direct Post) hinzufügen. Als Desktop-Redirect-URI eintragen:  {redirect}\n"
            "Hinweis: Bis TikTok die App geprüft hat, sind Posts nur privat („Nur ich“) möglich."),
            font=font(12), text_color=MUTED, wraplength=780, justify="left").pack(anchor="w", padx=20)
        form = ctk.CTkFrame(tt, fg_color="transparent")
        form.pack(fill="x", padx=20, pady=(10, 0))
        creds = tiktok.app_credentials(self.settings) or ("", "")
        self.tt_key = ctk.CTkEntry(form, placeholder_text="Client Key", width=280, height=36)
        self.tt_key.pack(side="left")
        self.tt_secret = ctk.CTkEntry(form, placeholder_text="Client Secret", width=280, height=36)
        self.tt_secret.pack(side="left", padx=8)
        if creds[0]:
            self.tt_key.insert(0, creds[0])
            self.tt_secret.configure(show="•")
            self.tt_secret.insert(0, creds[1])
        ghost_button(form, "Redirect-URI kopieren", lambda: (self.clipboard_clear(), self.clipboard_append(redirect)),
                     width=170).pack(side="left")
        btns = ctk.CTkFrame(tt, fg_color="transparent")
        btns.pack(fill="x", padx=20, pady=14)
        accent_button(btns, "Verbinden", self._connect_tiktok, width=140).pack(side="left")
        ghost_button(btns, "Trennen", self._disconnect_tiktok, width=110).pack(side="left", padx=8)

    def refresh_connections(self) -> None:
        from . import tiktok, youtube

        self.yt_status.configure(text="● verbunden" if youtube.is_connected(self.settings) else "○ nicht verbunden",
                                 text_color=OK if youtube.is_connected(self.settings) else MUTED)
        self.tt_status.configure(text="● verbunden" if tiktok.is_connected(self.settings) else "○ nicht verbunden",
                                 text_color=OK if tiktok.is_connected(self.settings) else MUTED)

    def _connect_youtube(self) -> None:
        from . import youtube

        secret = None
        if not youtube.client_secret_path(self.settings).exists():
            secret = filedialog.askopenfilename(title="OAuth-Client-Datei (client_secret….json) auswählen",
                                                filetypes=[("JSON", "*.json")])
            if not secret:
                return
        self.yt_status.configure(text="Browser-Anmeldung läuft …", text_color=WARN)
        self.run_bg(lambda: youtube.connect(self.settings, secret),
                    lambda name: (self.refresh_connections(), self.yt_status.configure(text=f"● verbunden: {name}")),
                    lambda e: (self.refresh_connections(), self._show_error(e)))

    def _disconnect_youtube(self) -> None:
        from . import youtube

        youtube.disconnect(self.settings)
        self.refresh_connections()

    def _connect_tiktok(self) -> None:
        from . import tiktok

        if not self.tt_key.get().strip() or not self.tt_secret.get().strip():
            messagebox.showinfo("TikTok", "Bitte Client Key und Client Secret deiner TikTok-App eintragen.")
            return
        tiktok.save_app_credentials(self.settings, self.tt_key.get(), self.tt_secret.get())
        self.tt_status.configure(text="Browser-Anmeldung läuft …", text_color=WARN)
        self.run_bg(lambda: tiktok.connect(self.settings),
                    lambda name: (self.refresh_connections(), self.tt_status.configure(text=f"● verbunden: {name}")),
                    lambda e: (self.refresh_connections(), self._show_error(e)))

    def _disconnect_tiktok(self) -> None:
        from . import tiktok

        tiktok.disconnect(self.settings)
        self.refresh_connections()

    # ------------------------------------------------------------ Seite: Freigaben
    def _page_permissions(self, page) -> None:
        self.header(page, "Freigaben", "Nur Clips von Creatorn mit „erlaubt“ (oder eigene VODs) dürfen hochgeladen werden.")
        self.perm = ctk.CTkScrollableFrame(page, fg_color=BG)
        self.perm.pack(fill="both", expand=True, padx=20, pady=(0, 16))

    def refresh_permissions(self) -> None:
        from .creators import PERMISSION_STATUSES, CreatorRegistry

        for w in self.perm.winfo_children():
            w.destroy()
        reg = CreatorRegistry.load(self.settings.creators_file)
        if not len(reg):
            ctk.CTkLabel(self.perm, text="Noch keine fremden Creator – sie erscheinen hier automatisch nach dem ersten Import.",
                         font=font(13), text_color=MUTED).pack(pady=40)
            return
        colors = {"erlaubt": OK, "angefragt": WARN, "nicht erlaubt": BAD}
        for creator in reg:
            c = card(self.perm)
            c.pack(fill="x", padx=12, pady=6)
            c.grid_columnconfigure(1, weight=1)
            ctk.CTkLabel(c, text=creator.name, font=font(15, "bold")).grid(row=0, column=0, sticky="w", padx=20, pady=(14, 0))
            link = creator.twitch_channel_url or creator.youtube_channel_url
            ctk.CTkLabel(c, text=link or "–", font=font(11), text_color=MUTED).grid(row=1, column=0, sticky="w", padx=20)
            var = ctk.StringVar(value=creator.permission_status)
            proof = ctk.StringVar(value=creator.permission_proof)
            seg = ctk.CTkSegmentedButton(c, values=list(PERMISSION_STATUSES), variable=var, font=font(12),
                                         selected_color=colors[creator.permission_status])
            seg.grid(row=0, column=2, rowspan=2, padx=16)
            ctk.CTkEntry(c, textvariable=proof, placeholder_text="Nachweis (Link, Mail, Clip-Richtlinie …)",
                         height=32).grid(row=2, column=0, columnspan=3, sticky="ew", padx=20, pady=(8, 14))

            def save(_=None, cid=creator.id, v=var, p=proof, s=seg):
                r = CreatorRegistry.load(self.settings.creators_file)
                cr = r.get(cid)
                cr.permission_status, cr.permission_proof = v.get(), p.get()
                r.save()
                s.configure(selected_color=colors[v.get()])

            seg.configure(command=save)
            proof.trace_add("write", lambda *_a, f=save: f())

    # ------------------------------------------------------------ Auto-Update
    def _update_worker(self) -> None:
        try:
            update = updater.check_for_update()
            if not update:
                self.events.put(("update_info", f"Version {__version__} · aktuell"))
                return
            self.events.put(("update_info", f"Lade Update {update.version} …"))
            self.events.put(("update_ready", updater.download_update(update), update.version))
        except Exception as exc:  # noqa: BLE001 – Update-Probleme dürfen die App nie blockieren
            logging.warning("Update fehlgeschlagen: %s", exc)
            self.events.put(("update_info", f"Version {__version__}"))

    def _maybe_apply_update(self) -> None:
        if self.pending_update and not self.busy:
            self.update_label.configure(text="Installiere Update …")
            self.update_idletasks()
            updater.apply_update_and_restart(self.pending_update)
            self.after(800, self.destroy)


class UploadDialog(ctk.CTkToplevel):
    """Datei wählen, Plattform wählen, Texte prüfen, hochladen."""

    def __init__(self, app: App, item):
        super().__init__(app)
        from . import tiktok, youtube

        self.app, self.item = app, item
        self.title("Hochladen")
        self.geometry("720x660")
        self.configure(fg_color=BG)
        self.transient(app)
        self.after(50, self.grab_set)

        ctk.CTkLabel(self, text="Hochladen", font=font(22, "bold")).pack(anchor="w", padx=24, pady=(20, 4))
        ctk.CTkLabel(self, text=item.title, font=font(12), text_color=MUTED).pack(anchor="w", padx=24)

        self.files = {f"{f.label}: {f.title}": f for f in item.files}
        self.file_var = ctk.StringVar(value=next(iter(self.files)))
        ctk.CTkOptionMenu(self, values=list(self.files), variable=self.file_var, command=lambda _v: self._fill(),
                          fg_color=CARD, button_color=ACCENT, button_hover_color=ACCENT_HOVER,
                          width=660, dynamic_resizing=False).pack(padx=24, pady=(14, 8))

        self.platform = ctk.StringVar(value="YouTube" if youtube.is_connected(app.settings) or not tiktok.is_connected(app.settings) else "TikTok")
        ctk.CTkSegmentedButton(self, values=["YouTube", "TikTok"], variable=self.platform, selected_color=ACCENT,
                               selected_hover_color=ACCENT_HOVER, command=lambda _v: self._fill()).pack(anchor="w", padx=24)
        self.conn = ctk.CTkLabel(self, text="", font=font(12))
        self.conn.pack(anchor="w", padx=24, pady=(4, 0))

        ctk.CTkLabel(self, text="Titel / Bildunterschrift", font=font(12), text_color=MUTED).pack(anchor="w", padx=24, pady=(10, 0))
        self.title_var = ctk.StringVar()
        ctk.CTkEntry(self, textvariable=self.title_var, height=36).pack(fill="x", padx=24)
        ctk.CTkLabel(self, text="Beschreibung (mit Credits)", font=font(12), text_color=MUTED).pack(anchor="w", padx=24, pady=(10, 0))
        self.desc = ctk.CTkTextbox(self, height=170, fg_color="#15151C")
        self.desc.pack(fill="x", padx=24)

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=24, pady=10)
        ctk.CTkLabel(row, text="Sichtbarkeit", font=font(12), text_color=MUTED).pack(side="left")
        self.privacy = ctk.StringVar(value="Privat")
        self.privacy_menu = ctk.CTkOptionMenu(row, values=list(YT_PRIVACY), variable=self.privacy, fg_color=CARD,
                                              button_color=ACCENT, button_hover_color=ACCENT_HOVER)
        self.privacy_menu.pack(side="left", padx=10)

        self.blocked = ctk.CTkLabel(self, text="", font=font(12), text_color=BAD, wraplength=660, justify="left")
        self.blocked.pack(anchor="w", padx=24)
        self.bar = ctk.CTkProgressBar(self, progress_color=ACCENT)
        self.bar.set(0)
        self.bar.pack(fill="x", padx=24, pady=8)
        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(fill="x", padx=24, pady=(0, 18))
        self.go = accent_button(btns, "⇪  Jetzt hochladen", self._upload, width=200, height=42)
        self.go.pack(side="right")
        ghost_button(btns, "Freigaben öffnen", lambda: (self.destroy(), app.show("freigaben")), width=150).pack(side="left")
        self._tt_options: list[str] | None = None
        self._fill()

    @property
    def media(self):
        return self.files[self.file_var.get()]

    def _fill(self) -> None:
        from . import tiktok, videobox, youtube

        platform = "youtube" if self.platform.get() == "YouTube" else "tiktok"
        connected = youtube.is_connected(self.app.settings) if platform == "youtube" else tiktok.is_connected(self.app.settings)
        self.conn.configure(text="● verbunden" if connected else "○ nicht verbunden – erst unter „Verbindungen“ verbinden",
                            text_color=OK if connected else WARN)
        try:
            title, desc = videobox.default_texts(self.item, self.media, self.app.settings, platform)
            blockers = videobox.blockers_for(self.item, self.media, self.app.settings)
        except Exception as exc:  # noqa: BLE001
            title, desc, blockers = self.media.title, "", []
            logging.warning("Texte konnten nicht erzeugt werden: %s", exc)
        self.title_var.set(title)
        self.desc.configure(state="normal")
        self.desc.delete("1.0", "end")
        self.desc.insert("1.0", desc if platform == "youtube" else "(TikTok nutzt nur die Bildunterschrift oben – Credits stehen darin.)")
        self.desc.configure(state="normal" if platform == "youtube" else "disabled")
        if platform == "youtube":
            self.privacy_menu.configure(values=list(YT_PRIVACY))
            self.privacy.set("Privat")
        else:
            options = self._tt_options or list(TT_PRIVACY)
            self.privacy_menu.configure(values=[TT_PRIVACY.get(o, o) for o in options])
            self.privacy.set(TT_PRIVACY["SELF_ONLY"])
            if connected and self._tt_options is None:
                self.app.run_bg(lambda: tiktok.creator_info(self.app.settings).get("privacy_level_options") or ["SELF_ONLY"],
                                self._set_tt_options, lambda e: None)
        if blockers:
            names = ", ".join(sorted({b.creator_name for b in blockers}))
            self.blocked.configure(text=f"Gesperrt: keine Freigabe von {names}. Unter „Freigaben“ auf „erlaubt“ setzen.")
            self.go.configure(state="disabled")
        else:
            self.blocked.configure(text="")
            self.go.configure(state="normal" if connected else "disabled")

    def _set_tt_options(self, options: list[str]) -> None:
        self._tt_options = options
        if self.winfo_exists() and self.platform.get() == "TikTok":
            self.privacy_menu.configure(values=[TT_PRIVACY.get(o, o) for o in options])

    def _upload(self) -> None:
        from . import videobox

        platform = self.platform.get()
        privacy_label = self.privacy.get()
        if privacy_label in ("Öffentlich",) and not messagebox.askyesno(
            "Öffentlich veröffentlichen?", "Das Video wird sofort öffentlich sichtbar. Fortfahren?", parent=self
        ):
            return
        self.go.configure(state="disabled", text="Lädt hoch …")
        title, media, item, settings = self.title_var.get(), self.media, self.item, self.app.settings
        desc = self.desc.get("1.0", "end").strip()
        prog = lambda f: self.app.events.put(("call", lambda v: self.winfo_exists() and self.bar.set(v), f))  # noqa: E731

        def job():
            if platform == "YouTube":
                return videobox.upload_youtube(item, media, settings, title=title, description=desc,
                                               privacy=YT_PRIVACY[privacy_label], progress=prog)
            code = next(k for k, v in TT_PRIVACY.items() if v == privacy_label)
            return videobox.upload_tiktok(item, media, settings, title=title, privacy_level=code, progress=prog)

        def done(entry):
            link = entry.get("url") or "TikTok (in der App sichtbar)"
            messagebox.showinfo("Hochgeladen", f"Fertig!\n{link}", parent=self.app)
            self.destroy()
            self.app.refresh_videobox()

        def fail(exc):
            if self.winfo_exists():
                self.go.configure(state="normal", text="⇪  Jetzt hochladen")
            self.app._show_error(exc)

        self.app.run_bg(job, done, fail)


def main() -> None:
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    App().mainloop()


if __name__ == "__main__":
    main()
