"""Ein Klick: VOD (Link oder Datei) rein → Highlight-Video (10–15 Min) + Einzelclips raus."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from . import detect, fetch, render
from .config import Settings
from .creators import OWN_VOD, STATUS_REQUESTED, Creator, CreatorRegistry, slugify
from .export import export_project
from .models import CLIP_SELECTED, Project, ProjectStore

Progress = Callable[[str, float], None]

SEGMENT_PADDING = 15.0  # Sekunden Puffer um jedes Highlight, damit man später nachtrimmen kann


@dataclass
class AutoResult:
    project: str
    video: Path
    clips: list[Path] = field(default_factory=list)
    description: Path | None = None
    folder: Path | None = None
    warnings: list[str] = field(default_factory=list)


def _noop(_msg: str, _frac: float) -> None:
    pass


def safe_filename(text: str, limit: int = 80) -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return (text[:limit].rstrip(" .") or "Video")


def _project_name(title: str, when: datetime) -> str:
    return f"{when.strftime('%Y-%m-%d_%H%M')}_{slugify(title)[:40]}"


def _merge_ranges(ranges: list[tuple[float, float]], gap: float = 5.0) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + gap:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


def _ensure_creator(registry: CreatorRegistry, meta: dict) -> str:
    """Neue Creator landen automatisch im Register (Status „angefragt“ – Freigabe kommt später)."""
    creator_id = slugify(meta["creator_name"])
    if registry.get(creator_id) is None:
        registry.add(
            Creator(
                id=creator_id,
                name=meta["creator_name"],
                twitch_channel_url=meta["twitch_channel_url"],
                youtube_channel_url=meta["youtube_channel_url"],
                permission_status=STATUS_REQUESTED,
                permission_proof="automatisch angelegt beim Import",
            )
        )
        registry.save()
    return creator_id


def _unique_project(store: ProjectStore, name: str) -> Project:
    candidate, n = name, 2
    while store.exists(candidate):
        candidate = f"{name}-{n}"
        n += 1
    return store.create(candidate)


def make_highlights(
    source: str,
    settings: Settings,
    *,
    minutes: float = 12,
    progress: Progress | None = None,
    output_root: Path | None = None,
) -> AutoResult:
    progress = progress or _noop
    source = source.strip().strip('"')
    registry = CreatorRegistry.load(settings.creators_file)
    store = ProjectStore(settings.projects_dir)
    warnings: list[str] = []

    if fetch.is_url(source):
        url = source if source.lower().startswith("http") else f"https://{source}"
        progress("Lese Infos zum VOD", 0.0)
        meta = fetch.source_metadata(fetch.fetch_info(url))
        creator_id = _ensure_creator(registry, meta)
        project = _unique_project(store, _project_name(meta["stream_title"] or meta["creator_name"], meta["live_start"]))
        dl_dir = project.dir / "download"
        audio = fetch.download_audio(url, dl_dir, progress)
        duration = meta["duration"] or render.probe_duration(audio)
        highlights = detect.detect_highlights(audio, detect.target_length(duration, minutes), duration, progress)
        if not highlights:
            raise RuntimeError("Im VOD wurden keine Highlights gefunden.")
        vod = project.add_vod(
            path="", creator_id=creator_id, creator_name=meta["creator_name"],
            live_start=meta["live_start"].astimezone(settings.tz), vod_url=meta["vod_url"],
            stream_title=meta["stream_title"], duration=duration,
        )
        ranges = _merge_ranges(
            [(max(0.0, h.start - SEGMENT_PADDING), min(duration, h.end + SEGMENT_PADDING)) for h in highlights]
        )
        try:
            segments = fetch.download_sections(url, ranges, dl_dir, progress)
            for seg in segments:  # tatsächliche Länge prüfen, damit die Zeiten exakt bleiben
                seg["end"] = seg["start"] + render.probe_duration(seg["path"])
            vod.segments = segments
        except Exception as exc:  # Abschnitts-Download nicht unterstützt → komplettes VOD
            warnings.append(f"Abschnitte nicht ladbar ({exc}); lade komplettes VOD.")
            vod.path = str(fetch.download_full(url, dl_dir, progress))
        audio.unlink(missing_ok=True)
        title = meta["stream_title"] or f"Highlights {meta['creator_name']}"
    else:
        path = Path(source)
        if not path.exists():
            raise FileNotFoundError(f"Datei nicht gefunden: {source}")
        progress("Lese Datei", 0.0)
        duration = render.probe_duration(path)
        live_start = datetime.fromtimestamp(path.stat().st_mtime, tz=settings.tz) - timedelta(seconds=duration)
        project = _unique_project(store, _project_name(path.stem, live_start))
        vod = project.add_vod(
            path=str(path.resolve()), creator_id=OWN_VOD,
            creator_name=settings["own_channel"].get("name") or "Eigener Kanal",
            live_start=live_start, stream_title=path.stem, duration=duration,
        )
        highlights = detect.detect_highlights(path, detect.target_length(duration, minutes), duration, progress)
        if not highlights:
            raise RuntimeError("Im Video wurden keine Highlights gefunden.")
        title = f"{path.stem} – Highlights"

    for i, h in enumerate(highlights, start=1):
        clip = project.add_candidate(vod.id, round(h.start, 2), round(min(h.end, duration), 2), f"Highlight {i}")
        project.apply_review_edit(clip.id, {"status": CLIP_SELECTED})
    project.meta.title = title[:100]
    project.save()

    manifest = export_project(project, registry, settings, progress=progress)
    warnings += manifest["warnings"]

    progress("Speichere Ergebnis", 1.0)
    out_root = output_root or settings.base_dir / "Fertig"
    folder = out_root / safe_filename(f"{project.created_at[:10]} {title}")
    n = 2
    while folder.exists():
        folder = out_root / safe_filename(f"{project.created_at[:10]} {title} ({n})")
        n += 1
    (folder / "Einzelclips").mkdir(parents=True)
    export_dir = Path(project.last_export["dir"])
    video = folder / f"{safe_filename(title)}.mp4"
    shutil.copy(export_dir / "video.mp4", video)
    clips = []
    for i, c in enumerate(manifest["clips"], start=1):
        target = folder / "Einzelclips" / f"{i:02d} {safe_filename(c['title'])}.mp4"
        shutil.copy(export_dir / f"clip_{i:02d}.mp4", target)
        clips.append(target)
    desc = folder / "Beschreibung.txt"
    desc.write_text(f"{manifest['title']}\n\n{manifest['description']}\n", encoding="utf-8")
    return AutoResult(project.name, video, clips, desc, folder, warnings)

