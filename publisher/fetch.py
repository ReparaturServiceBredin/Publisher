"""VODs von Links laden (Twitch, YouTube, … – alles, was yt-dlp kann).

Sparsam: Erst nur die Tonspur laden (klein, reicht für die Erkennung), danach nur
die Abschnitte mit Highlights als Video. Klappt das nicht, wird das ganze VOD geladen.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .ffmpeg_bin import ffmpeg_exe

log = logging.getLogger(__name__)
Progress = Callable[[str, float], None] | None

VIDEO_FORMAT = "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b"
AUDIO_FORMAT = "ba/bestaudio/worst"


def is_url(text: str) -> bool:
    return text.strip().lower().startswith(("http://", "https://", "www."))


def _ydl(opts: dict):
    import yt_dlp

    base = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "ffmpeg_location": ffmpeg_exe(),
        "noplaylist": True,
        "retries": 10,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 4,
    }
    return yt_dlp.YoutubeDL({**base, **opts})


def _hook(progress: Progress, label: str):
    def hook(d):
        if not progress or d.get("status") != "downloading":
            return
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        if total:
            progress(label, min(d.get("downloaded_bytes", 0) / total, 1.0))
        elif d.get("fragment_count"):
            progress(label, min((d.get("fragment_index") or 0) / d["fragment_count"], 1.0))

    return hook


def fetch_info(url: str) -> dict:
    with _ydl({}) as ydl:
        return ydl.sanitize_info(ydl.extract_info(url, download=False))


def source_metadata(info: dict) -> dict:
    """Creator, Kanal-Links, Startzeit und Titel aus den yt-dlp-Metadaten."""
    extractor = (info.get("extractor_key") or info.get("extractor") or "").lower()
    name = info.get("uploader") or info.get("channel") or info.get("uploader_id") or "Unbekannt"
    twitch = youtube = ""
    if "twitch" in extractor:
        login = info.get("uploader_id") or name
        twitch = f"https://www.twitch.tv/{str(login).lower()}"
    elif "youtube" in extractor:
        youtube = info.get("channel_url") or info.get("uploader_url") or ""
    ts = info.get("release_timestamp") or info.get("timestamp")
    if ts:
        live_start = datetime.fromtimestamp(ts, tz=timezone.utc)
    elif info.get("upload_date"):
        live_start = datetime.strptime(info["upload_date"], "%Y%m%d").replace(tzinfo=timezone.utc)
    else:
        live_start = datetime.now(timezone.utc)
    return {
        "creator_name": str(name),
        "twitch_channel_url": twitch,
        "youtube_channel_url": youtube,
        "live_start": live_start,
        "vod_url": info.get("webpage_url") or info.get("original_url") or "",
        "stream_title": info.get("title") or "",
        "duration": float(info["duration"]) if info.get("duration") else None,
    }


def _downloaded_path(info: dict) -> Path:
    downloads = info.get("requested_downloads") or []
    if downloads and downloads[0].get("filepath"):
        return Path(downloads[0]["filepath"])
    raise RuntimeError("Download fehlgeschlagen – keine Datei erhalten")


def download_audio(url: str, dest: Path, progress: Progress = None) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    opts = {
        "format": AUDIO_FORMAT,
        "outtmpl": str(dest / "ton.%(ext)s"),
        "progress_hooks": [_hook(progress, "Lade Tonspur")],
    }
    with _ydl(opts) as ydl:
        return _downloaded_path(ydl.extract_info(url, download=True))


def download_full(url: str, dest: Path, progress: Progress = None) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    opts = {
        "format": VIDEO_FORMAT,
        "merge_output_format": "mp4",
        "outtmpl": str(dest / "vod.%(ext)s"),
        "progress_hooks": [_hook(progress, "Lade Video")],
    }
    with _ydl(opts) as ydl:
        return _downloaded_path(ydl.extract_info(url, download=True))


def download_sections(
    url: str, ranges: list[tuple[float, float]], dest: Path, progress: Progress = None
) -> list[dict]:
    """Lädt nur die angegebenen Bereiche. Gibt [{start, end, path}] in Original-VOD-Zeit zurück."""
    from yt_dlp.utils import download_range_func

    dest.mkdir(parents=True, exist_ok=True)
    segments = []
    for i, (start, end) in enumerate(ranges, start=1):
        label = f"Lade Abschnitt {i}/{len(ranges)}"
        opts = {
            "format": VIDEO_FORMAT,
            "merge_output_format": "mp4",
            "outtmpl": str(dest / f"abschnitt_{i:03d}.%(ext)s"),
            "download_ranges": download_range_func(None, [(start, end)]),
            "force_keyframes_at_cuts": True,  # genaue Schnittpunkte, damit die Zeiten stimmen
            "progress_hooks": [_hook(progress, label)],
        }
        if progress:
            progress(label, 0.0)
        with _ydl(opts) as ydl:
            path = _downloaded_path(ydl.extract_info(url, download=True))
        segments.append({"start": float(start), "end": float(end), "path": str(path)})
    return segments
