"""Lernmodul „Aus fertigen Videos lernen“.

Referenzvideos (bereits geschnittene Videos) werden in eine Bibliothek kopiert und analysiert:
Länge, Schnittfrequenz, Hook, Tempo/Rhythmus, Text-Overlays/Untertitel, Musik/Sprache und
Struktur (Intro, Höhepunkt, Call-to-Action am Ende). Zusammen mit den Ergebnissen beim Publikum
(Views, Retention … oder einfach „lief gut/mittel/schlecht“) entsteht ein Erfolgsprofil:
Welche Merkmale haben gut laufende Videos häufiger als schwache? Daraus werden Empfehlungen
für neue Videos abgeleitet.

Alle Messungen laufen lokal mit ffmpeg + numpy (ohne Spracherkennung/OCR) und sind deshalb
Schätzungen – einzelne Werte lassen sich pro Video manuell korrigieren („Overrides“).
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import threading
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from . import render
from .ffmpeg_bin import ffmpeg_exe, popen_kwargs
from .models import now_utc

ANALYSIS_VERSION = 1
SUPPORTED_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".flv", ".ts", ".wmv", ".mpg", ".mpeg", ".3gp"}
PLATFORMS = ["TikTok", "YouTube", "YouTube Shorts", "Instagram", "Twitch", "Facebook", "Andere"]
RATINGS = {"gut": 1.0, "mittel": 0.5, "schlecht": 0.0}
METRIC_FIELDS = {
    "views": "Views",
    "avg_watch_s": "Ø Wiedergabedauer (s)",
    "retention_pct": "Retention (%)",
    "likes": "Likes",
    "comments": "Kommentare",
    "shares": "Shares",
}

Progress = Callable[[str, float], None] | None


class UnsupportedVideo(ValueError):
    pass


# ================================================================ Merkmale

@dataclass(frozen=True)
class Feature:
    key: str
    label: str
    unit: str
    kind: str  # "num" | "bool"
    more: str  # Empfehlung, wenn gute Videos MEHR davon haben
    less: str  # Empfehlung, wenn gute Videos WENIGER davon haben
    decimals: int = 1


FEATURES: list[Feature] = [
    Feature("hook_s", "Hook – erster starker Moment", "s", "num",
            "Lass dir am Anfang etwas mehr Zeit", "Starte schneller: Hook früher setzen"),
    Feature("cuts_per_min", "Schnittfrequenz", "Cuts/Min", "num",
            "Schnittfrequenz erhöhen", "Weniger schneiden, Szenen länger stehen lassen", 0),
    Feature("duration_s", "Länge", "s", "num", "Videos dürfen länger sein", "Videos kürzer halten", 0),
    Feature("avg_shot_s", "Ø Einstellungslänge", "s", "num",
            "Einstellungen länger stehen lassen", "Kürzere Einstellungen"),
    Feature("motion", "Tempo (Bewegung im Bild)", "", "num", "Mehr Bewegung/Action im Bild", "Ruhigere Bilder"),
    Feature("rhythm_cv", "Rhythmus-Abwechslung", "", "num",
            "Schnittrhythmus abwechslungsreicher gestalten", "Gleichmäßigerer Schnittrhythmus", 2),
    Feature("hook_energy_db", "Hook-Energie (Anfang vs. Rest)", "dB", "num",
            "Lauter/energiegeladener einsteigen", "Ruhiger einsteigen"),
    Feature("text_overlay_pct", "Text-Overlays", "% der Zeit", "num", "Mehr Text-Overlays einsetzen", "Weniger Text im Bild", 0),
    Feature("subtitles_pct", "Untertitel", "% der Zeit", "num", "Untertitel einblenden", "Weniger/keine Untertitel", 0),
    Feature("music_pct", "Musik", "% der Zeit", "num", "Mehr Musik unterlegen", "Weniger Musik", 0),
    Feature("speech_pct", "Sprache", "% der Zeit", "num", "Mehr gesprochene Inhalte", "Weniger Gerede, mehr Action", 0),
    Feature("loudness_db", "Lautstärke", "dB", "num", "Insgesamt lauter abmischen", "Leiser abmischen"),
    Feature("intro_s", "Intro-Länge", "s", "num", "Längeres Intro", "Intro kürzen oder weglassen"),
    Feature("climax_pos_pct", "Position des Höhepunkts", "% der Länge", "num",
            "Höhepunkt später setzen (Spannung aufbauen)", "Höhepunkt früher bringen", 0),
    Feature("has_subtitles", "Hat Untertitel", "", "bool", "Untertitel einblenden", "Untertitel weglassen"),
    Feature("has_music", "Hat Musik", "", "bool", "Musik unterlegen", "Auf Musik verzichten"),
    Feature("has_text_hook", "Text im Hook (erste 3 s)", "", "bool",
            "In den ersten Sekunden Text einblenden", "Ohne Text einsteigen"),
    Feature("has_cta", "Call-to-Action am Ende", "", "bool", "Am Ende einen Call-to-Action zeigen", "Ohne CTA enden"),
    Feature("vertical", "Hochformat (9:16)", "", "bool", "Hochformat nutzen", "Querformat nutzen"),
]
FEATURE_BY_KEY = {f.key: f for f in FEATURES}
EDITABLE_OVERRIDES = ("hook_s", "has_subtitles", "has_music", "has_text_hook", "has_cta")


# ================================================================ Bild-Analyse

def _read_frames(path: Path, fps: float, w: int, h: int, max_frames: int = 20000) -> np.ndarray:
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", str(path), "-an",
           "-vf", f"fps={fps},scale={w}:{h},format=gray", "-f", "rawvideo", "-"]
    proc = subprocess.run(cmd, capture_output=True, **popen_kwargs())
    data = proc.stdout
    n = min(len(data) // (w * h), max_frames)
    if n == 0:
        return np.zeros((0, h, w), np.uint8)
    return np.frombuffer(data[: n * w * h], dtype=np.uint8).reshape(n, h, w)


def detect_cuts(frames: np.ndarray, fps: float) -> tuple[list[float], np.ndarray]:
    """Harte Schnitte = sprunghafte Bildänderung gegenüber der lokalen Bewegung. Gibt (Zeiten, Differenzen)."""
    if len(frames) < 2:
        return [], np.zeros(0, np.float32)
    f = frames.astype(np.int16)
    diff = np.abs(f[1:] - f[:-1]).mean(axis=(1, 2)).astype(np.float32)
    hist = np.stack([np.histogram(fr, bins=16, range=(0, 256))[0] for fr in frames]).astype(np.float32)
    hist /= hist.sum(axis=1, keepdims=True) + 1e-9
    hdiff = np.abs(hist[1:] - hist[:-1]).sum(axis=1)
    k = max(3, int(fps))
    padded = np.pad(diff, (k, k), mode="edge")
    local = np.array([np.median(padded[i:i + 2 * k + 1]) for i in range(len(diff))])
    is_cut = (diff > 3 * local + 10) & (diff > 16) | (hdiff > 0.6)
    cuts, last = [], -1e9
    for i in np.nonzero(is_cut)[0]:
        t = (i + 1) / fps
        if t - last >= 0.4:
            cuts.append(round(t, 2))
            last = t
    return cuts, diff


def _text_rows(img: np.ndarray) -> np.ndarray:
    """Zeilen mit textartigem Muster: viele harte Hell/Dunkel-Wechsel UND helle Pixel mit dunklem Rand."""
    g = img.astype(np.int16)
    edges = np.abs(np.diff(g, axis=1)) > 70
    transitions = edges.sum(axis=1)
    bright = g > 190
    dark = g < 80
    outlined = bright[:, 2:-2] & (dark[:, :-4] | dark[:, 4:])
    return (transitions >= 8) & (outlined.sum(axis=1) >= 4)


def text_presence(frames: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Pro Frame: (Untertitel unten?, Text-Overlay sonst?). Mind. 3 benachbarte Textzeilen nötig."""
    n, h, _ = frames.shape if len(frames) else (0, 0, 0)
    subs = np.zeros(n, bool)
    over = np.zeros(n, bool)
    cut = int(h * 0.72)

    def has_block(rows: np.ndarray) -> bool:
        run = best = 0
        for r in rows:
            run = run + 1 if r else 0
            best = max(best, run)
        return best >= 3

    for i, fr in enumerate(frames):
        rows = _text_rows(fr)
        subs[i] = has_block(rows[cut:])
        over[i] = has_block(rows[:cut])
    # Overlays bleiben stehen – einzelne Treffer ohne Nachbarn sind meist Bildrauschen
    def persist(x: np.ndarray) -> np.ndarray:
        if len(x) < 2:
            return x
        nb = np.zeros_like(x)
        nb[1:] |= x[:-1]
        nb[:-1] |= x[1:]
        return x & nb

    return persist(subs), persist(over)


# ================================================================ Ton-Analyse

AUDIO_RATE = 16000
SPEC_FRAME = 512  # 32 ms


def _read_audio(path: Path) -> np.ndarray:
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn",
           "-ac", "1", "-ar", str(AUDIO_RATE), "-f", "s16le", "-"]
    data = subprocess.run(cmd, capture_output=True, **popen_kwargs()).stdout
    return np.frombuffer(data[: len(data) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768.0


def audio_features(x: np.ndarray) -> dict:
    """Lautstärke pro Sekunde, Musik- und Sprachanteil (geschätzt)."""
    secs = len(x) // AUDIO_RATE
    if secs == 0:
        return {"loud": np.zeros(0), "music": np.zeros(0, bool), "speech": np.zeros(0, bool), "loudness_db": -90.0}
    x = x[: secs * AUDIO_RATE]
    per_sec = x.reshape(secs, AUDIO_RATE)
    loud = 20 * np.log10(np.sqrt((per_sec ** 2).mean(axis=1)) + 1e-6)
    frames = x[: len(x) // SPEC_FRAME * SPEC_FRAME].reshape(-1, SPEC_FRAME)
    spec = np.abs(np.fft.rfft(frames * np.hanning(SPEC_FRAME), axis=1)) ** 2 + 1e-12
    freqs = np.fft.rfftfreq(SPEC_FRAME, 1 / AUDIO_RATE)
    band = (freqs >= 150) & (freqs <= 5000)
    sb = spec[:, band]
    flatness = np.exp(np.log(sb).mean(axis=1)) / sb.mean(axis=1)  # 0 = tonal, 1 = Rauschen
    energy = 10 * np.log10(sb.sum(axis=1))
    per = AUDIO_RATE // SPEC_FRAME  # Frames pro Sekunde (31)
    n = min(secs, len(flatness) // per)
    flat = flatness[: n * per].reshape(n, per)
    en = energy[: n * per].reshape(n, per)
    # „aktiv“ = höchstens 30 dB unter dem lauten Pegel (Pausen in Sprache fallen darunter, Musik nicht)
    active = en > np.percentile(energy, 95) - 30
    tonal = (flat < 0.12).mean(axis=1)
    continuity = active.mean(axis=1)
    # Silbenrhythmus über den spektralen Fluss: Sprache erzeugt 2–7 Mal pro Sekunde neue Klänge,
    # gleichbleibende Musiktöne kaum – so wird Sprache auch unter Hintergrundmusik erkannt.
    logspec = np.log(sb)
    weights = sb[1:] / sb[1:].sum(axis=1, keepdims=True)  # nach Energie gewichtet: Rauschboden zählt nicht
    flux = np.concatenate([[0.0], (np.clip(np.diff(logspec, axis=0), 0, None) * weights).sum(axis=1)])
    fl = flux[: n * per].reshape(n, per)
    env = fl - fl.mean(axis=1, keepdims=True)
    pw = np.abs(np.fft.rfft(env * np.hanning(per), axis=1)) ** 2
    mf = np.fft.rfftfreq(per, 1 / per)
    syll = pw[:, (mf >= 2) & (mf <= 7)].sum(axis=1) / (pw[:, (mf >= 0.5) & (mf <= 14)].sum(axis=1) + 1e-9)
    depth = en.std(axis=1)
    flux_peaks = np.percentile(fl, 90, axis=1)
    music = (tonal > 0.5) & (continuity > 0.85) & (depth < 9)
    speech = (syll > 0.35) & (flux_peaks > 1.0) & (continuity > 0.3)
    loud_active = loud[loud > np.percentile(loud, 20)] if len(loud) > 4 else loud
    return {
        "loud": loud[:n] if n else loud,
        "music": music,
        "speech": speech,
        "loudness_db": float(np.median(loud_active)) if len(loud_active) else -90.0,
    }


# ================================================================ Gesamtanalyse

def analyze_video(path: str | Path, progress: Progress = None) -> dict:
    path = Path(path)
    if path.suffix.lower() not in SUPPORTED_EXT:
        raise UnsupportedVideo(f"Format „{path.suffix or '?'}“ wird nicht unterstützt. Erlaubt: {', '.join(sorted(SUPPORTED_EXT))}")
    try:
        info = render.probe(path)
    except render.RenderError as exc:
        raise UnsupportedVideo(f"„{path.name}“ ist kein lesbares Video (Datei beschädigt oder Format unbekannt).") from exc
    if not info.get("width"):
        raise UnsupportedVideo(f"„{path.name}“ enthält keine Videospur.")
    duration = float(info["duration"])
    if duration < 1:
        raise UnsupportedVideo(f"„{path.name}“ ist kürzer als eine Sekunde.")

    if progress:
        progress("Analysiere Schnitte", 0.1)
    fps = 10.0 if duration <= 1200 else max(1.0, 12000 / duration)
    small = _read_frames(path, fps, 96, 54)
    cuts, diff = detect_cuts(small, fps)
    if progress:
        progress("Analysiere Text", 0.45)
    tfps = 2.0 if duration <= 1200 else max(0.2, 2400 / duration)
    big = _read_frames(path, tfps, 320, 180, max_frames=2400)
    subs, over = text_presence(big)
    if progress:
        progress("Analysiere Ton", 0.7)
    audio = audio_features(_read_audio(path)) if info.get("has_audio") else None

    minutes = duration / 60
    bounds = [0.0] + cuts + [duration]
    shots = np.diff(bounds)
    shots = shots[shots > 0.05]
    loud = audio["loud"] if audio else np.zeros(0)
    med_loud = float(np.median(loud)) if len(loud) else -90.0
    # Hook: erster Schnitt, erster deutlicher Lautstärke-Peak oder Text in den ersten 3 s
    first_cut = next((c for c in cuts if c <= 3.0), None)
    peak_t = None
    if len(loud) >= 2:
        head = loud[:3]
        i = int(np.argmax(head))
        if head[i] >= med_loud + 3:
            peak_t = float(i) + 0.5
    head_text = bool((subs[: max(1, int(3 * tfps))] | over[: max(1, int(3 * tfps))]).any()) if len(subs) else False
    candidates = [t for t in (first_cut, peak_t, 0.5 if head_text else None) if t is not None]
    hook_s = round(min(candidates), 2) if candidates else 3.0
    hook_energy = float(np.mean(loud[:3]) - med_loud) if len(loud) >= 4 else 0.0
    intro_s = 0.0
    if len(loud):
        above = np.nonzero(loud >= med_loud - 3)[0]
        intro_s = float(above[0]) if len(above) else 0.0
    # Höhepunkt: lauteste + bewegteste Stelle (geglättet)
    climax = 50.0
    if len(loud) >= 3:
        motion_sec = np.array([diff[int(s * fps):int((s + 1) * fps)].mean() if int(s * fps) < len(diff) else 0
                               for s in range(len(loud))])
        z = (loud - loud.mean()) / (loud.std() + 1e-6) + (motion_sec - motion_sec.mean()) / (motion_sec.std() + 1e-6)
        z = np.convolve(z, np.ones(3) / 3, mode="same")
        climax = 100.0 * (int(np.argmax(z)) + 0.5) / len(loud)
    # CTA am Ende: Text in den letzten Sekunden oder ruhiger Endbildschirm
    tail = max(1, int(3 * tfps))
    end_text = bool((subs[-tail:] | over[-tail:]).any()) if len(subs) else False
    # Endbildschirm: ruhiges Standbild am Ende eines sonst bewegten Videos
    end_static = bool(
        len(diff) > fps * 6 and diff[-int(fps * 3):].mean() < 1.0 and np.median(diff[: -int(fps * 3)]) > 2.0
    )

    features = {
        "duration_s": round(duration, 1),
        "cuts": len(cuts),
        "cuts_per_min": round(len(cuts) / minutes, 1) if minutes else 0.0,
        "avg_shot_s": round(float(shots.mean()), 2) if len(shots) else round(duration, 2),
        "rhythm_cv": round(float(shots.std() / shots.mean()), 2) if len(shots) > 1 else 0.0,
        "motion": round(float(np.median(diff)), 2) if len(diff) else 0.0,
        "hook_s": hook_s,
        "hook_energy_db": round(hook_energy, 1),
        "text_overlay_pct": round(100 * float(over.mean()), 0) if len(over) else 0.0,
        "subtitles_pct": round(100 * float(subs.mean()), 0) if len(subs) else 0.0,
        "music_pct": round(100 * float(audio["music"].mean()), 0) if audio and len(audio["music"]) else 0.0,
        "speech_pct": round(100 * float(audio["speech"].mean()), 0) if audio and len(audio["speech"]) else 0.0,
        "loudness_db": round(audio["loudness_db"], 1) if audio else -90.0,
        "intro_s": round(intro_s, 1),
        "climax_pos_pct": round(climax, 0),
        "has_text_hook": head_text,
        "has_cta": end_text or end_static,
        "vertical": bool(info["height"] > info["width"]),
        "width": info["width"],
        "height": info["height"],
        "has_audio": bool(info.get("has_audio")),
    }
    features["has_subtitles"] = features["subtitles_pct"] >= 20
    features["has_music"] = features["music_pct"] >= 30
    features["version"] = ANALYSIS_VERSION
    if progress:
        progress("Analyse fertig", 1.0)
    return features


# ================================================================ Bibliothek

def run_analysis(path: Path, progress: Progress = None) -> tuple[dict | None, str]:
    """(Merkmale, Fehlertext) – Fehler werden pro Video gespeichert statt die App abstürzen zu lassen."""
    try:
        return analyze_video(path, progress), ""
    except UnsupportedVideo as exc:
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001
        return None, f"Analyse fehlgeschlagen: {exc}"


@dataclass
class RefVideo:
    id: str
    title: str
    file: str  # relativ zum Bibliotheksordner
    original_name: str
    added_at: str
    platform: str = ""
    rating: str = ""  # gut | mittel | schlecht | ""
    metrics: dict = field(default_factory=dict)
    notes: str = ""
    excluded: bool = False
    analysis: dict = field(default_factory=dict)
    overrides: dict = field(default_factory=dict)
    error: str = ""
    source_url: str = ""  # bei Link-Videos: Original (YouTube, TikTok …)
    channel: str = ""
    metrics_auto: list = field(default_factory=list)  # Kennzahlen, die automatisch aus dem Link kamen

    def features(self) -> dict:
        return {**self.analysis, **self.overrides}

    @property
    def analyzed(self) -> bool:
        return bool(self.analysis) and not self.error


class Library:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.file = self.root / "library.json"
        self.videos: list[RefVideo] = []
        self._lock = threading.RLock()  # Downloads/Analysen laufen parallel zur Oberfläche
        if self.file.exists():
            data = json.loads(self.file.read_text(encoding="utf-8"))
            known = set(RefVideo.__dataclass_fields__)
            self.videos = [RefVideo(**{k: v for k, v in d.items() if k in known}) for d in data.get("videos", [])]

    def save(self) -> None:
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"videos": [asdict(v) for v in self.videos]}, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(self.file)

    def path(self, video: RefVideo) -> Path:
        return self.root / video.file

    def get(self, vid: str) -> RefVideo:
        for v in self.videos:
            if v.id == vid:
                return v
        raise KeyError(vid)

    def add(self, source: str | Path) -> RefVideo:
        """Kopiert ein Video in die Bibliothek (prüft vorher, ob ffmpeg es lesen kann)."""
        source = Path(source)
        if not source.is_file():
            raise UnsupportedVideo(f"Datei nicht gefunden: {source}")
        if source.suffix.lower() not in SUPPORTED_EXT:
            raise UnsupportedVideo(
                f"„{source.name}“: Format „{source.suffix or '?'}“ wird nicht unterstützt. "
                f"Erlaubt sind {', '.join(sorted(e.lstrip('.') for e in SUPPORTED_EXT))}."
            )
        try:
            info = render.probe(source)
        except render.RenderError as exc:
            raise UnsupportedVideo(f"„{source.name}“ ist kein lesbares Video.") from exc
        if not info.get("width"):
            raise UnsupportedVideo(f"„{source.name}“ enthält keine Videospur.")
        vid = uuid.uuid4().hex[:10]
        (self.root / "videos").mkdir(parents=True, exist_ok=True)
        rel = f"videos/{vid}{source.suffix.lower()}"
        shutil.copy2(source, self.root / rel)
        video = RefVideo(id=vid, title=source.stem, file=rel, original_name=source.name, added_at=now_utc().isoformat())
        self.videos.append(video)
        self.save()
        return video

    def add_url(self, url: str, progress: Progress = None) -> RefVideo:
        """Lädt ein Video per Link (YouTube, Shorts, TikTok, Instagram …) und übernimmt öffentliche Zahlen."""
        from . import fetch

        url = url.strip()
        if not fetch.is_url(url):
            raise UnsupportedVideo(f"„{url[:80]}“ ist kein Link.")
        if not url.lower().startswith("http"):
            url = f"https://{url}"
        for v in self.videos:
            if v.source_url and v.source_url.rstrip("/") == url.rstrip("/"):
                raise UnsupportedVideo(f"Dieses Video ist schon in den Referenzvideos („{v.title}“).")
        vid = uuid.uuid4().hex[:10]
        try:
            path, info = fetch.download_reference(url, self.root / "videos", vid, progress)
        except fetch.LinkError as exc:
            raise UnsupportedVideo(f"Link konnte nicht geladen werden: {exc}") from exc
        metrics = fetch.public_metrics(info)
        video = RefVideo(
            id=vid, title=(info.get("title") or url)[:120], file=f"videos/{path.name}",
            original_name=url, added_at=now_utc().isoformat(), platform=fetch.platform_of(info, url),
            metrics=metrics, source_url=info.get("webpage_url") or url,
            channel=info.get("uploader") or info.get("channel") or "", metrics_auto=sorted(metrics),
        )
        with self._lock:
            self.videos.append(video)
            self.save()
        return video

    def refresh_metrics(self, video: RefVideo) -> RefVideo:
        """Aktuelle Views/Likes/Kommentare für Link-Videos nachladen (manuell eingetragene Werte bleiben)."""
        from . import fetch

        if not video.source_url:
            raise UnsupportedVideo("Nur bei Videos, die per Link hinzugefügt wurden.")
        try:
            fresh = fetch.fetch_metrics(video.source_url)
        except fetch.LinkError as exc:
            raise UnsupportedVideo(f"Zahlen konnten nicht geladen werden: {exc}") from exc
        manual = {k: v for k, v in video.metrics.items() if k not in video.metrics_auto}
        video.metrics = {**fresh, **manual}
        video.metrics_auto = sorted(k for k in fresh if k not in manual)
        self.save()
        return video

    def analyze(self, video: RefVideo, progress: Progress = None) -> RefVideo:
        return self.apply_analysis(video, *run_analysis(self.path(video), progress))

    def apply_analysis(self, video: RefVideo, analysis: dict | None, error: str) -> RefVideo:
        if analysis is not None:
            video.analysis = analysis
        video.error = error
        self.save()
        return video

    def remove(self, video: RefVideo) -> None:
        self.path(video).unlink(missing_ok=True)
        thumb = self.path(video).with_suffix(".thumb.jpg")
        thumb.unlink(missing_ok=True)
        with self._lock:
            self.videos = [v for v in self.videos if v.id != video.id]
            self.save()

    def update(self, video: RefVideo, **changes) -> None:
        for key, value in changes.items():
            if key not in ("title", "platform", "rating", "metrics", "notes", "excluded", "overrides"):
                raise ValueError(f"Feld {key!r} ist nicht änderbar")
            if key == "metrics":  # von Hand geänderte Zahlen werden beim Aktualisieren nicht überschrieben
                video.metrics_auto = [k for k in video.metrics_auto if value.get(k) == video.metrics.get(k)]
            setattr(video, key, value)
        self.save()

    def learning_set(self) -> list[RefVideo]:
        return [v for v in self.videos if not v.excluded and v.analyzed]


# ================================================================ Erfolg + Profil

def _pct_rank(values: list[float]) -> list[float]:
    if len(values) == 1:
        return [0.5]
    order = sorted(values)
    return [(sum(1 for o in order if o < v) + 0.5 * (order.count(v) - 1)) / (len(values) - 1) for v in values]


def success_scores(videos: list[RefVideo]) -> dict[str, float]:
    """0..1 je Video. Zahlen werden innerhalb derselben Plattform verglichen (Ränge), die Bewertung zählt mit."""
    scores: dict[str, list[float]] = {v.id: [] for v in videos}
    by_platform: dict[str, list[RefVideo]] = {}
    for v in videos:
        by_platform.setdefault(v.platform or "?", []).append(v)
    for group in by_platform.values():
        def component(getter):
            pairs = [(v, getter(v)) for v in group]
            pairs = [(v, x) for v, x in pairs if x is not None]
            if len(pairs) >= 2:
                for (v, _), r in zip(pairs, _pct_rank([x for _, x in pairs])):
                    yield v, r

        def metric(v, k):
            x = v.metrics.get(k)
            return float(x) if x not in (None, "") else None

        def engagement(v):
            views = metric(v, "views")
            parts = [metric(v, k) for k in ("likes", "comments", "shares")]
            if not views or all(p is None for p in parts):
                return None
            return sum(p or 0 for p in parts) / views

        def watch_share(v):
            if metric(v, "retention_pct") is not None:
                return metric(v, "retention_pct")
            avg = metric(v, "avg_watch_s")
            dur = v.analysis.get("duration_s")
            return 100 * avg / dur if avg is not None and dur else None

        weighted = [(lambda v: watch_share(v), 0.5), (lambda v: metric(v, "views"), 0.3), (engagement, 0.2)]
        per_video: dict[str, list[tuple[float, float]]] = {}
        for getter, w in weighted:
            for v, r in component(getter):
                per_video.setdefault(v.id, []).append((r, w))
        for vid, parts in per_video.items():
            total_w = sum(w for _, w in parts)
            scores[vid].append(sum(r * w for r, w in parts) / total_w)
    for v in videos:
        if v.rating in RATINGS:
            scores[v.id].append(RATINGS[v.rating])
    return {vid: sum(s) / len(s) for vid, s in scores.items() if s}


@dataclass
class ProfileItem:
    feature: Feature
    good: float
    weak: float
    effect: float  # standardisierter Unterschied (numerisch) bzw. Prozentpunkte/100 (ja/nein)
    strength: str  # "deutlich" | "leicht" | "kein"

    @property
    def direction(self) -> int:
        return 1 if self.good > self.weak else -1 if self.good < self.weak else 0

    def fmt(self, value: float) -> str:
        f = self.feature
        if f.kind == "bool":
            return f"{value * 100:.0f} %"
        txt = f"{value:.{f.decimals}f}".replace(".", ",")
        return f"{txt} {f.unit}".strip()

    def advice(self) -> str:
        f = self.feature
        base = f.more if self.direction > 0 else f.less
        if f.kind == "bool":
            return f"{base} – {self.fmt(self.good)} deiner gut laufenden Videos vs. {self.fmt(self.weak)} der schwachen."
        return f"{base} – deine besten Videos: {self.fmt(self.good)}, schwache: {self.fmt(self.weak)}."


@dataclass
class Profile:
    n_total: int
    n_scored: int
    n_good: int
    n_weak: int
    items: list[ProfileItem] = field(default_factory=list)
    message: str = ""
    good_medians: dict = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return bool(self.items)

    def key_items(self, limit: int = 5) -> list[ProfileItem]:
        return [i for i in self.items if i.strength != "kein"][:limit]


MIN_PER_GROUP = 2


def build_profile(library: Library) -> Profile:
    videos = library.learning_set()
    scores = success_scores(videos)
    scored = [v for v in videos if v.id in scores]
    good = [v for v in scored if scores[v.id] >= 0.6]
    weak = [v for v in scored if scores[v.id] <= 0.4]
    if len(good) < MIN_PER_GROUP or len(weak) < MIN_PER_GROUP:
        # bei vielen mittelmäßigen Bewertungen: oberes/unteres Drittel nehmen
        if len(scored) >= 2 * MIN_PER_GROUP:
            ranked = sorted(scored, key=lambda v: scores[v.id])
            k = max(MIN_PER_GROUP, len(ranked) // 3)
            if scores[ranked[-1].id] > scores[ranked[0].id]:
                weak, good = ranked[:k], ranked[-k:]
    profile = Profile(len(videos), len(scored), len(good), len(weak))
    if len(good) < MIN_PER_GROUP or len(weak) < MIN_PER_GROUP:
        profile.message = (
            f"Noch zu wenig Vergleichsdaten: {len(scored)} bewertete Video(s). Nötig sind mindestens "
            f"{MIN_PER_GROUP} gut und {MIN_PER_GROUP} schwach laufende – trage Kennzahlen oder eine Bewertung ein."
        )
        return profile
    items = []
    for f in FEATURES:
        g = [float(v.features().get(f.key)) for v in good if v.features().get(f.key) is not None]
        w = [float(v.features().get(f.key)) for v in weak if v.features().get(f.key) is not None]
        if len(g) < MIN_PER_GROUP or len(w) < MIN_PER_GROUP:
            continue
        mg, mw = float(np.mean(g)), float(np.mean(w))
        if f.kind == "bool":
            effect = mg - mw
            strength = "deutlich" if abs(effect) >= 0.4 else "leicht" if abs(effect) >= 0.2 else "kein"
        else:
            sd = math.sqrt((np.var(g, ddof=1) + np.var(w, ddof=1)) / 2) if len(g) > 1 and len(w) > 1 else 0.0
            scale = max(sd, 0.05 * max(abs(mg), abs(mw), 1e-6))
            effect = (mg - mw) / scale
            strength = "deutlich" if abs(effect) >= 0.8 else "leicht" if abs(effect) >= 0.4 else "kein"
        items.append(ProfileItem(f, mg, mw, effect, strength))
        profile.good_medians[f.key] = float(np.median(g))
    items.sort(key=lambda i: (i.strength == "kein", -abs(i.effect)))
    profile.items = items
    return profile


def recommendations(profile: Profile, limit: int = 5) -> list[str]:
    return [i.advice() for i in profile.key_items(limit)]


def check_video(features: dict, profile: Profile, limit: int = 5) -> list[str]:
    """Konkrete Hinweise für ein neues Video: wo liegt es näher an den schwachen als an den guten Videos?"""
    tips = []
    for item in profile.key_items(12):
        value = features.get(item.feature.key)
        if value is None:
            continue
        value = float(value)
        if abs(value - item.weak) < abs(value - item.good):
            f = item.feature
            base = f.more if item.direction > 0 else f.less
            tips.append(f"{base}: dein Video {item.fmt(value)}, deine besten {item.fmt(item.good)}.")
        if len(tips) >= limit:
            break
    return tips


def suggested_options(profile: Profile) -> dict:
    """Übersetzt das Profil in Einstellungen für den Highlight-Generator."""
    opts: dict = {}
    if not profile.ready:
        return opts
    by_key = {i.feature.key: i for i in profile.items if i.strength != "kein"}
    dur = profile.good_medians.get("duration_s")
    if "duration_s" in by_key and dur:
        opts["minutes"] = min((5, 10, 12, 15), key=lambda m: abs(m * 60 - dur))
    if "hook_s" in by_key and by_key["hook_s"].direction < 0:
        opts["pre_roll"] = 10.0  # Clips näher am Höhepunkt beginnen lassen
    if ("cuts_per_min" in by_key and by_key["cuts_per_min"].direction > 0) or (
        "avg_shot_s" in by_key and by_key["avg_shot_s"].direction < 0
    ):
        opts["max_clip"] = 40.0  # kürzere Clips = mehr Schnitte
    return opts
