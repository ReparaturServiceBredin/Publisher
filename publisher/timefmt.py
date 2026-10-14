"""Zeitstempel parsen und formatieren (VOD-Offsets, Kapitelmarken, deutsche Datumsangaben)."""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

_TS_RE = re.compile(r"^\s*(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)\s*$")


def parse_timestamp(value: str | int | float) -> float:
    """'1:02:03', '62:03', '3723' oder 3723.5 -> Sekunden."""
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        text = str(value).strip()
        m = _TS_RE.match(text)
        if m:
            h, mnt, s = m.groups()
            seconds = int(h or 0) * 3600 + int(mnt) * 60 + float(s)
        else:
            try:
                seconds = float(text)
            except ValueError as exc:
                raise ValueError(f"Ungültiger Zeitstempel: {value!r}") from exc
    if seconds < 0:
        raise ValueError(f"Zeitstempel darf nicht negativ sein: {value!r}")
    return seconds


def format_hms(seconds: float) -> str:
    """Immer H:MM:SS, z. B. für Positionen im Original-VOD."""
    total = int(seconds)
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}"


def format_chapter(seconds: float, long_video: bool = False) -> str:
    """YouTube-Kapitelformat: MM:SS, bzw. H:MM:SS wenn das Video >= 1 Stunde lang ist."""
    total = int(seconds)
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    if long_video or h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def format_twitch_t(seconds: float) -> str:
    total = int(seconds)
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    return f"{h}h{m}m{s}s"


def format_de_datetime(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y, %H:%M Uhr")


def format_de_date(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y")


def vod_link_at(url: str | None, seconds: float) -> str | None:
    """Fügt dem Original-VOD-Link den Startzeitpunkt hinzu, wenn die Plattform das unterstützt."""
    if not url:
        return None
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    query = [(k, v) for k, v in parse_qsl(parsed.query) if k != "t"]
    if "twitch.tv" in host and "/videos/" in parsed.path:
        query.append(("t", format_twitch_t(seconds)))
    elif "youtube.com" in host or "youtu.be" in host:
        query.append(("t", f"{int(seconds)}s"))
    else:
        return url
    return urlunparse(parsed._replace(query=urlencode(query)))
