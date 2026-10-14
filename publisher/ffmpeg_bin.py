"""Findet ffmpeg (System, oder das mitgelieferte aus imageio-ffmpeg) und startet es ohne Konsolenfenster."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from functools import lru_cache


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


@lru_cache(maxsize=1)
def ffmpeg_exe() -> str:
    env = os.environ.get("PUBLISHER_FFMPEG")
    if env:
        return env
    bundled = None
    try:
        import imageio_ffmpeg

        bundled = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    # In der .exe immer das mitgelieferte ffmpeg nehmen (bekannte Features)
    if is_frozen() and bundled:
        return bundled
    return shutil.which("ffmpeg") or bundled or _missing()


def _missing() -> str:
    raise RuntimeError("ffmpeg wurde nicht gefunden – bitte ffmpeg installieren oder 'pip install imageio-ffmpeg'.")


def popen_kwargs() -> dict:
    """Unter Windows kein schwarzes Konsolenfenster pro ffmpeg-Aufruf."""
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}
