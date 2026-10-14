"""Sicherheitsregel: Upload/Veröffentlichung nur, wenn jeder Clip freigegeben ist.

Geprüft werden alle Clips, die aktuell ausgewählt sind, UND alle Clips, die im
zuletzt exportierten Video tatsächlich stecken (Export-Manifest). Der Status wird
immer frisch aus dem Creator-Register gelesen.

Verarbeitung und lokaler Export sind davon ausdrücklich nicht betroffen.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .creators import OWN_VOD, STATUS_ALLOWED, CreatorRegistry
from .models import Project


class UploadBlocked(PermissionError):
    def __init__(self, blockers: list["Blocker"]):
        self.blockers = blockers
        lines = "\n".join(f"  - {b.describe()}" for b in blockers)
        super().__init__(f"Upload gesperrt – nicht freigegebene Clips:\n{lines}")


@dataclass(frozen=True)
class Blocker:
    clip_id: str
    clip_title: str
    creator_id: str
    creator_name: str
    status: str  # aktueller Freigabe-Status oder "unbekannt"
    origin: str  # "auswahl" oder "export"

    def describe(self) -> str:
        where = "im exportierten Video" if self.origin == "export" else "ausgewählt"
        return (
            f"Clip „{self.clip_title}“ ({self.clip_id}, {where}) von {self.creator_name}: "
            f"Freigabe-Status „{self.status}“"
        )


def _exported_clips(project: Project) -> list[dict]:
    export = project.last_export
    if not export:
        return []
    manifest = export.get("manifest")
    if manifest and Path(manifest).exists():
        return json.loads(Path(manifest).read_text(encoding="utf-8")).get("clips", [])
    return export.get("clips", [])


def find_blockers(project: Project, registry: CreatorRegistry) -> list[Blocker]:
    entries: list[tuple[str, str, str, str, str]] = []
    for c in project.selected_clips():
        entries.append((c.id, c.title, c.source.creator_id, c.source.creator_name, "auswahl"))
        for comp in c.companions:  # Splitscreen: jeder sichtbare Streamer braucht eine Freigabe
            entries.append((c.id, c.title, comp["creator_id"], comp["creator_name"], "auswahl"))
    for c in _exported_clips(project):
        entries.append((c["clip_id"], c["title"], c["creator_id"], c["creator_name"], "export"))
        for comp in c.get("companions", []):
            entries.append((c["clip_id"], c["title"], comp["creator_id"], comp["creator_name"], "export"))

    blockers: list[Blocker] = []
    seen: set[tuple[str, str]] = set()
    for clip_id, title, creator_id, creator_name, origin in entries:
        if (clip_id, creator_id) in seen or creator_id == OWN_VOD:
            continue
        creator = registry.get(creator_id)
        status = creator.permission_status if creator else "unbekannt (nicht im Register)"
        if status != STATUS_ALLOWED:
            seen.add((clip_id, creator_id))
            name = creator.name if creator else creator_name
            blockers.append(Blocker(clip_id, title, creator_id, name, status, origin))
    return blockers


def assert_upload_allowed(project: Project, registry: CreatorRegistry) -> None:
    blockers = find_blockers(project, registry)
    if blockers:
        raise UploadBlocked(blockers)
