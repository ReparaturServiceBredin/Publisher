"""Selbst-Update der Windows-.exe über GitHub-Releases (öffentliches Repo, kein Login nötig)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ._version import __version__

REPO = "ReparaturServiceBredin/Publisher"
ASSET_NAME = "Publisher.exe"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"


@dataclass
class Update:
    version: str
    url: str
    size: int


def parse_version(text: str) -> tuple[int, ...]:
    parts = []
    for p in text.lstrip("vV").split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def can_self_update() -> bool:
    return bool(getattr(sys, "frozen", False)) and os.name == "nt"


def check_for_update(current: str = __version__, timeout: float = 10) -> Update | None:
    req = urllib.request.Request(API_URL, headers={"Accept": "application/vnd.github+json", "User-Agent": "Publisher"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        release = json.load(resp)
    tag = release.get("tag_name", "")
    if parse_version(tag) <= parse_version(current):
        return None
    for asset in release.get("assets", []):
        if asset.get("name") == ASSET_NAME:
            return Update(tag.lstrip("v"), asset["browser_download_url"], int(asset.get("size", 0)))
    return None


def download_update(update: Update, progress: Callable[[float], None] | None = None) -> Path:
    exe = Path(sys.executable)
    target = exe.with_name(exe.stem + ".new.exe")
    try:
        target.touch()
    except OSError as exc:
        raise PermissionError(
            f"Kein Schreibrecht in {exe.parent}. Bitte Publisher.exe in einen eigenen Ordner "
            "legen, z. B. C:\\Publisher\\Publisher.exe."
        ) from exc
    req = urllib.request.Request(update.url, headers={"User-Agent": "Publisher"})
    with urllib.request.urlopen(req, timeout=60) as resp, target.open("wb") as fh:
        total = update.size or int(resp.headers.get("Content-Length", 0))
        done = 0
        while chunk := resp.read(1024 * 256):
            fh.write(chunk)
            done += len(chunk)
            if progress and total:
                progress(done / total)
    if update.size and target.stat().st_size != update.size:
        target.unlink(missing_ok=True)
        raise RuntimeError("Update-Download unvollständig")
    return target


def apply_update_and_restart(new_exe: Path) -> None:
    """Startet ein kleines Skript, das nach dem Beenden die .exe ersetzt und neu startet."""
    exe = Path(sys.executable)
    pid = os.getpid()
    script = Path(tempfile.gettempdir()) / f"publisher_update_{pid}.cmd"
    script.write_text(
        "@echo off\r\n"
        ":wait\r\n"
        f'tasklist /FI "PID eq {pid}" 2>nul | "%SystemRoot%\\System32\\find.exe" "{pid}" >nul && (ping -n 2 127.0.0.1 >nul & goto wait)\r\n'
        "set n=0\r\n"
        ":move\r\n"
        f'move /y "{new_exe}" "{exe}" >nul 2>&1 && goto start\r\n'
        "set /a n+=1\r\n"
        "if %n% GEQ 30 goto start\r\n"
        "ping -n 2 127.0.0.1 >nul\r\n"
        "goto move\r\n"
        ":start\r\n"
        f'start "" "{exe}"\r\n'
        'del "%~f0"\r\n',
        encoding="mbcs" if os.name == "nt" else "utf-8",
    )
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP (kein Fenster)
    subprocess.Popen(["cmd", "/c", str(script)], creationflags=flags, close_fds=True)
