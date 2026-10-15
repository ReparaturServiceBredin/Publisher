"""Gemeinsame Farben und Bausteine der Oberfläche."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import customtkinter as ctk

ACCENT = "#7C3AED"
ACCENT_HOVER = "#6D28D9"
BG = "#0F0F14"
PANEL = "#17171F"
CARD = "#1F1F2A"
MUTED = "#9A9AB0"
OK = "#22C55E"
WARN = "#F59E0B"
BAD = "#EF4444"

VIDEO_TYPES = [("Videos", "*.mp4 *.mkv *.mov *.flv *.ts *.webm *.avi *.m4v"), ("Alle Dateien", "*.*")]
YT_PRIVACY = {"Privat": "private", "Nicht gelistet": "unlisted", "Öffentlich": "public"}
TT_PRIVACY = {
    "SELF_ONLY": "Nur ich", "MUTUAL_FOLLOW_FRIENDS": "Freunde",
    "FOLLOWER_OF_CREATOR": "Follower", "PUBLIC_TO_EVERYONE": "Öffentlich",
}


def open_path(path: Path) -> None:
    if os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def font(size: int = 13, weight: str = "normal") -> ctk.CTkFont:
    return ctk.CTkFont(family="Segoe UI", size=size, weight=weight)


def card(master, **kw) -> ctk.CTkFrame:
    return ctk.CTkFrame(master, fg_color=CARD, corner_radius=14, **kw)


def accent_button(master, text, command, **kw) -> ctk.CTkButton:
    return ctk.CTkButton(master, text=text, command=command, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                         corner_radius=10, font=font(13, "bold"), **kw)


def ghost_button(master, text, command, **kw) -> ctk.CTkButton:
    return ctk.CTkButton(master, text=text, command=command, fg_color="transparent", border_width=1,
                         border_color="#3A3A4A", hover_color="#2A2A38", corner_radius=10, font=font(12), **kw)
