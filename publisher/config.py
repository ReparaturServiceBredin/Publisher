"""Einstellungen aus config.yaml (optional) mit sinnvollen Standardwerten."""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

DEFAULTS: dict = {
    "data_dir": "data",
    "creators_file": "creators.yaml",
    "timezone": "Europe/Berlin",
    "own_channel": {
        "name": "Mein Kanal",
        "youtube_channel_url": "",
        "twitch_channel_url": "",
    },
    "llm": {
        "enabled": True,
        "model": "claude-opus-5-5",
        "effort": "low",
        # Ordner mit Beispiel-Beschreibungen der Referenz-Creator (*.txt / *.md)
        "style_reference_dir": "style_references",
    },
    "youtube": {
        "client_secret_file": "client_secret.json",
        "token_file": "data/token.json",
        "default_category_id": "20",  # Gaming
        "default_tags": [],
        "default_language": "de",
    },
    "overlay": {
        "duration": 3.0,
        "fontsize": 42,
        "font": "Sans",
        "short_full_duration": True,
    },
    "video": {"width": 1920, "height": 1080, "fps": 30},
    "short_video": {"width": 1080, "height": 1920, "fps": 30},
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


@dataclass
class Settings:
    raw: dict = field(default_factory=lambda: copy.deepcopy(DEFAULTS))
    base_dir: Path = field(default_factory=Path.cwd)

    def path(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else self.base_dir / p

    @property
    def data_dir(self) -> Path:
        return self.path(self.raw["data_dir"])

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    @property
    def upload_log_file(self) -> Path:
        return self.data_dir / "upload_log.jsonl"

    @property
    def creators_file(self) -> Path:
        return self.path(self.raw["creators_file"])

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.raw["timezone"])

    def __getitem__(self, key):
        return self.raw[key]


def load_settings(config_path: str | os.PathLike | None = None) -> Settings:
    """Lädt config.yaml aus dem aktuellen Ordner (oder $PUBLISHER_CONFIG)."""
    path = Path(config_path or os.environ.get("PUBLISHER_CONFIG", "config.yaml"))
    raw = {}
    if path.exists():
        with path.open(encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    return Settings(raw=_merge(DEFAULTS, raw), base_dir=path.resolve().parent)


def app_home() -> Path:
    """Arbeitsordner der Desktop-App: C:\\Users\\<Name>\\Publisher (oder $PUBLISHER_HOME)."""
    home = Path(os.environ.get("PUBLISHER_HOME") or Path.home() / "Publisher")
    home.mkdir(parents=True, exist_ok=True)
    return home


def load_app_settings() -> Settings:
    return load_settings(app_home() / "config.yaml")
