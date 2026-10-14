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


def assign_positions(
    clips: list[Candidate], durations: list[float], offset: float = 0.0, overlap: float = 0.0
) -> float:
    """Setzt highlight_start/-end jedes Clips; gibt die Gesamtlänge zurück.

    `overlap`: Dauer der Überblendung – der nächste Clip beginnt so viel früher.
    """
    pos = offset
    for i, (clip, dur) in enumerate(zip(clips, durations)):
        if i:
            pos -= overlap
        clip.highlight_start = round(pos, 3)
        pos += dur
        clip.highlight_end = round(pos, 3)
    return pos


def transition_seconds(settings: Settings, durations: list[float]) -> float:
    cfg = settings["transitions"]
    if not cfg.get("enabled", True) or len(durations) < 2:
        return 0.0
    # nie länger als ein Drittel des kürzesten Clips
    return round(min(float(cfg.get("duration", 0.5)), min(durations) / 3), 3)


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
        if clip.companions:
            _render_split(project, clip, i, src, offset, overlay_file, font, settings, out_dir, clip_file)
        else:
            vf = render.video_filter(settings, project.meta.is_short, overlay_file, font)
            cmd = render.cut_command(
                str(Path(src).resolve()), clip.trim_start - offset, clip.duration, clip_file, vf, render.has_audio(src)
            )
            render.run(cmd, cwd=out_dir)
        durations.append(render.probe_duration(out_dir / clip_file))
        list_lines.append(f"file '{clip_file}'")
    if progress:
        progress("Füge Highlight-Video zusammen (mit Überblendungen)", 1.0)
    fade = transition_seconds(settings, durations)
    if fade > 0:
        script = render.transition_filter(durations, fade, settings["video"]["fps"])
        (out_dir / "transitions.txt").write_text(script, encoding="utf-8")
        files = [f"clip_{i:02d}.mp4" for i in range(1, len(clips) + 1)]
        render.run(render.transition_command(files, "transitions.txt", "video.mp4"), cwd=out_dir)
    else:
        (out_dir / "concat.txt").write_text("\n".join(list_lines) + "\n", encoding="utf-8")
        render.run(render.concat_command("concat.txt", "video.mp4"), cwd=out_dir)
    return out_dir / "video.mp4", durations


def _render_split(project, clip, i, src, offset, overlay_file, font, settings, out_dir, clip_file) -> None:
    inputs = [(str(Path(src).resolve()), clip.trim_start - offset)]
    labels: list[str | None] = []
    names = [clip.source.creator_name] + [c["creator_name"] for c in clip.companions]
    for comp in clip.companions[:3]:
        cvod = project.vods[comp["vod_id"]]
        cstart = clip.trim_start + comp["offset"]
        csrc, coff = cvod.source_for(cstart, cstart + clip.duration)
        inputs.append((str(Path(csrc).resolve()), cstart - coff))
    for j, name in enumerate(names[: len(inputs)]):
        label = f"label_{i:02d}_{j}.txt"
        (out_dir / label).write_text(name, encoding="utf-8")
        labels.append(label)
    script = f"split_{i:02d}.txt"
    graph = render.split_filter(
        len(inputs), settings, clip.duration, labels, overlay_file, font, render.has_audio(inputs[0][0])
    )
    (out_dir / script).write_text(graph, encoding="utf-8")
    render.run(render.split_command(inputs, clip.duration, script, clip_file), cwd=out_dir)


def render_vertical(clip_files: list[Path], settings: Settings, out_dir: Path, progress=None) -> list[Path]:
    """Hochformat-Versionen (9:16) der Einzelclips für TikTok und YouTube Shorts."""
    out = []
    for i, f in enumerate(clip_files, start=1):
        if progress:
            progress(f"Hochformat {i}/{len(clip_files)}", (i - 1) / max(1, len(clip_files)))
        target = out_dir / f"vertical_{i:02d}.mp4"
        render.run(render.vertical_command(str(Path(f).resolve()), target.name, settings), cwd=out_dir)
        out.append(target)
    return out


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
    total = assign_positions(clips, durations, overlap=transition_seconds(settings, durations))

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
                {
                    **{k: p[k] for k in ("clip_id", "title", "creator_id", "creator_name")},
                    "companions": [
                        {"creator_id": c["creator_id"], "creator_name": c["creator_name"]} for c in p["companions"]
                    ],
                }
                for p in manifest["clips"]
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
