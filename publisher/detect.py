"""Automatische Highlight-Erkennung über die Tonspur – mit Fokus auf Lachen.

Pro Sekunde werden zwei Dinge gemessen:

* **Aufregung**: Wie viel lauter als der normale Pegel der letzten Minuten ist es?
* **Lachen**: Lachen ist rhythmisch – „ha-ha-ha“ pulsiert mit 3,5–7,5 Silben pro Sekunde
  im Sprachfrequenzbereich (300–3000 Hz). Wir messen, wie stark die Lautstärke-Hüllkurve
  genau in diesem Takt schwingt. Gleichmäßiges Schreien, Musik oder Explosionen
  schwingen anders und bekommen deshalb weniger Punkte.

Beides zusammen ergibt die Highlight-Kurve. Schnittpunkte werden anschließend auf die
nächste leise Stelle geschoben, damit kein Wort mittendrin abgeschnitten wird.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from .ffmpeg_bin import ffmpeg_exe, popen_kwargs

SAMPLE_RATE = 8000
FRAME = 160  # 20 ms → Hüllkurve mit 50 Hz
FRAMES_PER_SEC = SAMPLE_RATE // FRAME
Progress = Callable[[str, float], None] | None

FOCUS_LAUGH = "lachen"
FOCUS_LOUD = "laut"


@dataclass
class Highlight:
    start: float
    end: float
    peak: float
    score: float

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class AudioAnalysis:
    loudness: np.ndarray  # dB pro Sekunde (Gesamtsignal)
    laugh: np.ndarray  # 0..1 pro Sekunde: Anteil der Hüllkurven-Schwingung im Lach-Takt
    envelope: np.ndarray  # Sprachband-Hüllkurve mit 10 Hz (für die Synchronisierung mehrerer VODs)

    @property
    def duration(self) -> int:
        return len(self.loudness)


# ---------------------------------------------------------------- Messung

def _decode(path: str | Path):
    cmd = [
        ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", str(path),
        "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-",
    ]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **popen_kwargs())


_freqs = np.fft.rfftfreq(FRAME, 1 / SAMPLE_RATE)
_SPEECH = (_freqs >= 300) & (_freqs <= 3000)
_WINDOW = np.hanning(FRAME).astype(np.float32)


def analyze_audio(path: str | Path, duration: float | None = None, progress: Progress = None) -> AudioAnalysis:
    """Dekodiert die Tonspur einmal (streamend) und misst Lautstärke, Lach-Rhythmus und Hüllkurve."""
    proc = _decode(path)
    assert proc.stdout is not None
    loud: list[np.ndarray] = []
    band_env: list[np.ndarray] = []
    chunk = SAMPLE_RATE * 2 * 60  # eine Minute pro Lesevorgang
    rest = b""
    seconds = 0
    while True:
        data = proc.stdout.read(chunk)
        if not data:
            break
        data = rest + data
        usable = len(data) - len(data) % (SAMPLE_RATE * 2)
        rest = data[usable:]
        if not usable:
            continue
        x = np.frombuffer(data[:usable], dtype=np.int16).astype(np.float32) / 32768.0
        sec = x.reshape(-1, SAMPLE_RATE)
        loud.append(20 * np.log10(np.sqrt(np.mean(sec * sec, axis=1)) + 1e-6))
        frames = x.reshape(-1, FRAME) * _WINDOW
        spec = np.abs(np.fft.rfft(frames, axis=1)) ** 2
        band_env.append(np.sqrt(spec[:, _SPEECH].sum(axis=1) + 1e-12).astype(np.float32))
        seconds += sec.shape[0]
        if progress and duration:
            progress("Analysiere Ton", min(seconds / duration, 1.0))
    err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
    if proc.wait() != 0 and not loud:
        raise RuntimeError(f"Tonspur konnte nicht gelesen werden: {err.strip()[-300:]}")
    if not loud:
        return AudioAnalysis(np.zeros(0, np.float32), np.zeros(0, np.float32), np.zeros(0, np.float32))
    loudness = np.concatenate(loud).astype(np.float32)
    env = np.concatenate(band_env)
    return AudioAnalysis(loudness, laugh_rhythm(env), _envelope_10hz(env))


def laugh_rhythm(env50: np.ndarray) -> np.ndarray:
    """Anteil der Hüllkurven-Energie bei 3,5–7,5 Hz (Lach-Takt) je Sekunde, 2-Sekunden-Fenster."""
    n_sec = len(env50) // FRAMES_PER_SEC
    if n_sec == 0:
        return np.zeros(0, np.float32)
    log_env = np.log(env50[: n_sec * FRAMES_PER_SEC] + 1e-6)
    win = 2 * FRAMES_PER_SEC
    padded = np.pad(log_env, (FRAMES_PER_SEC // 2, win), mode="edge")
    idx = np.arange(n_sec)[:, None] * FRAMES_PER_SEC + np.arange(win)[None, :]
    w = padded[idx]
    w = w - w.mean(axis=1, keepdims=True)
    power = np.abs(np.fft.rfft(w * np.hanning(win), axis=1)) ** 2
    f = np.fft.rfftfreq(win, 1 / FRAMES_PER_SEC)
    laugh_band = (f >= 3.5) & (f <= 7.5)
    all_band = (f >= 1.0) & (f <= 12.0)
    ratio = power[:, laugh_band].sum(axis=1) / (power[:, all_band].sum(axis=1) + 1e-9)
    # gleichmäßiges Rauschen hat auch etwas Energie im Lach-Band – erst die starke Pulsation zählt
    depth = np.clip(w.std(axis=1) / 0.8, 0, 1)
    return (ratio * depth).astype(np.float32)


def _envelope_10hz(env50: np.ndarray) -> np.ndarray:
    n = len(env50) // 5
    e = env50[: n * 5].reshape(n, 5).mean(axis=1)
    return (20 * np.log10(e + 1e-6)).astype(np.float32)


def loudness_per_second(path: str | Path, duration: float | None = None, progress: Progress = None) -> np.ndarray:
    return analyze_audio(path, duration, progress).loudness


# ---------------------------------------------------------------- Kurven

def _moving_average(x: np.ndarray, window: int) -> np.ndarray:
    if window <= 1 or len(x) == 0:
        return x.copy()
    kernel = np.ones(window, dtype=np.float32) / window
    return np.convolve(x, kernel, mode="same")


def _rolling_baseline(x: np.ndarray, window: int = 300, block: int = 30) -> np.ndarray:
    """Gleitender Median (über Blöcke angenähert – schnell auch für 10-Stunden-Streams)."""
    n = len(x)
    if n == 0:
        return x
    n_blocks = int(np.ceil(n / block))
    block_medians = np.array([np.median(x[i * block:(i + 1) * block]) for i in range(n_blocks)])
    half = max(1, window // block // 2)
    smoothed = np.array(
        [np.median(block_medians[max(0, i - half):i + half + 1]) for i in range(n_blocks)]
    )
    centers = np.arange(n_blocks) * block + block / 2
    return np.interp(np.arange(n), centers, smoothed).astype(np.float32)


def _robust_z(x: np.ndarray) -> np.ndarray:
    resid = x - _rolling_baseline(x)
    mad = np.median(np.abs(resid - np.median(resid))) * 1.4826
    return resid / max(float(mad), 1e-3)


def excitement_curve(loudness: np.ndarray) -> np.ndarray:
    """Wie viel lauter als normal ist es gerade? (robuster z-Wert, leicht geglättet)"""
    if len(loudness) == 0:
        return loudness
    floor = np.percentile(loudness, 5)  # Stille zählt nicht als Normalpegel
    x = np.maximum(loudness, floor)
    resid = x - _rolling_baseline(x)
    mad = np.median(np.abs(resid - np.median(resid))) * 1.4826
    spread = max(float(mad), 1.0)  # mindestens 1 dB, sonst explodieren z-Werte bei Monoton-Ton
    return _moving_average(resid / spread, 3)


def highlight_curve(analysis: AudioAnalysis, focus: str = FOCUS_LAUGH) -> np.ndarray:
    """Kombinierte Kurve. Fokus „lachen“: Lach-Rhythmus zählt stark, reine Lautstärke weniger."""
    excite = excitement_curve(analysis.loudness)
    if focus != FOCUS_LAUGH or len(analysis.laugh) == 0:
        return excite
    n = min(len(excite), len(analysis.laugh))
    excite, laugh = excite[:n], analysis.laugh[:n]
    laugh_z = np.clip(_robust_z(_moving_average(laugh, 3)), 0, None)
    # Lachen zählt nur, wenn es auch hörbar ist (nicht leises Hintergrundgeräusch)
    audible = np.clip(excite + 1.0, 0, 3) / 3
    return (0.6 * excite + 1.4 * laugh_z * audible).astype(np.float32)


# ---------------------------------------------------------------- Auswahl

def snap_to_quiet(start: float, end: float, loudness: np.ndarray, reach: float = 3.0) -> tuple[float, float]:
    """Schnitt-Start/-Ende auf die leiseste Sekunde in der Nähe legen (Sprechpausen)."""
    n = len(loudness)
    if n == 0:
        return start, end

    def quietest(lo: float, hi: float, fallback: float) -> float:
        a, b = max(0, int(lo)), min(n, int(np.ceil(hi)))
        if b - a < 1:
            return fallback
        return float(a + int(np.argmin(loudness[a:b])))

    new_start = quietest(start - reach, start + 1.0, start)
    new_end = quietest(end - 1.0, end + reach, end - 1) + 1.0
    if new_end - new_start < (end - start) * 0.6:
        return start, end
    return max(0.0, new_start), min(float(n), new_end)


def pick_highlights(
    curve: np.ndarray,
    target_seconds: float,
    *,
    pre_roll: float = 20.0,
    post_roll: float = 8.0,
    max_clip: float = 75.0,
    min_score: float = 1.5,
    extend_threshold: float = 1.0,
    skip_start: float = 30.0,
    max_clips: int | None = None,
    loudness: np.ndarray | None = None,
) -> list[Highlight]:
    """Wählt die stärksten Momente, bis `target_seconds` erreicht ist. Chronologisch sortiert."""
    n = len(curve)
    if n == 0:
        return []
    order = np.argsort(curve)[::-1]
    taken = np.zeros(n, dtype=bool)
    picks: list[Highlight] = []
    total = 0.0
    skip = int(skip_start) if n > skip_start * 4 else 0
    for idx in order:
        idx = int(idx)
        score = float(curve[idx])
        if score < min_score or total >= target_seconds:
            break
        if max_clips is not None and len(picks) >= max_clips:
            break
        if idx < skip or taken[idx]:
            continue
        peak_end = idx  # verlängern, solange die Aufregung anhält (begrenzt)
        while peak_end + 1 < n and curve[peak_end + 1] >= extend_threshold and peak_end - idx < max_clip / 2:
            peak_end += 1
        start = max(0.0, idx - pre_roll)
        end = min(float(n), peak_end + 1 + post_roll)
        end = min(end, start + max_clip)
        if loudness is not None:
            start, end = snap_to_quiet(start, end, loudness)
        s_i, e_i = int(start), int(np.ceil(end))
        if taken[s_i:e_i].any():
            continue  # überlappt mit einem stärkeren Highlight
        taken[s_i:e_i] = True
        picks.append(Highlight(start, end, float(idx), score))
        total += end - start
    return sorted(picks, key=lambda h: h.start)


def pick_with_fallback(curve: np.ndarray, target_seconds: float, **kwargs) -> list[Highlight]:
    picks = pick_highlights(curve, target_seconds, **kwargs)
    if not picks and len(curve):
        # sehr gleichförmiger Stream: Schwelle senken, damit trotzdem etwas entsteht
        picks = pick_highlights(curve, target_seconds, **{**kwargs, "min_score": 0.5})
    return picks


def detect_highlights(
    audio_or_video: str | Path,
    target_seconds: float,
    duration: float | None = None,
    progress: Progress = None,
    focus: str = FOCUS_LAUGH,
    **kwargs,
) -> list[Highlight]:
    analysis = analyze_audio(audio_or_video, duration, progress)
    curve = highlight_curve(analysis, focus)
    return pick_with_fallback(curve, target_seconds, loudness=analysis.loudness, **kwargs)


def target_length(vod_seconds: float, wanted_minutes: float = 12) -> float:
    """10–15 Minuten-Video; bei kurzen VODs höchstens ~30 % des Materials."""
    return min(wanted_minutes * 60, max(60.0, vod_seconds * 0.3))
