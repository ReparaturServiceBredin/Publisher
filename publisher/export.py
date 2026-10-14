"""Lokaler Export: Video rendern, Positionen berechnen, Beschreibung + Manifest schreiben.

Der Export ist für alle VODs möglich – unabhängig vom Freigabe-Status.
Die Sperre greift erst beim Upload (siehe gate.py).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import render
from .config import Settings
from .creators import CreatorRegistry
from .description import build_description, build_short_description, build_tags, build_title, overlay_text_for
from .gate import find_blockers
from .llm import generate_intro
from .models import Candidate, Project, ProjectError, now_utc


def assign_positions(clips: list[Candidate], durations: list[float], offset: float = 0.0) -> float:
    """Setzt highlight_start/-end jedes Clips; gibt die Gesamtlänge zurück."""
    pos = offset
    for clip, dur in zip(clips, durations):
        clip.highlight_start = round(pos, 3)
        pos += dur
        clip.highlight_end = round(pos, 3)
    return pos


def ensure_intro(project: Project, clips: list[Candidate], settings: Settings) -> list[str]:
    if project.meta.intro_text.strip():
        return []
    text, warning = generate_intro(project, clips, settings)
    project.meta.intro_text = text
    return [warning] if warning else []


def _font_file(out_dir: Path) -> str | None:
    """Kopiert eine Systemschrift in den Export-Ordner (relativer Pfad = kein Escaping im Filter)."""
    import os
    import shutil

    candidates = [
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name
        for name in ("segoeui.ttf", "arial.ttf")
    ] + [Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")]
    for font in candidates:
        if font.exists():
            shutil.copy(font, out_dir / "overlay_font.ttf")
            return "overlay_font.ttf"
    return None


def render_clips(
    project: Project,
    clips: list[Candidate],
    registry: CreatorRegistry,
    settings: Settings,
    out_dir: Path,
    progress=None,
) -> tuple[Path, list[float]]:
    durations: list[float] = []
    list_lines: list[str] = []
    font = _font_file(out_dir)
    for i, clip in enumerate(clips, start=1):
        if progress:
            progress(f"Schneide Clip {i}/{len(clips)}", (i - 1) / len(clips))
        vod = project.vods[clip.source.vod_id]
        src, offset = vod.source_for(clip.trim_start, clip.trim_end)
        overlay = overlay_text_for(clip, registry, settings)
        overlay_file = None
        if overlay:
            overlay_file = f"overlay_{i:02d}.txt"
            (out_dir / overlay_file).write_text(overlay, encoding="utf-8")
        clip_file = f"clip_{i:02d}.mp4"
        vf = render.video_filter(settings, project.meta.is_short, overlay_file, font)
        cmd = render.cut_command(
            str(Path(src).resolve()), clip.trim_start - offset, clip.duration, clip_file, vf, render.has_audio(src)
        )
        render.run(cmd, cwd=out_dir)
        durations.append(render.probe_duration(out_dir / clip_file))
        list_lines.append(f"file '{clip_file}'")
    if progress:
        progress("Füge Highlight-Video zusammen", 1.0)
    (out_dir / "concat.txt").write_text("\n".join(list_lines) + "\n", encoding="utf-8")
    render.run(render.concat_command("concat.txt", "video.mp4"), cwd=out_dir)
    return out_dir / "video.mp4", durations


def export_project(
    project: Project,
    registry: CreatorRegistry,
    settings: Settings,
    do_render: bool = True,
    progress=None,
) -> dict:
    clips = project.selected_clips()
    if not clips:
        raise ProjectError("Keine Clips ausgewählt – bitte in der Review-UI Clips auf „ausgewählt“ setzen.")
    if project.meta.is_short and len(clips) != 1:
        raise ProjectError("Ein Short besteht aus genau einem Clip.")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = project.dir / "exports" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    if do_render:
        video, durations = render_clips(project, clips, registry, settings, out_dir, progress)
    else:
        video, durations = None, [c.duration for c in clips]
    total = assign_positions(clips, durations)

    warnings = ensure_intro(project, clips, settings)
    if project.meta.is_short:
        desc = build_short_description(project, clips, registry, settings)
    else:
        desc = build_description(project, clips, registry, settings, total, project.meta.intro_text)
    warnings += desc.warnings
    if project.meta.is_short and total > 180:
        warnings.append("Short ist länger als 3 Minuten – YouTube stuft ihn dann nicht als Short ein.")

    blockers = find_blockers(project, registry)
    manifest = {
        "project": project.name,
        "created_at": now_utc().isoformat(),
        "video": str(video) if video else None,
        "is_short": project.meta.is_short,
        "title": build_title(project),
        "tags": build_tags(project, settings),
        "category_id": project.meta.category_id or settings["youtube"]["default_category_id"],
        "duration": round(total, 3),
        "description": desc.text,
        "chapters": [{"start": s, "title": t} for s, t in desc.chapters],
        "warnings": warnings,
        "clips": [c.provenance() for c in clips],
        "upload_blocked_at_export": [b.describe() for b in blockers],
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "description.txt").write_text(desc.text, encoding="utf-8")

    project.exports.append(
        {
            "created_at": manifest["created_at"],
            "dir": str(out_dir),
            "video": manifest["video"],
            "manifest": str(manifest_path),
            "clips": [
                {k: p[k] for k in ("clip_id", "title", "creator_id", "creator_name")} for p in manifest["clips"]
            ],
        }
    )
    project.save()
    return manifest


def load_manifest(project: Project) -> dict | None:
    export = project.last_export
    if not export or not export.get("manifest") or not Path(export["manifest"]).exists():
        return None
    return json.loads(Path(export["manifest"]).read_text(encoding="utf-8"))
