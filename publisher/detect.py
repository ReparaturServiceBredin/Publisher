"""Automatische Highlight-Erkennung über die Tonspur.

Idee: In Streams sind Highlights fast immer *lauter als üblich* – Schreien, Lachen,
Jubel, Explosionen. Wir messen die Lautstärke pro Sekunde, vergleichen sie mit dem
gleitenden Normalpegel der letzten Minuten (so stören leise oder laute Streamphasen
nicht) und nehmen die stärksten Ausreißer. Jeder Treffer bekommt Vorlauf (was führte
zur Reaktion?) und Nachlauf und wird verlängert, solange es laut bleibt.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from .ffmpeg_bin import ffmpeg_exe, popen_kwargs

SAMPLE_RATE = 8000
Progress = Callable[[str, float], None] | None


@dataclass
class Highlight:
    start: float
    end: float
    peak: float
    score: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def loudness_per_second(path: str | Path, duration: float | None = None, progress: Progress = None) -> np.ndarray:
    """Lautstärke in dB pro Sekunde (dekodiert die Tonspur einmal, streamend)."""
    cmd = [
        ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", str(path),
        "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **popen_kwargs())
    values: list[float] = []
    chunk_bytes = SAMPLE_RATE * 2 * 60  # eine Minute pro Lesevorgang
    rest = b""
    assert proc.stdout is not None
    while True:
        data = proc.stdout.read(chunk_bytes)
        if not data:
            break
        data = rest + data
        usable = len(data) - len(data) % (SAMPLE_RATE * 2)
        rest = data[usable:]
        samples = np.frombuffer(data[:usable], dtype=np.int16).astype(np.float32).reshape(-1, SAMPLE_RATE)
        rms = np.sqrt(np.mean(samples * samples, axis=1)) / 32768.0
        values.extend((20 * np.log10(rms + 1e-6)).tolist())
        if progress and duration:
            progress("Analysiere Ton", min(len(values) / duration, 1.0))
    err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
    if proc.wait() != 0 and not values:
        raise RuntimeError(f"Tonspur konnte nicht gelesen werden: {err.strip()[-300:]}")
    return np.array(values, dtype=np.float32)


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


def excitement_curve(loudness: np.ndarray) -> np.ndarray:
    """Wie viel lauter als normal ist es gerade? (robuster z-Wert, leicht geglättet)"""
    if len(loudness) == 0:
        return loudness
    # Stille (Pausen, "Gleich geht's los"-Bildschirm) nicht als Normalpegel zählen
    floor = np.percentile(loudness, 5)
    x = np.maximum(loudness, floor)
    resid = x - _rolling_baseline(x)
    mad = np.median(np.abs(resid - np.median(resid))) * 1.4826
    spread = max(float(mad), 1.0)  # mindestens 1 dB, sonst explodieren z-Werte bei Monoton-Ton
    return _moving_average(resid / spread, 3)


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
        # verlängern, solange die Aufregung anhält (begrenzt)
        peak_end = idx
        while peak_end + 1 < n and curve[peak_end + 1] >= extend_threshold and peak_end - idx < max_clip / 2:
            peak_end += 1
        start = max(0.0, idx - pre_roll)
        end = min(float(n), peak_end + 1 + post_roll)
        end = min(end, start + max_clip)
        s_i, e_i = int(start), int(np.ceil(end))
        if taken[s_i:e_i].any():
            # überlappt mit einem stärkeren Highlight – zusammenlegen statt doppelt nehmen
            continue
        taken[s_i:e_i] = True
        picks.append(Highlight(start, end, float(idx), score))
        total += end - start
    return sorted(picks, key=lambda h: h.start)


def detect_highlights(
    audio_or_video: str | Path,
    target_seconds: float,
    duration: float | None = None,
    progress: Progress = None,
    **kwargs,
) -> list[Highlight]:
    loud = loudness_per_second(audio_or_video, duration, progress)
    curve = excitement_curve(loud)
    picks = pick_highlights(curve, target_seconds, **kwargs)
    if not picks and len(curve):
        # sehr gleichförmiger Stream: Schwelle senken, damit trotzdem etwas entsteht
        kwargs = {**kwargs, "min_score": 0.5}
        picks = pick_highlights(curve, target_seconds, **kwargs)
    return picks


def target_length(vod_seconds: float, wanted_minutes: float = 12) -> float:
    """10–15 Minuten-Video; bei kurzen VODs höchstens ~30 % des Materials."""
    return min(wanted_minutes * 60, max(60.0, vod_seconds * 0.3))
