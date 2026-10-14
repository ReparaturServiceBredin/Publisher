"""Ein Klick: ein oder mehrere VODs (Links oder Dateien) rein → Highlight-Video + Einzelclips raus.

Mehrere VODs = gemeinsamer Stream aus verschiedenen POVs. Die VODs werden zeitlich
synchronisiert, Highlights auf der gemeinsamen Zeitachse gesucht (reagieren mehrere
Streamer gleichzeitig, zählt der Moment mehr) und dann entweder
* „pov“: die Situation aus dem stärksten POV, danach die Reaktionen der anderen, oder
* „split“: alle Streamer gleichzeitig im Splitscreen.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import numpy as np

from . import detect, fetch, render, sync
from .config import Settings
from .creators import OWN_VOD, STATUS_REQUESTED, Creator, CreatorRegistry, slugify
from .export import export_project, render_vertical
from .models import CLIP_SELECTED, Project, ProjectStore, Vod

Progress = Callable[[str, float], None]

SEGMENT_PADDING = 15.0  # Sekunden Puffer um jedes Highlight, damit man später nachtrimmen kann
LAYOUT_POV = "pov"
LAYOUT_SPLIT = "split"


@dataclass
class Options:
    minutes: float = 12
    focus: str = detect.FOCUS_LAUGH
    layout: str = LAYOUT_POV
    vertical: bool = True
    transitions: bool = True


@dataclass
class AutoResult:
    project: str
    video: Path
    clips: list[Path] = field(default_factory=list)
    description: Path | None = None
    folder: Path | None = None
    warnings: list[str] = field(default_factory=list)
    vertical: list[Path] = field(default_factory=list)


@dataclass
class _Source:
    text: str
    url: str | None
    meta: dict
    duration: float
    creator_id: str
    local: Path | None = None
    audio: Path | None = None
    analysis: detect.AudioAnalysis | None = None
    vod: Vod | None = None
    offset: float = 0.0  # Zeit in diesem VOD = Referenzzeit + offset
    curve: np.ndarray | None = None


@dataclass
class _Plan:
    src: int
    start: float
    end: float
    title: str
    moment: int
    companions: list[tuple[int, float]] = field(default_factory=list)  # (Quelle, offset zur Hauptquelle)


def _noop(_msg: str, _frac: float) -> None:
    pass


def safe_filename(text: str, limit: int = 80) -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:limit].rstrip(" .") or "Video"


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


# ---------------------------------------------------------------- Quellen einlesen

def _read_source(text: str, settings: Settings, registry: CreatorRegistry, progress: Progress) -> _Source:
    text = text.strip().strip('"')
    if fetch.is_url(text):
        url = text if text.lower().startswith("http") else f"https://{text}"
        progress("Lese Infos zum VOD", 0.0)
        meta = fetch.source_metadata(fetch.fetch_info(url))
        return _Source(text, url, meta, meta["duration"] or 0.0, _ensure_creator(registry, meta))
    path = Path(text)
    if not path.exists():
        raise FileNotFoundError(f"Datei nicht gefunden: {text}")
    duration = render.probe_duration(path)
    meta = {
        "creator_name": settings["own_channel"].get("name") or "Eigener Kanal",
        "twitch_channel_url": "", "youtube_channel_url": "",
        "live_start": datetime.fromtimestamp(path.stat().st_mtime, tz=settings.tz) - timedelta(seconds=duration),
        "vod_url": "", "stream_title": path.stem, "duration": duration, "from_file": True,
    }
    return _Source(text, None, meta, duration, OWN_VOD, local=path)


# ---------------------------------------------------------------- Planung

def _plan_single(src: _Source, target: float) -> list[_Plan]:
    a = src.analysis
    picks = detect.pick_with_fallback(src.curve, target, loudness=a.loudness)
    return [_Plan(0, h.start, min(h.end, src.duration), f"Highlight {i}", i) for i, h in enumerate(picks, start=1)]


def _sync_sources(sources: list[_Source]) -> list[str]:
    notes = []
    ref = sources[0]
    ref_ts = None if ref.meta.get("from_file") else ref.meta["live_start"].timestamp()
    for s in sources[1:]:
        ts = None if s.meta.get("from_file") else s.meta["live_start"].timestamp()
        res = sync.find_offset(ref.analysis.envelope, s.analysis.envelope, sync.metadata_guess(ref_ts, ts))
        s.offset = res.offset
        how = {"audio": "über den Ton", "metadaten": "über die Startzeit", "keine": "NICHT sicher"}[res.method]
        notes.append(f"{s.meta['creator_name']}: Versatz {res.offset:+.1f} s ({how} synchronisiert)")
    return notes


def _plan_multi(sources: list[_Source], target: float, layout: str) -> list[_Plan]:
    g0 = min(0.0, min(-s.offset for s in sources))
    g_end = max(s.duration - s.offset for s in sources)
    length = int(np.ceil(g_end - g0))
    if length <= 0:
        return []
    grid = np.full((len(sources), length), np.nan, dtype=np.float32)
    for j, s in enumerate(sources):
        g = np.arange(length) + g0 + s.offset  # Zeit im VOD j für jede globale Sekunde
        ok = (g >= 0) & (g < len(s.curve))
        grid[j, ok] = s.curve[g[ok].astype(int)]
    filled = np.nan_to_num(grid, nan=-5.0)
    top = np.sort(filled, axis=0)[::-1]
    # Momente, in denen mehrere Streamer gleichzeitig reagieren, zählen mehr
    combined = top[0] + (0.5 * np.clip(top[1], 0, None) if len(sources) > 1 else 0)
    share = 0.75 if layout == LAYOUT_POV else 1.0
    moments = detect.pick_with_fallback(combined, target * share)

    plans: list[_Plan] = []
    for k, m in enumerate(moments, start=1):
        peak = int(m.peak)
        lo, hi = max(0, peak - 2), min(length, peak + 5)
        reactions = np.nanmax(np.where(np.isnan(grid[:, lo:hi]), -np.inf, grid[:, lo:hi]), axis=1)

        def window(j: int, gs: float, ge: float) -> tuple[float, float] | None:
            s = sources[j]
            a, b = gs + g0 + s.offset, ge + g0 + s.offset
            a, b = max(0.0, a), min(s.duration, b)
            return (a, b) if b - a >= 4 else None

        order = [int(j) for j in np.argsort(reactions)[::-1] if np.isfinite(reactions[j])]
        primary = next((j for j in order if window(j, m.start, m.end)), None)
        if primary is None:
            continue
        ps, pe = window(primary, m.start, m.end)
        ps, pe = detect.snap_to_quiet(ps, pe, sources[primary].analysis.loudness)
        title = f"Highlight {k}"
        if layout == LAYOUT_SPLIT:
            comps = []
            for j in order:
                if j == primary or len(comps) >= 3:
                    continue
                off = sources[j].offset - sources[primary].offset
                if ps + off >= 0 and pe + off <= sources[j].duration:
                    comps.append((j, off))
            plans.append(_Plan(primary, ps, pe, title, k, comps))
            continue
        plans.append(_Plan(primary, ps, pe, title, k))
        for j in order:
            if j == primary or reactions[j] < 1.0 or len([p for p in plans if p.moment == k]) >= 3:
                continue
            w = window(j, peak - 4, peak + 6)
            if w:
                rs, re_ = detect.snap_to_quiet(*w, sources[j].analysis.loudness, reach=1.5)
                plans.append(_Plan(j, rs, re_, f"{title} – Reaktion {sources[j].meta['creator_name']}", k))
    return plans


# ---------------------------------------------------------------- Hauptablauf

def make_highlights(
    sources: str | list[str],
    settings: Settings,
    *,
    minutes: float | None = None,
    options: Options | None = None,
    progress: Progress | None = None,
    output_root: Path | None = None,
) -> AutoResult:
    progress = progress or _noop
    opts = options or Options()
    if minutes is not None:
        opts.minutes = minutes
    texts = [sources] if isinstance(sources, str) else [s for s in sources if s.strip()]
    if not texts:
        raise ValueError("Kein VOD angegeben.")
    settings.raw.setdefault("transitions", {})["enabled"] = opts.transitions
    registry = CreatorRegistry.load(settings.creators_file)
    store = ProjectStore(settings.projects_dir)
    warnings: list[str] = []

    srcs = [_read_source(t, settings, registry, progress) for t in texts]
    first = srcs[0].meta
    title = first["stream_title"] or f"Highlights {first['creator_name']}"
    if len(srcs) > 1:
        names = ", ".join(dict.fromkeys(s.meta["creator_name"] for s in srcs))
        title = f"{title} – mit {names}" if not srcs[0].meta.get("from_file") else f"Highlights – {names}"
    elif srcs[0].local:
        title = f"{srcs[0].local.stem} – Highlights"
    project = _unique_project(store, _project_name(first["stream_title"] or first["creator_name"], first["live_start"]))

    for i, s in enumerate(srcs):
        label = f"VOD {i + 1}/{len(srcs)}: " if len(srcs) > 1 else ""
        step = lambda msg, f, label=label: progress(label + msg, f)  # noqa: E731
        if s.url:
            s.audio = fetch.download_audio(s.url, project.dir / "download" / f"{i + 1}", step)
            s.duration = s.duration or render.probe_duration(s.audio)
        s.analysis = detect.analyze_audio(s.audio or s.local, s.duration, step)
        s.duration = min(s.duration, float(s.analysis.duration)) or s.duration
        s.curve = detect.highlight_curve(s.analysis, opts.focus)
        s.vod = project.add_vod(
            path=str(s.local.resolve()) if s.local else "", creator_id=s.creator_id,
            creator_name=s.meta["creator_name"], live_start=s.meta["live_start"].astimezone(settings.tz),
            vod_url=s.meta["vod_url"], stream_title=s.meta["stream_title"], duration=s.duration,
        )

    if len(srcs) == 1:
        plans = _plan_single(srcs[0], detect.target_length(srcs[0].duration, opts.minutes))
    else:
        progress("Synchronisiere VODs", 0.0)
        warnings += _sync_sources(srcs)
        longest = max(s.duration for s in srcs)
        plans = _plan_multi(srcs, detect.target_length(longest, opts.minutes), opts.layout)
    if not plans:
        raise RuntimeError("Es wurden keine Highlights gefunden.")

    # Nur die benötigten Abschnitte herunterladen
    for i, s in enumerate(srcs):
        if not s.url:
            continue
        need = [(p.start, p.end) for p in plans if p.src == i]
        for p in plans:
            need += [(p.start + off, p.end + off) for j, off in p.companions if j == i]
        if not need:
            continue
        ranges = _merge_ranges(
            [(max(0.0, a - SEGMENT_PADDING), min(s.duration, b + SEGMENT_PADDING)) for a, b in need]
        )
        dl_dir = project.dir / "download" / f"{i + 1}"
        try:
            segments = fetch.download_sections(s.url, ranges, dl_dir, progress)
            for seg in segments:  # tatsächliche Länge prüfen, damit die Zeiten exakt bleiben
                seg["end"] = seg["start"] + render.probe_duration(seg["path"])
            s.vod.segments = segments
        except Exception as exc:  # Abschnitts-Download nicht unterstützt → komplettes VOD
            warnings.append(f"Abschnitte nicht ladbar ({exc}); lade komplettes VOD.")
            s.vod.path = str(fetch.download_full(s.url, dl_dir, progress))
        if s.audio:
            s.audio.unlink(missing_ok=True)

    for order, p in enumerate(plans):
        clip = project.add_candidate(srcs[p.src].vod.id, round(p.start, 2), round(p.end, 2), p.title)
        clip.moment = p.moment
        clip.companions = [
            {
                "vod_id": srcs[j].vod.id, "creator_id": srcs[j].creator_id,
                "creator_name": srcs[j].meta["creator_name"], "vod_url": srcs[j].meta["vod_url"],
                "live_start": srcs[j].vod.live_start, "offset": round(off, 2),
            }
            for j, off in p.companions
        ]
        project.apply_review_edit(clip.id, {"status": CLIP_SELECTED, "order": order})
    project.meta.title = title[:100]
    project.save()

    manifest = export_project(project, registry, settings, progress=progress)
    warnings += manifest["warnings"]
    export_dir = Path(project.last_export["dir"])
    clip_files = [export_dir / f"clip_{i:02d}.mp4" for i in range(1, len(manifest["clips"]) + 1)]
    vertical = render_vertical(clip_files, settings, export_dir, progress) if opts.vertical else []

    progress("Speichere Ergebnis", 1.0)
    return _write_output(project, manifest, title, clip_files, vertical, warnings, output_root or settings.base_dir / "Fertig")


def _write_output(project, manifest, title, clip_files, vertical, warnings, out_root: Path) -> AutoResult:
    folder = out_root / safe_filename(f"{project.created_at[:10]} {title}")
    n = 2
    while folder.exists():
        folder = out_root / safe_filename(f"{project.created_at[:10]} {title} ({n})")
        n += 1
    (folder / "Einzelclips").mkdir(parents=True)
    video = folder / f"{safe_filename(title)}.mp4"
    shutil.copy(Path(project.last_export["dir"]) / "video.mp4", video)
    clips, verts, items = [], [], []
    if vertical:
        (folder / "Hochformat").mkdir()
    for i, (c, src) in enumerate(zip(manifest["clips"], clip_files), start=1):
        name = f"{i:02d} {safe_filename(c['title'])}.mp4"
        target = folder / "Einzelclips" / name
        shutil.copy(src, target)
        clips.append(target)
        item = {"clip_id": c["clip_id"], "title": c["title"], "file": f"Einzelclips/{name}"}
        if vertical:
            vt = folder / "Hochformat" / name
            shutil.copy(vertical[i - 1], vt)
            verts.append(vt)
            item["vertical"] = f"Hochformat/{name}"
        items.append(item)
    desc = folder / "Beschreibung.txt"
    desc.write_text(f"{manifest['title']}\n\n{manifest['description']}\n", encoding="utf-8")
    info = {
        "project": project.name,
        "title": manifest["title"],
        "created_at": manifest["created_at"],
        "video": video.name,
        "duration": manifest["duration"],
        "description_file": desc.name,
        "clips": items,
        "uploads": [],
    }
    (folder / "info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    return AutoResult(project.name, video, clips, desc, folder, warnings, verts)
