"""Upload-Log (JSON Lines): welches Video, welche Quellen, wann hochgeladen/veröffentlicht."""

from __future__ import annotations

import json
from pathlib import Path

from .models import now_utc


def append(log_file: Path, event: str, **data) -> dict:
    entry = {"at": now_utc().isoformat(), "event": event, **data}
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def read(log_file: Path, project: str | None = None) -> list[dict]:
    if not log_file.exists():
        return []
    entries = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    if project:
        entries = [e for e in entries if e.get("project") == project]
    return entries
