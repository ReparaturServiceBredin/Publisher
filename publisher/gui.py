"""Einfaches Fenster: Link oder Datei rein, „Highlights erstellen“ klicken, fertig."""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import updater
from ._version import __version__
from .config import load_app_settings

VIDEO_TYPES = [("Videos", "*.mp4 *.mkv *.mov *.flv *.ts *.webm *.avi *.m4v"), ("Alle Dateien", "*.*")]


def open_folder(path: Path) -> None:
    if os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.settings = load_app_settings()
        self.output_root = self.settings.base_dir / "Fertig"
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.events: queue.Queue = queue.Queue()
        self.busy = False
        self.pending_update: Path | None = None
        self.last_folder: Path | None = None
        logging.basicConfig(
            filename=self.settings.base_dir / "publisher.log",
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
        )

        root.title(f"Publisher {__version__} – Highlights automatisch")
        root.geometry("720x480")
        root.minsize(560, 420)
        pad = {"padx": 14, "pady": 6}

        ttk.Label(root, text="VOD-Link einfügen oder Video-Datei auswählen", font=("Segoe UI", 13, "bold")).pack(
            anchor="w", **pad
        )
        row = ttk.Frame(root)
        row.pack(fill="x", **pad)
        self.source = tk.StringVar()
        entry = ttk.Entry(row, textvariable=self.source, font=("Segoe UI", 11))
        entry.pack(side="left", fill="x", expand=True, ipady=4)
        entry.bind("<Return>", lambda _e: self.start())
        ttk.Button(row, text="Datei…", command=self.pick_file).pack(side="left", padx=(8, 0))
        ttk.Label(
            root,
            text="z. B. https://www.twitch.tv/videos/123456789 oder https://youtu.be/… oder eine .mp4 vom PC",
            foreground="#666",
        ).pack(anchor="w", padx=14)

        opts = ttk.Frame(root)
        opts.pack(fill="x", **pad)
        ttk.Label(opts, text="Länge Highlight-Video:").pack(side="left")
        self.minutes = tk.StringVar(value="12 Minuten")
        ttk.Combobox(
            opts, textvariable=self.minutes, width=12, state="readonly",
            values=["5 Minuten", "10 Minuten", "12 Minuten", "15 Minuten"],
        ).pack(side="left", padx=8)

        self.start_btn = ttk.Button(root, text="▶  Highlights erstellen", command=self.start)
        self.start_btn.pack(fill="x", padx=14, pady=10, ipady=8)

        self.status = tk.StringVar(value="Bereit.")
        ttk.Label(root, textvariable=self.status).pack(anchor="w", padx=14)
        self.bar = ttk.Progressbar(root, maximum=1.0)
        self.bar.pack(fill="x", **pad)

        self.log = tk.Text(root, height=8, state="disabled", wrap="word", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True, padx=14, pady=(0, 6))

        bottom = ttk.Frame(root)
        bottom.pack(fill="x", padx=14, pady=(0, 12))
        self.open_btn = ttk.Button(bottom, text="Ergebnis öffnen", command=self.open_result, state="disabled")
        self.open_btn.pack(side="left")
        ttk.Button(bottom, text="Ausgabeordner", command=lambda: open_folder(self.output_root)).pack(side="left", padx=8)
        ttk.Label(bottom, text=str(self.output_root), foreground="#666").pack(side="left")

        self.root.after(100, self.poll)
        if updater.can_self_update():
            threading.Thread(target=self.update_worker, daemon=True).start()

    # ---------- UI-Helfer ----------
    def write(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")
        logging.info(text)

    def pick_file(self) -> None:
        path = filedialog.askopenfilename(title="Video auswählen", filetypes=VIDEO_TYPES)
        if path:
            self.source.set(path)

    def open_result(self) -> None:
        if self.last_folder:
            open_folder(self.last_folder)

    # ---------- Ablauf ----------
    def start(self) -> None:
        source = self.source.get().strip()
        if self.busy:
            return
        if not source:
            messagebox.showinfo("Publisher", "Bitte einen VOD-Link einfügen oder eine Datei auswählen.")
            return
        minutes = float(self.minutes.get().split()[0])
        self.busy = True
        self.start_btn.configure(state="disabled")
        self.open_btn.configure(state="disabled")
        self.bar["value"] = 0
        self.write(f"Start: {source}")
        threading.Thread(target=self.job, args=(source, minutes), daemon=True).start()

    def job(self, source: str, minutes: float) -> None:
        from .auto import make_highlights

        def progress(msg: str, frac: float) -> None:
            self.events.put(("progress", msg, frac))

        try:
            result = make_highlights(
                source, self.settings, minutes=minutes, progress=progress, output_root=self.output_root
            )
            self.events.put(("done", result))
        except Exception as exc:
            logging.error(traceback.format_exc())
            self.events.put(("error", str(exc)))

    def poll(self) -> None:
        last_msg = None
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "progress":
                    _, msg, frac = event
                    self.status.set(msg)
                    self.bar["value"] = frac
                    if msg != last_msg and not msg.startswith(("Analysiere", "Lade")):
                        self.write(msg)
                    last_msg = msg
                elif kind == "done":
                    self.finish(event[1])
                elif kind == "error":
                    self.fail(event[1])
                elif kind == "update_ready":
                    self.pending_update = event[1]
                    self.write(f"Update {event[2]} geladen – wird installiert, sobald nichts läuft.")
                    self.maybe_apply_update()
                elif kind == "info":
                    self.write(event[1])
        except queue.Empty:
            pass
        self.root.after(150, self.poll)

    def finish(self, result) -> None:
        self.busy = False
        self.start_btn.configure(state="normal")
        self.bar["value"] = 1.0
        self.last_folder = result.folder
        self.open_btn.configure(state="normal")
        self.status.set(f"Fertig: {len(result.clips)} Clips + Highlight-Video")
        self.write(f"Fertig! Ordner: {result.folder}")
        self.source.set("")
        self.maybe_apply_update()
        if messagebox.askyesno("Fertig", f"{len(result.clips)} Highlights gefunden.\n\nOrdner jetzt öffnen?"):
            open_folder(result.folder)

    def fail(self, message: str) -> None:
        self.busy = False
        self.start_btn.configure(state="normal")
        self.status.set("Fehler – Details im Protokoll.")
        self.write(f"FEHLER: {message}")
        self.maybe_apply_update()
        messagebox.showerror("Publisher", message[:1500])

    # ---------- Auto-Update ----------
    def update_worker(self) -> None:
        try:
            update = updater.check_for_update()
            if not update:
                return
            self.events.put(("info", f"Neue Version {update.version} gefunden – lade herunter…"))
            path = updater.download_update(update)
            self.events.put(("update_ready", path, update.version))
        except Exception as exc:  # Update-Probleme dürfen die App nie blockieren
            logging.warning("Update fehlgeschlagen: %s", exc)
            self.events.put(("info", f"Update nicht möglich: {exc}"))

    def maybe_apply_update(self) -> None:
        if self.pending_update and not self.busy:
            self.status.set("Installiere Update und starte neu…")
            self.root.update_idletasks()
            updater.apply_update_and_restart(self.pending_update)
            self.root.after(800, self.root.destroy)


def main() -> None:
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista" if os.name == "nt" else "clam")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
