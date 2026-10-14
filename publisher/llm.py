"""Intro-Text für die Beschreibung, generiert mit Claude im Stil der Referenz-Creator.

Fällt auf einen schlichten Vorlagentext zurück, wenn das `anthropic`-Paket fehlt,
keine Zugangsdaten vorhanden sind oder die Anfrage scheitert. Der Text wird im
Projekt gespeichert und kann in der Review-UI überarbeitet werden.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .config import Settings
from .models import Candidate, Project

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """Du schreibst Intro-Texte für YouTube-Beschreibungen von Highlight-Videos aus Livestreams.
Schreibe auf Deutsch, 2 bis 4 Sätze, ohne Hashtags, ohne Links, ohne Zeitstempel und ohne Aufzählungen.
Credits, Kapitel und Links ergänzt das Tool selbst – erwähne sie nicht.
Triff Ton, Länge und Wortwahl der Referenz-Beschreibungen, falls welche mitgegeben werden.
Gib ausschließlich den Intro-Text aus."""


def load_style_references(settings: Settings, limit_chars: int = 12000) -> list[str]:
    folder = settings.path(settings["llm"].get("style_reference_dir") or "style_references")
    if not folder.is_dir():
        return []
    refs, total = [], 0
    for path in sorted(folder.glob("*")):
        if path.suffix.lower() not in (".txt", ".md"):
            continue
        text = path.read_text(encoding="utf-8").strip()
        if not text or total + len(text) > limit_chars:
            continue
        refs.append(text)
        total += len(text)
    return refs


def fallback_intro(project: Project, clips: list[Candidate]) -> str:
    creators = sorted({c.source.creator_name for c in clips})
    title = project.meta.title or project.name
    if not creators:
        return f"{title}: die besten Momente aus dem Stream."
    who = creators[0] if len(creators) == 1 else ", ".join(creators[:-1]) + " und " + creators[-1]
    return f"{title} – die besten Momente mit {who}. Alle Quellen findet ihr unten in den Credits."


def _build_prompt(project: Project, clips: list[Candidate], refs: list[str]) -> str:
    parts = []
    if refs:
        parts.append(
            "Referenz-Beschreibungen (nur als Stilvorlage):\n\n"
            + "\n\n---\n\n".join(f"<referenz>\n{r}\n</referenz>" for r in refs)
        )
    clip_lines = "\n".join(
        f"- {c.title} (von {c.source.creator_name}"
        + (f", Stream „{c.source.stream_title}“" if c.source.stream_title else "")
        + ")"
        for c in clips
    )
    parts.append(
        f"Videotitel: {project.meta.title or project.name}\n"
        f"Format: {'YouTube Short' if project.meta.is_short else 'Highlight-Video'}\n"
        f"Clips:\n{clip_lines}\n\nSchreibe jetzt den Intro-Text."
    )
    return "\n\n".join(parts)


def generate_intro(project: Project, clips: list[Candidate], settings: Settings) -> tuple[str, str | None]:
    """Gibt (text, warnung) zurück. Warnung ist None, wenn das LLM geantwortet hat."""
    cfg = settings["llm"]
    if not cfg.get("enabled", True):
        return fallback_intro(project, clips), "LLM deaktiviert – Vorlagentext verwendet."
    try:
        import anthropic
    except ImportError:
        return fallback_intro(project, clips), None  # z. B. in der .exe: einfach Vorlagentext

    try:
        client = anthropic.Anthropic()
        response = client.beta.messages.create(
            model=cfg.get("model", "claude-opus-5-5"),
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            output_config={"effort": cfg.get("effort", "low")},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": _build_prompt(project, clips, load_style_references(settings))}],
        )
    except anthropic.APIError as exc:
        log.warning("Intro-Generierung fehlgeschlagen: %s", exc)
        return fallback_intro(project, clips), f"LLM-Fehler ({type(exc).__name__}) – Vorlagentext verwendet."
    except Exception as exc:  # z. B. fehlende Zugangsdaten
        log.warning("Intro-Generierung nicht möglich: %s", exc)
        return fallback_intro(project, clips), f"LLM nicht verfügbar ({exc}) – Vorlagentext verwendet."

    if response.stop_reason == "refusal":
        return fallback_intro(project, clips), "LLM hat abgelehnt – Vorlagentext verwendet."
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        return fallback_intro(project, clips), "LLM lieferte keinen Text – Vorlagentext verwendet."
    return text, None
