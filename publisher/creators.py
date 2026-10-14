"""Creator-Register (creators.yaml).

Jeder Creator, dessen Material verarbeitet wird, steht hier mit seinem Freigabe-Status.
Der Status wird bei jedem Upload frisch aus der Datei gelesen – eine Änderung auf
"nicht erlaubt" sperrt also auch bereits exportierte Videos.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import yaml

STATUS_ALLOWED = "erlaubt"
STATUS_REQUESTED = "angefragt"
STATUS_DENIED = "nicht erlaubt"
PERMISSION_STATUSES = (STATUS_ALLOWED, STATUS_REQUESTED, STATUS_DENIED)

# Kennung für Material vom eigenen Kanal – braucht keine Freigabe.
OWN_VOD = "__eigen__"


def slugify(text: str) -> str:
    norm = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", norm).strip("-").lower()
    return slug or "creator"


def handle_from_url(url: str | None) -> str | None:
    """'https://www.twitch.tv/Beispiel/' -> 'beispiel'; YouTube '@handle' -> '@handle'."""
    if not url:
        return None
    parsed = urlparse(url if "://" in url else f"https://{url}")
    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        return None
    if "twitch.tv" in parsed.netloc:
        return parts[0].lower()
    return parts[-1] if parts[0] in ("c", "user", "channel") else parts[0]


@dataclass
class CreditRules:
    """Eigene Credit-Vorgaben eines Creators (alle optional)."""

    # Text, der als erster Satz der Beschreibung erscheinen muss (z. B. "Alle Clips von X – link")
    first_line: str = ""
    # Ersetzt die Standard-Einblendung "Clip von [Name] – twitch.tv/[name]"
    overlay_text: str = ""
    # Zusätzliche Zeile im Credit-Block jedes Clips dieses Creators
    extra_line: str = ""
    # Bevorzugter Link (z. B. Linktree), wird in der Creator-Liste zusätzlich genannt
    link: str = ""


@dataclass
class Creator:
    id: str
    name: str
    youtube_channel_url: str = ""
    twitch_channel_url: str = ""
    permission_status: str = STATUS_REQUESTED
    permission_proof: str = ""
    credit: CreditRules = field(default_factory=CreditRules)

    def __post_init__(self) -> None:
        if self.permission_status not in PERMISSION_STATUSES:
            raise ValueError(
                f"Creator {self.name!r}: permission_status muss einer von "
                f"{', '.join(PERMISSION_STATUSES)} sein, nicht {self.permission_status!r}"
            )

    @property
    def is_allowed(self) -> bool:
        return self.permission_status == STATUS_ALLOWED

    @property
    def twitch_handle(self) -> str | None:
        return handle_from_url(self.twitch_channel_url)

    def overlay_text(self) -> str:
        if self.credit.overlay_text:
            return self.credit.overlay_text
        if self.twitch_handle:
            return f"Clip von {self.name} – twitch.tv/{self.twitch_handle}"
        return f"Clip von {self.name}"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["credit"] = {k: v for k, v in data["credit"].items() if v}
        if not data["credit"]:
            del data["credit"]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Creator":
        data = dict(data)
        if not data.get("name"):
            raise ValueError(f"Creator ohne Namen in creators.yaml: {data!r}")
        data.setdefault("id", slugify(data["name"]))
        credit = data.pop("credit", None) or {}
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"Creator {data['name']!r}: unbekannte Felder {sorted(unknown)}")
        for key in ("youtube_channel_url", "twitch_channel_url", "permission_proof"):
            data[key] = data.get(key) or ""
        return cls(credit=CreditRules(**credit), **data)


class CreatorRegistry:
    def __init__(self, creators: list[Creator] | None = None, path: Path | None = None):
        self.path = path
        self._creators: dict[str, Creator] = {}
        for c in creators or []:
            self.add(c)

    def add(self, creator: Creator) -> None:
        if creator.id == OWN_VOD:
            raise ValueError(f"Die ID {OWN_VOD!r} ist für eigene VODs reserviert")
        if creator.id in self._creators:
            raise ValueError(f"Doppelte Creator-ID {creator.id!r} in creators.yaml")
        self._creators[creator.id] = creator

    def get(self, creator_id: str) -> Creator | None:
        return self._creators.get(creator_id)

    def __iter__(self):
        return iter(self._creators.values())

    def __len__(self) -> int:
        return len(self._creators)

    @classmethod
    def load(cls, path: str | Path) -> "CreatorRegistry":
        path = Path(path)
        if not path.exists():
            return cls([], path=path)
        with path.open(encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        entries = raw.get("creators", []) if isinstance(raw, dict) else raw
        return cls([Creator.from_dict(e) for e in entries or []], path=path)

    def save(self, path: str | Path | None = None) -> None:
        target = Path(path or self.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(
                {"creators": [c.to_dict() for c in self]},
                fh,
                allow_unicode=True,
                sort_keys=False,
            )
