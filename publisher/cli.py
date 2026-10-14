"""Kommandozeile: publisher <befehl> …"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from . import uploadlog
from .config import load_settings
from .creators import OWN_VOD, CreatorRegistry
from .gate import UploadBlocked, find_blockers
from .models import CLIP_SELECTED, ProjectError, ProjectStore
from .timefmt import format_de_datetime, format_hms, parse_timestamp


def _ctx(args):
    settings = load_settings(args.config)
    registry = CreatorRegistry.load(settings.creators_file)
    store = ProjectStore(settings.projects_dir)
    return settings, registry, store


def cmd_creators(args) -> int:
    settings, registry, _ = _ctx(args)
    if not len(registry):
        print(f"Keine Creator in {settings.creators_file}")
    for c in registry:
        print(f"{c.id:20} {c.name:25} {c.permission_status:14} {c.permission_proof}")
    return 0


def cmd_import(args) -> int:
    from .importer import import_vod

    settings, registry, store = _ctx(args)
    project = store.load_or_create(args.project)
    vod = import_vod(
        project,
        registry,
        settings,
        args.video,
        creator_id=OWN_VOD if args.own else args.creator,
        date=args.date,
        time=args.time,
        vod_url=args.vod_url,
        stream_title=args.stream_title,
        interactive=not args.no_input,
    )
    project.save()
    print(f"VOD importiert: {vod.id} ({vod.creator_name}, live ab {format_de_datetime(vod.live_start_dt)})")
    return 0


def cmd_add_clip(args) -> int:
    settings, registry, store = _ctx(args)
    project = store.load(args.project)
    clip = project.add_candidate(args.vod, parse_timestamp(args.start), parse_timestamp(args.end), args.title or "")
    if args.select:
        project.apply_review_edit(clip.id, {"status": CLIP_SELECTED})
    project.save()
    print(f"Clip angelegt: {clip.id} – {clip.title}")
    return 0


def cmd_show(args) -> int:
    settings, registry, store = _ctx(args)
    project = store.load(args.project)
    print(f"Projekt {project.name} – {len(project.vods)} VOD(s), {len(project.candidates)} Clip(s)")
    for c in sorted(project.candidates, key=lambda c: c.order):
        pos = f"@{format_hms(c.highlight_start)}" if c.highlight_start is not None else ""
        print(
            f"  [{c.status:11}] {c.id} {c.title!r} – {c.source.creator_name}, "
            f"VOD {format_hms(c.trim_start)}–{format_hms(c.trim_end)}, "
            f"live {format_de_datetime(c.live_start_dt)} {pos}"
        )
    blockers = find_blockers(project, registry)
    if blockers:
        print("\nUpload gesperrt:")
        for b in blockers:
            print(f"  - {b.describe()}")
    else:
        print("\nUpload möglich (alle Clips freigegeben).")
    return 0


def cmd_export(args) -> int:
    from .export import export_project

    settings, registry, store = _ctx(args)
    project = store.load(args.project)
    manifest = export_project(project, registry, settings, do_render=not args.no_render)
    print(f"Export fertig: {manifest['video'] or '(nicht gerendert)'}")
    print(f"Manifest: {project.last_export['manifest']}")
    for w in manifest["warnings"]:
        print(f"Warnung: {w}")
    if manifest["upload_blocked_at_export"]:
        print("Hinweis: Upload ist derzeit gesperrt:")
        for line in manifest["upload_blocked_at_export"]:
            print(f"  - {line}")
    return 0


def cmd_upload(args) -> int:
    from .youtube import upload_project

    settings, registry, store = _ctx(args)
    project = store.load(args.project)
    when = datetime.fromisoformat(args.publish_at) if args.publish_at else None
    if when is not None and when.tzinfo is None:
        when = when.replace(tzinfo=settings.tz)
    record = upload_project(project, registry, settings, publish_at=when)
    print(f"Hochgeladen (privat): {record['url']}")
    if record["publish_at"]:
        print(f"Geplante Veröffentlichung: {record['publish_at']}")
    else:
        print("Veröffentlichen über die Review-UI (publisher ui).")
    return 0


def cmd_check_scheduled(args) -> int:
    from .youtube import revoke_schedule_if_blocked

    settings, registry, store = _ctx(args)
    names = args.projects or store.list()
    for name in names:
        project = store.load(name)
        if revoke_schedule_if_blocked(project, registry, settings):
            print(f"{name}: geplante Veröffentlichung aufgehoben (Freigabe fehlt)")
    return 0


def cmd_log(args) -> int:
    settings, _, _ = _ctx(args)
    for entry in uploadlog.read(settings.upload_log_file, args.project):
        print(json.dumps(entry, ensure_ascii=False))
    return 0


def cmd_ui(args) -> int:
    from .webui.app import create_app

    app = create_app(args.config)
    app.run(host=args.host, port=args.port, debug=False)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="publisher", description="Highlights mit Quellenangaben und YouTube-Upload")
    p.add_argument("--config", help="Pfad zur config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("creators", help="Creator-Register anzeigen")
    s.set_defaults(func=cmd_creators)

    s = sub.add_parser("import", help="VOD importieren (fragt Creator und Aufnahmezeit ab)")
    s.add_argument("project")
    s.add_argument("video")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--creator", help="Creator-ID aus creators.yaml")
    g.add_argument("--own", action="store_true", help="eigenes VOD")
    s.add_argument("--date", help="Datum der Live-Aufnahme, TT.MM.JJJJ")
    s.add_argument("--time", help="Startuhrzeit der Live-Aufnahme, HH:MM")
    s.add_argument("--vod-url")
    s.add_argument("--stream-title")
    s.add_argument("--no-input", action="store_true", help="nicht nachfragen")
    s.set_defaults(func=cmd_import)

    s = sub.add_parser("add-clip", help="Clip-Kandidat anlegen")
    s.add_argument("project")
    s.add_argument("--vod", required=True)
    s.add_argument("--start", required=True, help="z. B. 1:02:03")
    s.add_argument("--end", required=True)
    s.add_argument("--title")
    s.add_argument("--select", action="store_true", help="direkt auswählen")
    s.set_defaults(func=cmd_add_clip)

    s = sub.add_parser("show", help="Clips, Quellen und Upload-Status anzeigen")
    s.add_argument("project")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("export", help="Video lokal rendern + Beschreibung erzeugen")
    s.add_argument("project")
    s.add_argument("--no-render", action="store_true", help="nur Beschreibung/Manifest")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("upload", help="Letzten Export privat zu YouTube hochladen")
    s.add_argument("project")
    s.add_argument("--publish-at", help="geplante Veröffentlichung, ISO (z. B. 2026-10-20T18:00)")
    s.set_defaults(func=cmd_upload)

    s = sub.add_parser("check-scheduled", help="Geplante Veröffentlichungen aufheben, wenn Freigaben fehlen")
    s.add_argument("projects", nargs="*")
    s.set_defaults(func=cmd_check_scheduled)

    s = sub.add_parser("log", help="Upload-Log anzeigen")
    s.add_argument("project", nargs="?")
    s.set_defaults(func=cmd_log)

    s = sub.add_parser("ui", help="Review-UI starten")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=5000)
    s.set_defaults(func=cmd_ui)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except UploadBlocked as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except (ProjectError, ValueError, RuntimeError) as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
