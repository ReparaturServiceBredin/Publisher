"""Selbst-Update der Windows-.exe über GitHub-Releases (öffentliches Repo, kein Login nötig)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
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


def clean_restart_env() -> dict:
    """Umgebung für den Neustart einer PyInstaller-exe.

    Die alte exe vererbt sonst interne Variablen (u. a. ihren temporären Entpack-Ordner) an die neue –
    die neue findet den Ordner nicht mehr und beendet sich sofort ohne Meldung. PyInstaller ≥ 6.9 erkennt
    PYINSTALLER_RESET_ENVIRONMENT=1 und startet dann wie per Doppelklick."""
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith("_PYI") or k.startswith("_MEI") or k in ("TCL_LIBRARY", "TK_LIBRARY"))}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


UPDATE_SELFTEST_FILE = "_update_selftest.json"


def swap_in_new_exe(new_exe: Path) -> Path:
    """Neue Version sofort an die Stelle der laufenden legen (Windows erlaubt das Umbenennen einer laufenden
    .exe). Damit ist das Update installiert, selbst wenn der Neustart scheitert. Aufräumen: cleanup_old_exes()."""
    exe = Path(sys.executable)
    old = exe.with_name(exe.stem + ".old.exe")
    try:
        old.unlink(missing_ok=True)
    except OSError:
        old = exe.with_name(f"{exe.stem}.old{int(time.time())}.exe")
    os.replace(exe, old)
    try:
        os.replace(new_exe, exe)
    except OSError:
        os.replace(old, exe)
        raise
    return exe


def restart_exe(exe: Path) -> str:
    """Neue Instanz starten, die das Beenden der alten überlebt (PyInstaller-exe laufen in einem Windows-Job,
    der beim Beenden alle gestarteten Prozesse mitnimmt → CREATE_BREAKAWAY_FROM_JOB, sonst über den Explorer)."""
    env = clean_restart_env()
    DETACHED, NEW_GROUP, BREAKAWAY = 0x00000008, 0x00000200, 0x01000000
    try:
        subprocess.Popen([str(exe)], creationflags=DETACHED | NEW_GROUP | BREAKAWAY, env=env, close_fds=True,
                         cwd=str(exe.parent))
        return "direkt"
    except OSError:
        subprocess.Popen(["explorer.exe", str(exe)], env=env, close_fds=True)
        return "explorer"


def apply_update_and_restart(new_exe: Path) -> None:
    """Neue Version eintauschen und neu starten (ohne Hilfsskript, das Windows mitbeenden könnte)."""
    restart_exe(swap_in_new_exe(new_exe))


def cleanup_old_exes() -> None:
    if not getattr(sys, "frozen", False):
        return
    exe = Path(sys.executable)
    for old in exe.parent.glob(exe.stem + ".old*.exe"):
        try:
            old.unlink()
        except OSError:
            pass


def update_selftest(result: Path) -> int:
    """Build-Test auf GitHub: Update mit einer Kopie von sich selbst echt durchspielen."""
    import shutil

    exe = Path(sys.executable)
    new = exe.with_name(exe.stem + ".new.exe")
    shutil.copy2(exe, new)
    (exe.parent / UPDATE_SELFTEST_FILE).write_text(json.dumps({"result": str(result)}), encoding="utf-8")
    swap_in_new_exe(new)
    result.with_suffix(".how").write_text(restart_exe(exe), encoding="utf-8")
    return 0


def answer_update_selftest() -> bool:
    job = Path(sys.executable).parent / UPDATE_SELFTEST_FILE
    if not getattr(sys, "frozen", False) or not job.exists():
        return False
    try:
        result = Path(json.loads(job.read_text(encoding="utf-8"))["result"])
        old_left = [p.name for p in Path(sys.executable).parent.glob("*.old*.exe")]
        result.write_text(f"OK version={__version__} old_files={old_left}", encoding="utf-8")
    finally:
        job.unlink(missing_ok=True)
    return True

