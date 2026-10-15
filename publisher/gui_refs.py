"""Bereich „Referenzvideos“: fertige Videos hinzufügen, bewerten, analysieren – und das Erfolgsprofil."""

from __future__ import annotations

import logging
import threading
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk

from . import learn

# Farben/Helfer kommen aus gui.py (gleicher Look)
from .gui_style import ACCENT, ACCENT_HOVER, BAD, BG, CARD, MUTED, OK, WARN, accent_button, card, font, ghost_button, open_path

RATING_COLORS = {"gut": OK, "mittel": WARN, "schlecht": BAD}
STRENGTH_TEXT = {"deutlich": "deutlicher Unterschied", "leicht": "leichter Unterschied", "kein": "kein klarer Unterschied"}
GOOD_COLOR = "#22C55E"
WEAK_COLOR = "#6B6B80"


def parse_number(text: str) -> float | None:
    """„12.345“, „12,5“, „1,2k“, „3 Mio“ → Zahl. Leer → None. Ungültig → ValueError."""
    t = text.strip().lower().replace(" ", "")
    if not t:
        return None
    mult = 1.0
    for suffix, m in (("mio", 1e6), ("m", 1e6), ("k", 1e3), ("tsd", 1e3), ("%", 1.0)):
        if t.endswith(suffix):
            t, mult = t[: -len(suffix)], m
            break
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".")
    elif "," in t:
        t = t.replace(",", ".")
    elif t.count(".") == 1 and len(t.split(".")[1]) == 3 and mult == 1.0:
        t = t.replace(".", "")  # 12.345 = Tausenderpunkt
    value = float(t) * mult
    if value < 0:
        raise ValueError("negativ")
    return value


def fmt_number(value) -> str:
    if value in (None, ""):
        return ""
    v = float(value)
    if v >= 1e6:
        return f"{v / 1e6:.1f} Mio".replace(".", ",")
    if v >= 1e4:
        return f"{v / 1e3:.0f}k"
    return f"{v:g}".replace(".", ",")


def analysis_summary(a: dict) -> str:
    if not a:
        return ""
    parts = [
        f"{a.get('duration_s', 0):.0f} s",
        f"{a.get('cuts_per_min', 0):.0f} Cuts/Min",
        f"Hook {a.get('hook_s', 0):.1f} s".replace(".", ","),
    ]
    for key, label in (("has_subtitles", "Untertitel"), ("has_music", "Musik"), ("has_cta", "CTA"), ("vertical", "9:16")):
        if a.get(key):
            parts.append(label)
    return "  ·  ".join(parts)


class ReferencePage:
    """Baut die Seite in `page` und verwaltet die Bibliothek (Thread-sicher über `lock`)."""

    def __init__(self, app, page: ctk.CTkFrame):
        self.app = app
        self.library = learn.Library(app.settings.base_dir / "Referenzvideos")
        self.lock = threading.RLock()
        self.busy_ids: set[str] = set()
        self._thumbs: list = []
        self.profile = self._build_profile()

        app.header(page, "Referenzvideos", "Fertige Videos rein – das Tool lernt, was bei deinem Publikum ankommt.")
        switch = ctk.CTkFrame(page, fg_color="transparent")
        switch.pack(fill="x", padx=32, pady=(0, 6))
        self.view = ctk.StringVar(value="Videos")
        ctk.CTkSegmentedButton(switch, values=["Videos", "Erfolgsprofil"], variable=self.view, command=lambda _v: self.refresh(),
                               selected_color=ACCENT, selected_hover_color=ACCENT_HOVER, font=font(13)).pack(side="left")
        self.count_label = ctk.CTkLabel(switch, text="", font=font(12), text_color=MUTED)
        self.count_label.pack(side="left", padx=14)
        self.body = ctk.CTkScrollableFrame(page, fg_color=BG)
        self.body.pack(fill="both", expand=True, padx=20, pady=(0, 16))
        self._register_drop(page)

    # ------------------------------------------------------------ Drag & Drop
    def _register_drop(self, page) -> None:
        if not getattr(self.app, "dnd_enabled", False):
            return
        from tkinterdnd2 import DND_FILES

        for widget in (page, self.body, getattr(self.body, "_parent_canvas", None)):
            if widget is None:
                continue
            try:
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", lambda e: self.add_files(self.app.tk.splitlist(e.data)))
            except (tk.TclError, AttributeError):
                logging.info("Drag & Drop für %s nicht verfügbar", widget)

    # ------------------------------------------------------------ Daten
    def _build_profile(self) -> learn.Profile:
        with self.lock:
            return learn.build_profile(self.library)

    def profile_changed(self) -> None:
        self.profile = self._build_profile()
        self.app.on_profile_changed(self.profile)

    def add_files(self, paths) -> None:
        paths = [p for p in paths if p]
        if not paths:
            return
        self.app.write_status(f"Füge {len(paths)} Referenzvideo(s) hinzu …")

        def job():
            added, errors = [], []
            for p in paths:
                try:
                    with self.lock:
                        v = self.library.add(p)
                    added.append(v)
                except learn.UnsupportedVideo as exc:
                    errors.append(str(exc))
                except OSError as exc:
                    errors.append(f"„{p}“ konnte nicht kopiert werden: {exc}")
            self.app.events.put(("call", lambda _r: self.refresh(), None))
            for v in added:
                self.busy_ids.add(v.id)
                self.app.events.put(("call", lambda _r: self.refresh(), None))
                result = learn.run_analysis(self.library.path(v))  # ohne Sperre – UI bleibt bedienbar
                with self.lock:
                    self.library.apply_analysis(v, *result)
                self.busy_ids.discard(v.id)
                self.app.events.put(("call", lambda _r: (self.profile_changed(), self.refresh()), None))
            return added, errors

        def done(result):
            added, errors = result
            self.app.write_status(f"{len(added)} Referenzvideo(s) hinzugefügt und analysiert.")
            failed = [v for v in added if v.error]
            if errors or failed:
                lines = errors + [f"„{v.title}“: {v.error}" for v in failed]
                messagebox.showwarning("Nicht alle Videos übernommen", "\n\n".join(lines)[:1800])

        self.app.run_bg(job, done)

    def pick_files(self) -> None:
        exts = " ".join(f"*{e}" for e in sorted(learn.SUPPORTED_EXT))
        self.add_files(filedialog.askopenfilenames(title="Referenzvideos auswählen",
                                                   filetypes=[("Videos", exts), ("Alle Dateien", "*.*")]))

    def reanalyze(self, video: learn.RefVideo) -> None:
        self.busy_ids.add(video.id)
        self.refresh()

        def job():
            result = learn.run_analysis(self.library.path(video))
            with self.lock:
                self.library.apply_analysis(video, *result)
            return video

        def done(v):
            self.busy_ids.discard(v.id)
            self.profile_changed()
            self.refresh()
            if v.error:
                messagebox.showerror("Analyse", v.error)

        self.app.run_bg(job, done)

    def delete(self, video: learn.RefVideo) -> None:
        if not messagebox.askyesno("Löschen", f"„{video.title}“ aus den Referenzvideos löschen?\n"
                                   "Die Kopie in der Bibliothek wird entfernt, dein Original bleibt erhalten."):
            return
        with self.lock:
            self.library.remove(video)
        self.profile_changed()
        self.refresh()

    def set_excluded(self, video: learn.RefVideo, use: bool) -> None:
        with self.lock:
            self.library.update(video, excluded=not use)
        self.profile_changed()
        self.refresh_counts()

    # ------------------------------------------------------------ Anzeige
    def refresh_counts(self) -> None:
        with self.lock:
            total = len(self.library.videos)
            used = len(self.library.learning_set())
        self.count_label.configure(text=f"{total} Video(s) · {used} fürs Lernen genutzt")

    def refresh(self) -> None:
        for w in self.body.winfo_children():
            w.destroy()
        self._thumbs.clear()
        self.refresh_counts()
        if self.view.get() == "Erfolgsprofil":
            self._render_profile()
        else:
            self._render_videos()

    def _render_videos(self) -> None:
        from PIL import Image

        from . import videobox

        drop = ctk.CTkFrame(self.body, fg_color="#191926", corner_radius=14, border_width=2, border_color="#3B3360")
        drop.pack(fill="x", padx=12, pady=8)
        hint = "Videos hierher ziehen" if getattr(self.app, "dnd_enabled", False) else "Videos hinzufügen"
        ctk.CTkLabel(drop, text=f"⇩  {hint}", font=font(17, "bold")).pack(pady=(18, 2))
        ctk.CTkLabel(drop, text="Fertig geschnittene Videos (mp4, mov, mkv, webm …). Mehrere gleichzeitig möglich.",
                     font=font(12), text_color=MUTED).pack()
        accent_button(drop, "Dateien auswählen", self.pick_files, width=180).pack(pady=(10, 18))
        with self.lock:
            videos = list(reversed(self.library.videos))
            scores = learn.success_scores(self.library.learning_set())
        if not videos:
            ctk.CTkLabel(self.body, text="Noch keine Referenzvideos. Am besten 4+ Videos mit Ergebnis (gut/schlecht oder Zahlen).",
                         font=font(13), text_color=MUTED).pack(pady=24)
            return
        for v in videos:
            c = card(self.body)
            c.pack(fill="x", padx=12, pady=6)
            c.grid_columnconfigure(1, weight=1)
            try:
                img = Image.open(videobox.thumbnail(self.library.path(v), 240))
                th = ctk.CTkImage(light_image=img, dark_image=img,
                                  size=(160, int(160 * img.height / img.width)) if img.width >= img.height else (90, 160))
                self._thumbs.append(th)
                ctk.CTkLabel(c, image=th, text="").grid(row=0, column=0, rowspan=4, padx=14, pady=14, sticky="n")
            except Exception:  # noqa: BLE001
                ctk.CTkLabel(c, text="▶", width=160, font=font(30)).grid(row=0, column=0, rowspan=4, padx=14, pady=14)
            top = ctk.CTkFrame(c, fg_color="transparent")
            top.grid(row=0, column=1, sticky="w", pady=(14, 0))
            ctk.CTkLabel(top, text=v.title, font=font(15, "bold")).pack(side="left")
            for text, color in ((v.platform, "#C9C9F5"), (v.rating, RATING_COLORS.get(v.rating, MUTED))):
                if text:
                    ctk.CTkLabel(top, text=text, font=font(11), fg_color="#24243A", corner_radius=8,
                                 text_color=color).pack(side="left", padx=(8, 0), ipadx=6)
            if v.id in scores:
                ctk.CTkLabel(top, text=f"Erfolg {scores[v.id] * 100:.0f}/100", font=font(11), text_color=MUTED).pack(side="left", padx=8)
            units = {"retention_pct": " %", "avg_watch_s": " s"}
            metrics = "  ·  ".join(f"{learn.METRIC_FIELDS[k].split(' (')[0]} {fmt_number(val)}{units.get(k, '')}"
                                   for k, val in v.metrics.items() if val not in (None, ""))
            ctk.CTkLabel(c, text=metrics or "Keine Kennzahlen – über „Bearbeiten“ eintragen", font=font(12),
                         text_color="#DADAE8" if metrics else MUTED).grid(row=1, column=1, sticky="w")
            if v.id in self.busy_ids:
                status, color = "Analysiere …", WARN
            elif v.error:
                status, color = f"⚠ {v.error}", BAD
            else:
                status, color = analysis_summary(v.features()), MUTED
            ctk.CTkLabel(c, text=status, font=font(12), text_color=color, wraplength=520, justify="left").grid(
                row=2, column=1, sticky="w")
            use = ctk.BooleanVar(value=not v.excluded)
            ctk.CTkSwitch(c, text="Für Lernen nutzen", variable=use, progress_color=ACCENT, font=font(12),
                          command=lambda vid=v, var=use: self.set_excluded(vid, var.get())).grid(row=3, column=1, sticky="w", pady=(4, 12))
            actions = ctk.CTkFrame(c, fg_color="transparent")
            actions.grid(row=0, column=2, rowspan=4, padx=14)
            ghost_button(actions, "▶  Ansehen", lambda vid=v: open_path(self.library.path(vid)), width=140).pack(pady=2)
            accent_button(actions, "Bearbeiten", lambda vid=v: RefEditDialog(self.app, self, vid), width=140).pack(pady=2)
            ghost_button(actions, "Neu analysieren", lambda vid=v: self.reanalyze(vid), width=140).pack(pady=2)
            ctk.CTkButton(actions, text="Löschen", width=140, fg_color="transparent", border_width=1, border_color="#5A2A2A",
                          hover_color="#3A2020", text_color="#FF9A9A", corner_radius=10, font=font(12),
                          command=lambda vid=v: self.delete(vid)).pack(pady=2)

    def _render_profile(self) -> None:
        p = self.profile
        tiles = ctk.CTkFrame(self.body, fg_color="transparent")
        tiles.pack(fill="x", padx=12, pady=8)
        for i, (num, label) in enumerate(((p.n_total, "Videos im Lernset"), (p.n_scored, "mit Ergebnis"),
                                          (p.n_good, "gut laufend"), (p.n_weak, "schwach"))):
            t = card(tiles)
            t.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 8, 0))
            tiles.grid_columnconfigure(i, weight=1)
            ctk.CTkLabel(t, text=str(num), font=font(28, "bold"),
                         text_color=GOOD_COLOR if i == 2 else BAD if i == 3 else "white").pack(anchor="w", padx=18, pady=(12, 0))
            ctk.CTkLabel(t, text=label, font=font(12), text_color=MUTED).pack(anchor="w", padx=18, pady=(0, 12))
        if not p.ready:
            c = card(self.body)
            c.pack(fill="x", padx=12, pady=8)
            ctk.CTkLabel(c, text="Profil noch nicht verfügbar", font=font(16, "bold")).pack(anchor="w", padx=20, pady=(16, 4))
            ctk.CTkLabel(c, text=p.message, font=font(13), text_color=MUTED, wraplength=760, justify="left").pack(anchor="w", padx=20, pady=(0, 16))
            return
        rec = card(self.body)
        rec.pack(fill="x", padx=12, pady=8)
        ctk.CTkLabel(rec, text="Was deine erfolgreichen Videos ausmacht", font=font(16, "bold")).pack(anchor="w", padx=20, pady=(16, 6))
        for text in learn.recommendations(p, 5) or ["Noch keine klaren Unterschiede zwischen guten und schwachen Videos."]:
            ctk.CTkLabel(rec, text=f"✓  {text}", font=font(13), wraplength=760, justify="left").pack(anchor="w", padx=20, pady=2)
        ctk.CTkFrame(rec, height=12, fg_color="transparent").pack()
        legend = ctk.CTkFrame(self.body, fg_color="transparent")
        legend.pack(fill="x", padx=16, pady=(8, 0))
        for color, label in ((GOOD_COLOR, "gut laufende Videos"), (WEAK_COLOR, "schwache Videos")):
            ctk.CTkLabel(legend, text="■", text_color=color, font=font(14)).pack(side="left")
            ctk.CTkLabel(legend, text=label, font=font(12), text_color=MUTED).pack(side="left", padx=(4, 16))
        grid = ctk.CTkFrame(self.body, fg_color="transparent")
        grid.pack(fill="x", padx=12, pady=4)
        for col in range(2):
            grid.grid_columnconfigure(col, weight=1)
        for n, item in enumerate(p.items):
            self._feature_card(grid, item).grid(row=n // 2, column=n % 2, sticky="nsew", padx=(0, 8) if n % 2 == 0 else (0, 0), pady=6)

    def _feature_card(self, master, item: learn.ProfileItem) -> ctk.CTkFrame:
        c = card(master)
        f = item.feature
        color = {"deutlich": OK, "leicht": WARN, "kein": MUTED}[item.strength]
        ctk.CTkLabel(c, text=f.label, font=font(14, "bold")).pack(anchor="w", padx=16, pady=(12, 0))
        ctk.CTkLabel(c, text=STRENGTH_TEXT[item.strength], font=font(11), text_color=color).pack(anchor="w", padx=16)
        canvas = tk.Canvas(c, height=54, bg=CARD, highlightthickness=0)
        canvas.pack(fill="x", padx=16, pady=6)
        top = max(abs(item.good), abs(item.weak), 1e-9) if f.kind == "num" else 1.0

        def draw(_e=None):
            canvas.delete("all")
            w = max(canvas.winfo_width() - 110, 60)
            for row, (val, col) in enumerate(((item.good, GOOD_COLOR), (item.weak, WEAK_COLOR))):
                y = 6 + row * 24
                length = max(2, int(w * min(abs(val) / top, 1.0)))
                canvas.create_rectangle(0, y, length, y + 16, fill=col, width=0)
                canvas.create_text(length + 8, y + 8, text=item.fmt(val), anchor="w", fill="#DADAE8",
                                   font=("Segoe UI", 10))

        canvas.bind("<Configure>", draw)
        if item.strength != "kein":
            ctk.CTkLabel(c, text=item.advice().split(" – ")[0], font=font(12), text_color="#C9C9F5",
                         wraplength=380, justify="left").pack(anchor="w", padx=16, pady=(0, 12))
        else:
            ctk.CTkFrame(c, height=8, fg_color="transparent").pack()
        return c


class RefEditDialog(ctk.CTkToplevel):
    """Titel, Plattform, Bewertung, Kennzahlen, Notizen und manuelle Korrekturen der Analyse."""

    TRI = {"automatisch": None, "ja": True, "nein": False}

    def __init__(self, app, page: ReferencePage, video: learn.RefVideo):
        super().__init__(app)
        self.app, self.page, self.video = app, page, video
        self.title("Referenzvideo bearbeiten")
        self.geometry("720x720")
        self.configure(fg_color=BG)
        self.transient(app)
        self.after(50, self.grab_set)
        app.open_dialogs.add(self)
        self.protocol("WM_DELETE_WINDOW", self.cancel)

        body = ctk.CTkScrollableFrame(self, fg_color=BG)
        body.pack(fill="both", expand=True, padx=12, pady=(12, 0))
        ctk.CTkLabel(body, text="Referenzvideo bearbeiten", font=font(20, "bold")).pack(anchor="w", padx=12, pady=(6, 10))

        def label(text):
            ctk.CTkLabel(body, text=text, font=font(12), text_color=MUTED).pack(anchor="w", padx=12, pady=(8, 2))

        label("Titel")
        self.title_entry = ctk.CTkEntry(body, height=36)
        self.title_entry.insert(0, video.title)
        self.title_entry.pack(fill="x", padx=12)
        row = ctk.CTkFrame(body, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(10, 0))
        ctk.CTkLabel(row, text="Plattform", font=font(12), text_color=MUTED).pack(side="left")
        self.platform = ctk.StringVar(value=video.platform or "—")
        ctk.CTkOptionMenu(row, values=["—"] + learn.PLATFORMS, variable=self.platform, fg_color=CARD,
                          button_color=ACCENT, button_hover_color=ACCENT_HOVER).pack(side="left", padx=(8, 24))
        ctk.CTkLabel(row, text="Wie lief es?", font=font(12), text_color=MUTED).pack(side="left")
        self.rating = ctk.StringVar(value=video.rating or "keine Angabe")
        ctk.CTkSegmentedButton(row, values=["gut", "mittel", "schlecht", "keine Angabe"], variable=self.rating,
                               selected_color=ACCENT, selected_hover_color=ACCENT_HOVER).pack(side="left", padx=8)

        label("Kennzahlen (optional – alle Felder leer lassen ist ok)")
        grid = ctk.CTkFrame(body, fg_color="transparent")
        grid.pack(fill="x", padx=12)
        self.metric_entries: dict[str, ctk.CTkEntry] = {}
        for i, (key, text) in enumerate(learn.METRIC_FIELDS.items()):
            cell = ctk.CTkFrame(grid, fg_color="transparent")
            cell.grid(row=i // 3, column=i % 3, sticky="ew", padx=(0, 10), pady=4)
            grid.grid_columnconfigure(i % 3, weight=1)
            ctk.CTkLabel(cell, text=text, font=font(11), text_color=MUTED).pack(anchor="w")
            e = ctk.CTkEntry(cell, height=32, placeholder_text="z. B. 12.500" if key == "views" else "")
            val = video.metrics.get(key)
            if val not in (None, ""):
                e.insert(0, fmt_number(val) if key == "views" else f"{float(val):g}".replace(".", ","))
            e.pack(fill="x")
            self.metric_entries[key] = e

        label("Analyse korrigieren (falls das Tool etwas falsch erkannt hat)")
        fixes = ctk.CTkFrame(body, fg_color="transparent")
        fixes.pack(fill="x", padx=12)
        feats = video.analysis or {}
        self.override_vars: dict[str, ctk.StringVar] = {}
        for i, key in enumerate(k for k in learn.EDITABLE_OVERRIDES if k != "hook_s"):
            cell = ctk.CTkFrame(fixes, fg_color="transparent")
            cell.grid(row=i, column=0, sticky="w", pady=3)
            detected = "ja" if feats.get(key) else "nein"
            ctk.CTkLabel(cell, text=f"{learn.FEATURE_BY_KEY[key].label} (erkannt: {detected})", font=font(12),
                         width=300, anchor="w").pack(side="left")
            current = video.overrides.get(key)
            var = ctk.StringVar(value="automatisch" if current is None else "ja" if current else "nein")
            ctk.CTkOptionMenu(cell, values=list(self.TRI), variable=var, width=130, fg_color=CARD,
                              button_color=ACCENT, button_hover_color=ACCENT_HOVER).pack(side="left")
            self.override_vars[key] = var
        hook_row = ctk.CTkFrame(body, fg_color="transparent")
        hook_row.pack(fill="x", padx=12, pady=(6, 0))
        ctk.CTkLabel(hook_row, text=f"Hook in Sekunden (erkannt: {feats.get('hook_s', '–')})", font=font(12)).pack(side="left")
        self.hook_entry = ctk.CTkEntry(hook_row, width=90, placeholder_text="auto")
        if video.overrides.get("hook_s") is not None:
            self.hook_entry.insert(0, f"{video.overrides['hook_s']:g}".replace(".", ","))
        self.hook_entry.pack(side="left", padx=8)

        label("Notizen")
        self.notes = ctk.CTkTextbox(body, height=70, fg_color="#15151C")
        self.notes.insert("1.0", video.notes)
        self.notes.pack(fill="x", padx=12)

        self.error = ctk.CTkLabel(self, text="", font=font(12), text_color=BAD, wraplength=660, justify="left")
        self.error.pack(anchor="w", padx=24, pady=(6, 0))
        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(fill="x", padx=24, pady=14)
        accent_button(btns, "Speichern", self.save, width=160, height=40).pack(side="right")
        ghost_button(btns, "Abbrechen", self.cancel, width=120, height=40).pack(side="right", padx=8)
        self._initial = self._collect_raw()

    # ------------------------------------------------------------ Werte
    def _collect_raw(self) -> tuple:
        return (
            self.title_entry.get(), self.platform.get(), self.rating.get(),
            tuple(e.get() for e in self.metric_entries.values()),
            tuple(v.get() for v in self.override_vars.values()), self.hook_entry.get(),
            self.notes.get("1.0", "end").strip(),
        )

    def has_changes(self) -> bool:
        return self.winfo_exists() and self._collect_raw() != self._initial

    def _parse(self) -> dict:
        metrics, bad = {}, []
        for key, entry in self.metric_entries.items():
            try:
                value = parse_number(entry.get())
            except ValueError:
                bad.append(learn.METRIC_FIELDS[key])
                continue
            if value is not None:
                if key == "retention_pct" and value > 100:
                    bad.append("Retention (max. 100 %)")
                    continue
                metrics[key] = value
        overrides = {k: self.TRI[v.get()] for k, v in self.override_vars.items() if self.TRI[v.get()] is not None}
        if self.hook_entry.get().strip():
            try:
                overrides["hook_s"] = parse_number(self.hook_entry.get())
            except ValueError:
                bad.append("Hook (Sekunden)")
        if bad:
            raise ValueError("Bitte gültige Zahlen eintragen bei: " + ", ".join(bad))
        title = self.title_entry.get().strip()
        if not title:
            raise ValueError("Der Titel darf nicht leer sein.")
        return {
            "title": title,
            "platform": "" if self.platform.get() == "—" else self.platform.get(),
            "rating": "" if self.rating.get() == "keine Angabe" else self.rating.get(),
            "metrics": metrics,
            "overrides": overrides,
            "notes": self.notes.get("1.0", "end").strip(),
        }

    def save(self) -> bool:
        try:
            changes = self._parse()
        except ValueError as exc:
            self.error.configure(text=str(exc))
            return False
        with self.page.lock:
            self.page.library.update(self.video, **changes)
        self._initial = self._collect_raw()
        self.page.profile_changed()
        self.page.refresh()
        self.close()
        return True

    def autosave(self) -> bool:
        """Vor einem Update: Änderungen speichern, wenn möglich (ungültige Eingaben → False)."""
        return self.save() if self.has_changes() else (self.close() or True)

    def cancel(self) -> None:
        if self.has_changes() and not messagebox.askyesno("Verwerfen?", "Änderungen verwerfen?", parent=self):
            return
        self.close()

    def close(self) -> None:
        self.app.open_dialogs.discard(self)
        self.destroy()
