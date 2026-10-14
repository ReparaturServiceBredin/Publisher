"""Mehrere VODs zeitlich übereinanderlegen (gemeinsame Streams, unterschiedliche POVs).

1. Grobe Ausrichtung über die Startzeit der Livesendungen (Twitch/YouTube-Metadaten).
2. Feinabgleich über die Tonspur: Streamer im selben Call hören sich gegenseitig, deshalb
   ähneln sich die Lautstärkeverläufe. Die Kreuzkorrelation der 10-Hz-Hüllkurven liefert
   den genauen Versatz (auf 0,1 s). Ist der Treffer nicht eindeutig, bleibt es bei (1).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

ENV_RATE = 10  # Hz


@dataclass
class SyncResult:
    offset: float  # Sekunden: Zeit im VOD = Zeit im Referenz-VOD + offset
    confidence: float  # Peak-Stärke der Korrelation (z-Wert); >= 6 gilt als sicher
    method: str  # "audio", "metadaten" oder "keine"


def _normalize(x: np.ndarray) -> np.ndarray:
    x = x.astype(np.float64)
    x = x - np.median(x)
    x = np.clip(x, -30, 30)
    # langsame Pegelwanderung entfernen, damit nur die Sprach-/Ereignismuster zählen
    k = ENV_RATE * 30
    if len(x) > k:
        x = x - np.convolve(x, np.ones(k) / k, mode="same")
    s = x.std()
    return x / s if s > 0 else x


def find_offset(
    ref_env: np.ndarray,
    other_env: np.ndarray,
    guess: float | None = None,
    search: float = 600.0,
) -> SyncResult:
    """Versatz von `other` gegenüber `ref` über FFT-Kreuzkorrelation der Hüllkurven."""
    if len(ref_env) < ENV_RATE * 30 or len(other_env) < ENV_RATE * 30:
        return SyncResult(guess or 0.0, 0.0, "metadaten" if guess is not None else "keine")
    a, b = _normalize(ref_env), _normalize(other_env)
    n = len(a) + len(b) - 1
    size = 1 << (n - 1).bit_length()
    corr = np.fft.irfft(np.fft.rfft(b, size) * np.conj(np.fft.rfft(a, size)), size)
    # corr[k] gehört zu Versatz k (b ist um k Samples „später“ im Material) – negative k am Ende
    lags = np.concatenate([np.arange(0, len(b)), np.arange(-(len(a) - 1), 0)])
    corr = np.concatenate([corr[: len(b)], corr[size - (len(a) - 1):]])
    # Überlappungslänge berücksichtigen (sonst gewinnen Randbereiche mit wenig Überlappung zufällig)
    # Anzahl gemeinsamer Samples für Versatz k: t in [max(0,-k), min(len(a), len(b)-k))
    overlap = np.minimum(len(a), len(b) - lags) - np.maximum(0, -lags)
    valid = overlap >= ENV_RATE * 60
    if guess is not None:
        valid &= np.abs(lags / ENV_RATE - guess) <= search
    if not valid.any():
        return SyncResult(guess or 0.0, 0.0, "metadaten" if guess is not None else "keine")
    score = np.where(valid, corr / np.maximum(overlap, 1), -np.inf)
    best = int(np.argmax(score))
    vals = score[valid]
    conf = float((score[best] - np.median(vals)) / (vals.std() + 1e-9))
    offset = float(lags[best]) / ENV_RATE
    if conf >= 6.0:
        return SyncResult(offset, conf, "audio")
    if guess is not None:
        return SyncResult(guess, conf, "metadaten")
    return SyncResult(offset, conf, "audio" if conf >= 4.0 else "keine")


def metadata_guess(ref_start_ts: float | None, other_start_ts: float | None) -> float | None:
    """Aus den Startzeitpunkten (Unix-Sekunden): Zeit im anderen VOD = Referenzzeit + (ref_start - other_start)."""
    if ref_start_ts is None or other_start_ts is None:
        return None
    return float(ref_start_ts - other_start_ts)
