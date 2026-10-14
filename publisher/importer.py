"""VOD-Import: fragt Creator, Aufnahmezeitpunkt und optionale Quellen-Infos ab."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable

from .config import Settings
from .creators import OWN_VOD, STATUS_ALLOWED, CreatorRegistry
from .models import Project, ProjectError, Vod

Ask = Callable[[str], str]


def parse_live_start(date_text: str, time_text: str, settings: Settings) -> datetime:
    date_text, time_text = date_text.strip(), time_text.strip()
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            naive = datetime.strptime(f"{date_text} {time_text}", fmt)
            return naive.replace(tzinfo=settings.tz)
        except ValueError:
            continue
    raise ProjectError(
        f"Datum/Uhrzeit nicht erkannt: {date_text!r} {time_text!r} (erwartet TT.MM.JJJJ und HH:MM)"
    )


def choose_creator(registry: CreatorRegistry, ask: Ask, out: Callable[[str], None]) -> str:
    creators = list(registry)
    out("Welcher Creator?")
    out("  0) eigenes VOD")
    for i, c in enumerate(creators, start=1):
        out(f"  {i}) {c.name} [{c.permission_status}]")
    while True:
        answer = ask("Auswahl (Nummer oder ID): ").strip()
        if answer in ("0", "eigen", "eigenes VOD", OWN_VOD):
            return OWN_VOD
        if answer.isdigit() and 1 <= int(answer) <= len(creators):
            return creators[int(answer) - 1].id
        if registry.get(answer):
            return answer
        out("Unbekannte Auswahl – neue Creator bitte zuerst in creators.yaml eintragen.")


def _ask_required(ask: Ask, prompt: str) -> str:
    while True:
        value = ask(prompt).strip()
        if value:
            return value


def import_vod(
    project: Project,
    registry: CreatorRegistry,
    settings: Settings,
    path: str,
    *,
    creator_id: str | None = None,
    date: str | None = None,
    time: str | None = None,
    vod_url: str | None = None,
    stream_title: str | None = None,
    duration: float | None = None,
    ask: Ask = input,
    out: Callable[[str], None] = print,
    interactive: bool = True,
) -> Vod:
    if not Path(path).exists():
        raise ProjectError(f"Datei nicht gefunden: {path}")

    if creator_id is None:
        if not interactive:
            raise ProjectError("Creator fehlt (--creator ID oder --own)")
        creator_id = choose_creator(registry, ask, out)
    if creator_id == OWN_VOD:
        creator_name = settings["own_channel"].get("name") or "Eigener Kanal"
    else:
        creator = registry.get(creator_id)
        if creator is None:
            raise ProjectError(f"Creator {creator_id!r} steht nicht in {registry.path or 'creators.yaml'}")
        creator_name = creator.name
        if creator.permission_status != STATUS_ALLOWED:
            out(
                f"Hinweis: {creator.name} hat Status „{creator.permission_status}“. "
                "Verarbeitung und Export sind möglich, der Upload bleibt gesperrt."
            )

    if date is None or time is None:
        if not interactive:
            raise ProjectError("Datum und Startuhrzeit der Live-Aufnahme fehlen (--date, --time)")
        date = date or _ask_required(ask, "Datum der Live-Aufnahme (TT.MM.JJJJ): ")
        time = time or _ask_required(ask, "Startuhrzeit der Live-Aufnahme (HH:MM): ")
    live_start = parse_live_start(date, time, settings)

    if interactive and vod_url is None:
        vod_url = ask("Original-VOD-Link (optional, Enter = leer): ").strip()
    if interactive and stream_title is None:
        stream_title = ask("Stream-Titel (optional, Enter = leer): ").strip()

    if duration is None:
        try:
            from .render import probe_duration

            duration = probe_duration(path)
        except Exception:
            duration = None

    return project.add_vod(
        path=str(Path(path).resolve()),
        creator_id=creator_id,
        creator_name=creator_name,
        live_start=live_start,
        vod_url=vod_url or "",
        stream_title=stream_title or "",
        duration=duration,
    )
