"""Projekt-, VOD- und Clip-Modelle inklusive Quellen-Tracking.

Grundregel: Die Quelle eines Clips (`ClipSource`) wird beim Anlegen des Kandidaten
einmalig festgehalten und ist unveränderlich. Die Review-UI darf nur Titel, Trim,
Status und Reihenfolge ändern (`Candidate.EDITABLE_FIELDS`). Verworfene Clips werden
nicht gelöscht, sondern nur als "verworfen" markiert. Jede Änderung landet in
`Candidate.history`.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .creators import OWN_VOD

CLIP_CANDIDATE = "kandidat"
CLIP_SELECTED = "ausgewählt"
CLIP_REJECTED = "verworfen"
CLIP_STATUSES = (CLIP_CANDIDATE, CLIP_SELECTED, CLIP_REJECTED)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("Zeitangaben müssen eine Zeitzone haben")
    return dt.isoformat()


def _short_id() -> str:
    return uuid.uuid4().hex[:8]


@dataclass
class Vod:
    id: str
    path: str
    creator_id: str  # OWN_VOD für eigene VODs
    creator_name: str  # Snapshot zum Importzeitpunkt
    live_start: str  # ISO-Zeitpunkt, an dem die Live-Aufnahme begann (mit Zeitzone)
    vod_url: str = ""
    stream_title: str = ""
    duration: float | None = None
    imported_at: str = field(default_factory=lambda: _iso(now_utc()))
    # Nur teilweise heruntergeladen: [{"start": s, "end": s, "path": "..."}] (Zeiten im Original-VOD)
    segments: list[dict] = field(default_factory=list)

    def source_for(self, start: float, end: float) -> tuple[str, float]:
        """Datei + Offset, aus der der Bereich [start, end] des Original-VODs geschnitten werden kann."""
        for seg in self.segments:
            if seg["start"] <= start + 0.01 and end <= seg["end"] + 0.01:
                return seg["path"], seg["start"]
        if self.path and Path(self.path).exists():
            return self.path, 0.0
        raise ProjectError(
            f"Der Bereich {start:.0f}s–{end:.0f}s von {self.id} wurde nicht heruntergeladen."
        )

    @property
    def is_own(self) -> bool:
        return self.creator_id == OWN_VOD

    @property
    def live_start_dt(self) -> datetime:
        return datetime.fromisoformat(self.live_start)


@dataclass(frozen=True)
class ClipSource:
    """Unveränderliche Herkunft eines Clips, festgehalten bei der Erkennung."""

    vod_id: str
    creator_id: str
    creator_name: str
    vod_url: str
    stream_title: str
    live_start: str  # Beginn der Livesendung
    vod_start: float  # Sekunden im Original-VOD
    vod_end: float
    live_clip_start: str  # echte Uhrzeit der Live-Aufnahme
    live_clip_end: str
    recorded_at: str  # wann der Kandidat angelegt wurde

    @classmethod
    def from_vod(cls, vod: Vod, start: float, end: float) -> "ClipSource":
        live = vod.live_start_dt
        return cls(
            vod_id=vod.id,
            creator_id=vod.creator_id,
            creator_name=vod.creator_name,
            vod_url=vod.vod_url,
            stream_title=vod.stream_title,
            live_start=vod.live_start,
            vod_start=float(start),
            vod_end=float(end),
            live_clip_start=_iso(live + timedelta(seconds=start)),
            live_clip_end=_iso(live + timedelta(seconds=end)),
            recorded_at=_iso(now_utc()),
        )


@dataclass
class Candidate:
    id: str
    title: str
    source: ClipSource
    trim_start: float
    trim_end: float
    status: str = CLIP_CANDIDATE
    order: int = 0
    # Position im fertigen Highlight-Video, gesetzt beim Export
    highlight_start: float | None = None
    highlight_end: float | None = None
    history: list[dict] = field(default_factory=list)
    # Mehrere POVs: weitere Streamer, die im Splitscreen gleichzeitig zu sehen sind.
    # Jeder Eintrag: vod_id, creator_id, creator_name, vod_url, live_start, offset
    # (Zeit im Companion-VOD = trim_start + offset). Wird wie die Quelle nie verändert.
    companions: list[dict] = field(default_factory=list)
    moment: int | None = None  # gleiche Nummer = gleiche Situation aus verschiedenen POVs

    EDITABLE_FIELDS = ("title", "trim_start", "trim_end", "status", "order")

    @property
    def creator_id(self) -> str:
        return self.source.creator_id

    @property
    def is_own(self) -> bool:
        return self.source.creator_id == OWN_VOD

    @property
    def duration(self) -> float:
        return self.trim_end - self.trim_start

    def live_time_at(self, vod_seconds: float) -> datetime:
        return datetime.fromisoformat(self.source.live_start) + timedelta(seconds=vod_seconds)

    @property
    def live_start_dt(self) -> datetime:
        """Echte Uhrzeit, zu der der (getrimmte) Clip live lief."""
        return self.live_time_at(self.trim_start)

    @property
    def live_end_dt(self) -> datetime:
        return self.live_time_at(self.trim_end)

    def provenance(self) -> dict:
        """Vollständige Quellenangabe – wird in Export-Manifest und Upload-Log geschrieben."""
        return {
            "clip_id": self.id,
            "title": self.title,
            "creator_id": self.source.creator_id,
            "creator_name": self.source.creator_name,
            "vod_id": self.source.vod_id,
            "vod_url": self.source.vod_url,
            "stream_title": self.source.stream_title,
            "live_start": self.source.live_start,
            "vod_start": self.trim_start,
            "vod_end": self.trim_end,
            "live_clip_start": _iso(self.live_start_dt),
            "live_clip_end": _iso(self.live_end_dt),
            "highlight_start": self.highlight_start,
            "highlight_end": self.highlight_end,
            "original_detection": asdict(self.source),
            "companions": [
                {**c, "vod_start": self.trim_start + c["offset"], "vod_end": self.trim_end + c["offset"]}
                for c in self.companions
            ],
            "moment": self.moment,
        }

    def to_dict(self) -> dict:
        data = asdict(self)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Candidate":
        data = dict(data)
        data["source"] = ClipSource(**data["source"])
        return cls(**data)


class ProjectError(ValueError):
    pass


_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass
class ProjectMeta:
    title: str = ""
    tags: list[str] = field(default_factory=list)
    category_id: str = ""
    is_short: bool = False
    thumbnail: str = ""
    intro_text: str = ""  # LLM-Intro, in der Review-UI editierbar
    publish_at: str = ""  # optional geplanter Veröffentlichungszeitpunkt (ISO)


@dataclass
class Project:
    name: str
    vods: dict[str, Vod] = field(default_factory=dict)
    candidates: list[Candidate] = field(default_factory=list)
    meta: ProjectMeta = field(default_factory=ProjectMeta)
    exports: list[dict] = field(default_factory=list)
    uploads: list[dict] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: _iso(now_utc()))
    path: Path | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not _NAME_RE.match(self.name):
            raise ProjectError(
                f"Ungültiger Projektname {self.name!r} (erlaubt: Buchstaben, Ziffern, - _ .)"
            )

    # ---------- VODs ----------
    def add_vod(
        self,
        path: str,
        creator_id: str,
        creator_name: str,
        live_start: datetime,
        vod_url: str = "",
        stream_title: str = "",
        duration: float | None = None,
    ) -> Vod:
        vod_id = f"vod-{len(self.vods) + 1}-{_short_id()}"
        vod = Vod(
            id=vod_id,
            path=str(path),
            creator_id=creator_id,
            creator_name=creator_name,
            live_start=_iso(live_start),
            vod_url=vod_url or "",
            stream_title=stream_title or "",
            duration=duration,
        )
        self.vods[vod_id] = vod
        return vod

    # ---------- Kandidaten ----------
    def add_candidate(self, vod_id: str, start: float, end: float, title: str = "") -> Candidate:
        vod = self.vods.get(vod_id)
        if vod is None:
            raise ProjectError(f"Unbekanntes VOD {vod_id!r}")
        self._check_range(vod, start, end)
        cand = Candidate(
            id=f"clip-{_short_id()}",
            title=title.strip() or f"Clip {len(self.candidates) + 1}",
            source=ClipSource.from_vod(vod, start, end),
            trim_start=float(start),
            trim_end=float(end),
            order=len(self.candidates),
        )
        self.candidates.append(cand)
        return cand

    def candidate(self, clip_id: str) -> Candidate:
        for c in self.candidates:
            if c.id == clip_id:
                return c
        raise ProjectError(f"Unbekannter Clip {clip_id!r}")

    @staticmethod
    def _check_range(vod: Vod, start: float, end: float) -> None:
        if start < 0 or end <= start:
            raise ProjectError("Clip-Ende muss nach dem Clip-Start liegen (Start >= 0)")
        if vod.duration is not None and end > vod.duration + 0.5:
            raise ProjectError(
                f"Clip-Ende ({end:.1f}s) liegt hinter dem VOD-Ende ({vod.duration:.1f}s)"
            )

    def apply_review_edit(self, clip_id: str, changes: dict) -> Candidate:
        """Einziger Weg, einen Clip zu bearbeiten. Die Quelle bleibt dabei unangetastet."""
        cand = self.candidate(clip_id)
        forbidden = set(changes) - set(Candidate.EDITABLE_FIELDS)
        if forbidden:
            raise ProjectError(
                f"Nicht editierbar: {', '.join(sorted(forbidden))}. Quellen-Daten sind schreibgeschützt."
            )
        new = {k: getattr(cand, k) for k in Candidate.EDITABLE_FIELDS}
        for key, value in changes.items():
            if key in ("trim_start", "trim_end"):
                value = float(value)
            elif key == "order":
                value = int(value)
            elif key == "status" and value not in CLIP_STATUSES:
                raise ProjectError(f"Ungültiger Status {value!r}")
            elif key == "title":
                value = str(value).strip()
                if not value:
                    raise ProjectError("Titel darf nicht leer sein")
            new[key] = value
        if (new["trim_start"], new["trim_end"]) != (cand.trim_start, cand.trim_end):
            self._check_range(self.vods[cand.source.vod_id], new["trim_start"], new["trim_end"])

        stamp = _iso(now_utc())
        for key in Candidate.EDITABLE_FIELDS:
            old = getattr(cand, key)
            if old != new[key]:
                cand.history.append({"at": stamp, "field": key, "old": old, "new": new[key]})
                setattr(cand, key, new[key])
        return cand

    def selected_clips(self) -> list[Candidate]:
        clips = [c for c in self.candidates if c.status == CLIP_SELECTED]
        return sorted(clips, key=lambda c: (c.order, c.source.vod_id, c.trim_start))

    # ---------- Persistenz ----------
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "created_at": self.created_at,
            "meta": asdict(self.meta),
            "vods": {k: asdict(v) for k, v in self.vods.items()},
            "candidates": [c.to_dict() for c in self.candidates],
            "exports": self.exports,
            "uploads": self.uploads,
        }

    @classmethod
    def from_dict(cls, data: dict, path: Path | None = None) -> "Project":
        return cls(
            name=data["name"],
            created_at=data.get("created_at") or _iso(now_utc()),
            meta=ProjectMeta(**data.get("meta", {})),
            vods={k: Vod(**v) for k, v in data.get("vods", {}).items()},
            candidates=[Candidate.from_dict(c) for c in data.get("candidates", [])],
            exports=data.get("exports", []),
            uploads=data.get("uploads", []),
            path=path,
        )

    @property
    def dir(self) -> Path:
        if self.path is None:
            raise ProjectError("Projekt wurde noch nicht gespeichert")
        return self.path.parent

    def save(self, path: Path | None = None) -> Path:
        target = Path(path or self.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, target)
        self.path = target
        return target

    @classmethod
    def load(cls, path: Path) -> "Project":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data, path=Path(path))

    @property
    def last_export(self) -> dict | None:
        return self.exports[-1] if self.exports else None

    @property
    def last_upload(self) -> dict | None:
        return self.uploads[-1] if self.uploads else None


class ProjectStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def path_for(self, name: str) -> Path:
        return self.root / name / "project.json"

    def exists(self, name: str) -> bool:
        return self.path_for(name).exists()

    def create(self, name: str) -> Project:
        if self.exists(name):
            raise ProjectError(f"Projekt {name!r} existiert bereits")
        project = Project(name=name)
        project.save(self.path_for(name))
        return project

    def load(self, name: str) -> Project:
        Project(name=name)  # validiert den Namen (kein Pfad-Traversal)
        if not self.exists(name):
            raise ProjectError(f"Projekt {name!r} nicht gefunden")
        return Project.load(self.path_for(name))

    def load_or_create(self, name: str) -> Project:
        return self.load(name) if self.exists(name) else self.create(name)

    def list(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(p.parent.name for p in self.root.glob("*/project.json"))
