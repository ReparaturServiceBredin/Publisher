"""Videobox: alle fertigen Videos/Clips anzeigen und direkt zu YouTube oder TikTok hochladen.

Jeder Upload prüft vorher die Freigaben (gate.py) – auch hier gilt: Clips von Creatorn ohne
Status „erlaubt“ werden nicht hochgeladen.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import uploadlog
from .config import Settings
from .creators import CreatorRegistry
from .description import build_tags, credit_info, sanitize, _original_vod_line
from .ffmpeg_bin import ffmpeg_exe, popen_kwargs
from .gate import Blocker, UploadBlocked, find_blockers
from .models import Project, ProjectStore
from .timefmt import format_de_datetime

KIND_VIDEO = "video"
KIND_CLIP = "clip"
KIND_VERTICAL = "vertical"


@dataclass
class MediaFile:
    kind: str  # video | clip | vertical
    path: Path
    title: str
    clip_id: str | None = None

    @property
    def label(self) -> str:
        return {"video": "Highlight-Video", "clip": "Clip", "vertical": "Hochformat"}[self.kind]


@dataclass
class BoxItem:
    folder: Path
    info: dict
    files: list[MediaFile] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.info.get("title") or self.folder.name

    @property
    def video(self) -> MediaFile:
        return self.files[0]

    @property
    def created(self) -> str:
        try:
            return datetime.fromisoformat(self.info["created_at"]).astimezone().strftime("%d.%m.%Y %H:%M")
        except (KeyError, ValueError):
            return ""

    def uploads_for(self, path: Path) -> list[dict]:
        rel = str(path.relative_to(self.folder)).replace("\\", "/")
        return [u for u in self.info.get("uploads", []) if u.get("file") == rel]

    def save(self) -> None:
        (self.folder / "info.json").write_text(json.dumps(self.info, ensure_ascii=False, indent=2), encoding="utf-8")


def list_items(root: Path) -> list[BoxItem]:
    items = []
    for info_file in sorted(root.glob("*/info.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            info = json.loads(info_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        folder = info_file.parent
        files = [MediaFile(KIND_VIDEO, folder / info["video"], info.get("title", folder.name))]
        for c in info.get("clips", []):
            files.append(MediaFile(KIND_CLIP, folder / c["file"], c["title"], c["clip_id"]))
            if c.get("vertical"):
                files.append(MediaFile(KIND_VERTICAL, folder / c["vertical"], c["title"], c["clip_id"]))
        items.append(BoxItem(folder, info, [f for f in files if f.path.exists()]))
    return [i for i in items if i.files]


def thumbnail(media: Path, size: int = 320) -> Path:
    """Vorschaubild (wird einmal erzeugt und neben dem Video gespeichert)."""
    thumb = media.with_suffix(".thumb.jpg")
    if not thumb.exists():
        subprocess.run(
            [ffmpeg_exe(), "-y", "-loglevel", "error", "-ss", "3", "-i", str(media), "-frames:v", "1",
             "-vf", f"scale={size}:-2", str(thumb)],
            capture_output=True, **popen_kwargs(),
        )
    return thumb


# ---------------------------------------------------------------- Texte + Freigaben

def _project(item: BoxItem, settings: Settings) -> Project:
    return ProjectStore(settings.projects_dir).load(item.info["project"])


def blockers_for(item: BoxItem, media: MediaFile, settings: Settings) -> list[Blocker]:
    project = _project(item, settings)
    registry = CreatorRegistry.load(settings.creators_file)
    blockers = find_blockers(project, registry)
    if media.kind == KIND_VIDEO:
        return blockers
    return [b for b in blockers if b.clip_id == media.clip_id]


def default_texts(item: BoxItem, media: MediaFile, settings: Settings, platform: str) -> tuple[str, str]:
    """Titel + Beschreibung mit Credits. Für Einzelclips/Hochformat: kurzer Credit (+ #Shorts bei YouTube)."""
    if media.kind == KIND_VIDEO:
        desc_file = item.folder / item.info.get("description_file", "Beschreibung.txt")
        text = desc_file.read_text(encoding="utf-8") if desc_file.exists() else ""
        title, _, body = text.partition("\n\n")
        return (title.strip() or item.title)[:100], body.strip()
    project = _project(item, settings)
    registry = CreatorRegistry.load(settings.creators_file)
    clip = project.candidate(media.clip_id)
    people = [(clip.creator_id, clip.source.creator_name)] + [(c["creator_id"], c["creator_name"]) for c in clip.companions]
    lines = []
    for creator_id, name in people:
        info = credit_info(creator_id, name, registry, settings)
        if info.is_own:
            continue
        lines.append(f"Clip von {info.name} – live am {format_de_datetime(clip.live_start_dt)}")
        if info.link_line():
            lines.append(info.link_line())
    if not clip.is_own:
        lines.insert(1 if lines else 0, _original_vod_line(clip))
    short = media.kind == KIND_VERTICAL
    title = f"{project.meta.title or item.title} – {clip.title}"
    if platform == "youtube" and short:
        title = f"{title[:90]} #Shorts"
        lines.append("#Shorts")
    if platform == "tiktok":
        # TikTok hat nur eine Bildunterschrift – Credits stehen direkt darin
        caption = "\n".join([title] + [l for l in lines if not l.startswith("Original-VOD")])
        return sanitize(caption)[:2200], ""
    return sanitize(title)[:100], sanitize("\n".join(lines))


# ---------------------------------------------------------------- Upload

def _record(item: BoxItem, media: MediaFile, result: dict, settings: Settings) -> dict:
    rel = str(media.path.relative_to(item.folder)).replace("\\", "/")
    entry = {**result, "file": rel, "kind": media.kind, "title": media.title}
    item.info.setdefault("uploads", []).append(entry)
    item.save()
    project = _project(item, settings)
    clips = [c for c in project.selected_clips() if media.kind == KIND_VIDEO or c.id == media.clip_id]
    uploadlog.append(
        settings.upload_log_file, "upload", project=project.name, file=str(media.path),
        sources=[c.provenance() for c in clips],
        creators=sorted({c.source.creator_name for c in clips} | {x["creator_name"] for c in clips for x in c.companions}),
        **{k: v for k, v in result.items() if k not in ("file",)},
    )
    return entry


def upload_youtube(
    item: BoxItem, media: MediaFile, settings: Settings, *, title: str, description: str,
    privacy: str = "private", publish_at=None, service=None, progress: Callable[[float], None] | None = None,
) -> dict:
    from . import youtube

    blockers = blockers_for(item, media, settings)
    if blockers:
        raise UploadBlocked(blockers)
    tags = build_tags(_project(item, settings), settings)
    result = youtube.upload_file(
        media.path, settings, title=title, description=description, tags=tags, privacy=privacy,
        publish_at=publish_at, service=service, progress=progress,
    )
    return _record(item, media, result, settings)


def upload_tiktok(
    item: BoxItem, media: MediaFile, settings: Settings, *, title: str, privacy_level: str = "SELF_ONLY",
    progress: Callable[[float], None] | None = None,
) -> dict:
    from . import tiktok

    blockers = blockers_for(item, media, settings)
    if blockers:
        raise UploadBlocked(blockers)
    result = tiktok.upload_video(media.path, settings, title=title, privacy_level=privacy_level, progress=progress)
    return _record(item, media, result, settings)
