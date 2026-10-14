"""YouTube-Beschreibung mit Kapitelmarken, Credit-Blöcken und Creator-Liste."""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Settings
from .creators import OWN_VOD, CreatorRegistry, CreditRules
from .models import Candidate, Project
from .timefmt import format_chapter, format_de_datetime, format_hms, vod_link_at

YOUTUBE_DESCRIPTION_LIMIT = 5000
YOUTUBE_TITLE_LIMIT = 100
YOUTUBE_TAGS_LIMIT = 500
MIN_CHAPTERS = 3
MIN_CHAPTER_SECONDS = 10


def sanitize(text: str) -> str:
    """YouTube lehnt '<' und '>' in Titel und Beschreibung ab."""
    return text.replace("<", "‹").replace(">", "›")


@dataclass
class CreditInfo:
    name: str
    youtube: str
    twitch: str
    rules: CreditRules
    is_own: bool

    def link_line(self) -> str:
        parts = []
        if self.youtube:
            parts.append(f"YouTube: {self.youtube}")
        if self.twitch:
            parts.append(f"Twitch: {self.twitch}")
        return " | ".join(parts)


def credit_info(creator_id: str, fallback_name: str, registry: CreatorRegistry, settings: Settings) -> CreditInfo:
    if creator_id == OWN_VOD:
        own = settings["own_channel"]
        return CreditInfo(
            name=own.get("name") or fallback_name,
            youtube=own.get("youtube_channel_url", ""),
            twitch=own.get("twitch_channel_url", ""),
            rules=CreditRules(),
            is_own=True,
        )
    creator = registry.get(creator_id)
    if creator is None:
        return CreditInfo(fallback_name, "", "", CreditRules(), False)
    return CreditInfo(
        creator.name, creator.youtube_channel_url, creator.twitch_channel_url, creator.credit, False
    )


def _overlay_one(creator_id: str, name: str, registry: CreatorRegistry) -> str:
    if creator_id == OWN_VOD:
        return ""  # eigene Clips brauchen keine Fremd-Einblendung
    creator = registry.get(creator_id)
    return creator.overlay_text() if creator else f"Clip von {name}"


def overlay_text_for(clip: Candidate, registry: CreatorRegistry, settings: Settings) -> str:
    texts = [_overlay_one(clip.creator_id, clip.source.creator_name, registry)]
    texts += [_overlay_one(c["creator_id"], c["creator_name"], registry) for c in clip.companions]
    return "   |   ".join(t for t in texts if t)


@dataclass
class DescriptionResult:
    text: str
    chapters: list[tuple[float, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _original_vod_line(clip: Candidate) -> str:
    stamp = format_hms(clip.trim_start)
    link = vod_link_at(clip.source.vod_url, clip.trim_start)
    if link:
        return f"Original-VOD: {link} (ab {stamp})"
    if clip.source.stream_title:
        return f"Original-VOD: „{clip.source.stream_title}“ (ab {stamp})"
    return f"Original-VOD: ab {stamp}"


def clip_credit_block(
    clip: Candidate, info: CreditInfo, long_video: bool, with_links: bool = True
) -> str:
    pos = format_chapter(clip.highlight_start or 0, long_video)
    lines = [
        f"▶ {pos} – {clip.title}",
        f"   Creator: {info.name} | Live am {format_de_datetime(clip.live_start_dt)}",
        f"   {_original_vod_line(clip)}",
    ]
    if with_links and info.link_line():
        lines.append(f"   {info.link_line()}")
    if info.rules.extra_line:
        lines.append(f"   {info.rules.extra_line}")
    for comp in clip.companions:  # Splitscreen: weitere Streamer im Bild
        start = clip.trim_start + comp["offset"]
        link = vod_link_at(comp.get("vod_url"), start)
        src = f"{link} (ab {format_hms(start)})" if link else f"ab {format_hms(start)}"
        lines.append(f"   Mit im Bild: {comp['creator_name']} | Original-VOD: {src}")
    return "\n".join(lines)


def _require_positions(clips: list[Candidate]) -> None:
    missing = [c.id for c in clips if c.highlight_start is None]
    if missing:
        raise ValueError(
            "Position im Highlight-Video fehlt (erst exportieren oder Positionen berechnen): "
            + ", ".join(missing)
        )


def build_chapters(clips: list[Candidate], total_duration: float) -> tuple[list[tuple[float, str]], list[str]]:
    chapters: list[tuple[float, str]] = []
    warnings: list[str] = []
    if clips and (clips[0].highlight_start or 0) > 0:
        chapters.append((0.0, "Intro"))
    for c in clips:
        chapters.append((c.highlight_start or 0.0, c.title))
    if chapters:
        chapters[0] = (0.0, chapters[0][1])  # YouTube verlangt 00:00 als erste Marke
    if len(chapters) < MIN_CHAPTERS:
        warnings.append(
            f"Nur {len(chapters)} Kapitel – YouTube erzeugt Kapitel erst ab {MIN_CHAPTERS} Marken."
        )
    ends = [s for s, _ in chapters[1:]] + [total_duration]
    for (start, title), end in zip(chapters, ends):
        if end - start < MIN_CHAPTER_SECONDS:
            warnings.append(
                f"Kapitel „{title}“ ist kürzer als {MIN_CHAPTER_SECONDS}s – YouTube ignoriert dann alle Kapitel."
            )
    return chapters, warnings


def _creator_list(clips: list[Candidate], registry: CreatorRegistry, settings: Settings) -> list[str]:
    seen: dict[str, CreditInfo] = {}
    for c in clips:
        people = [(c.creator_id, c.source.creator_name)] + [
            (comp["creator_id"], comp["creator_name"]) for comp in c.companions
        ]
        for creator_id, name in people:
            if creator_id not in seen:
                seen[creator_id] = credit_info(creator_id, name, registry, settings)
    lines = []
    for info in seen.values():
        line = f"• {info.name}"
        details = [p for p in (info.link_line(),) if p]
        if info.rules.link:
            details.append(info.rules.link)
        if details:
            line += " – " + " | ".join(details)
        lines.append(line)
    return lines


def _first_lines(clips: list[Candidate], registry: CreatorRegistry, settings: Settings) -> list[str]:
    out: list[str] = []
    for c in clips:
        info = credit_info(c.creator_id, c.source.creator_name, registry, settings)
        if info.rules.first_line and info.rules.first_line not in out:
            out.append(info.rules.first_line)
    return out


def build_description(
    project: Project,
    clips: list[Candidate],
    registry: CreatorRegistry,
    settings: Settings,
    total_duration: float,
    intro_text: str = "",
) -> DescriptionResult:
    """Beschreibung für ein Highlight-Video (Langform)."""
    _require_positions(clips)
    long_video = total_duration >= 3600
    chapters, warnings = build_chapters(clips, total_duration)

    def render(with_clip_links: bool, with_intro: bool) -> str:
        parts: list[str] = []
        first = _first_lines(clips, registry, settings)
        if first:
            parts.append("\n".join(first))
        if with_intro and intro_text.strip():
            parts.append(intro_text.strip())
        parts.append(
            "Kapitel:\n" + "\n".join(f"{format_chapter(s, long_video)} {t}" for s, t in chapters)
        )
        blocks = []
        for c in clips:
            info = credit_info(c.creator_id, c.source.creator_name, registry, settings)
            blocks.append(clip_credit_block(c, info, long_video, with_links=with_clip_links))
        parts.append("Quellen & Credits:\n\n" + "\n\n".join(blocks))
        parts.append("Alle Creator in diesem Video:\n" + "\n".join(_creator_list(clips, registry, settings)))
        return sanitize("\n\n".join(parts))

    text = render(True, True)
    if len(text) > YOUTUBE_DESCRIPTION_LIMIT:
        text = render(False, True)
        warnings.append(
            "Beschreibung zu lang: Kanal-Links pro Clip entfernt (stehen weiterhin in der Creator-Liste)."
        )
    if len(text) > YOUTUBE_DESCRIPTION_LIMIT:
        text = render(False, False)
        warnings.append("Beschreibung zu lang: Intro-Text entfernt.")
    if len(text) > YOUTUBE_DESCRIPTION_LIMIT:
        raise ValueError(
            f"Beschreibung ist mit {len(text)} Zeichen länger als das YouTube-Limit "
            f"({YOUTUBE_DESCRIPTION_LIMIT}), obwohl sie bereits gekürzt wurde. Bitte weniger Clips verwenden."
        )
    return DescriptionResult(text=text, chapters=chapters, warnings=warnings)


def build_short_description(
    project: Project, clips: list[Candidate], registry: CreatorRegistry, settings: Settings
) -> DescriptionResult:
    """Kurze Beschreibung für Shorts: Credit (Creator + Links) und #Shorts."""
    parts: list[str] = []
    first = _first_lines(clips, registry, settings)
    if first:
        parts.append("\n".join(first))
    if project.meta.intro_text.strip():
        parts.append(project.meta.intro_text.strip())
    for c in clips:
        info = credit_info(c.creator_id, c.source.creator_name, registry, settings)
        lines = [
            f"Clip von {info.name} – live am {format_de_datetime(c.live_start_dt)}",
            _original_vod_line(c),
        ]
        if info.link_line():
            lines.append(info.link_line())
        if info.rules.extra_line:
            lines.append(info.rules.extra_line)
        parts.append("\n".join(lines))
    parts.append("#Shorts")
    text = sanitize("\n\n".join(parts))
    if len(text) > YOUTUBE_DESCRIPTION_LIMIT:
        raise ValueError("Shorts-Beschreibung überschreitet das YouTube-Limit")
    return DescriptionResult(text=text)


def build_title(project: Project) -> str:
    title = sanitize((project.meta.title or project.name).strip())
    if project.meta.is_short and "#shorts" not in title.lower():
        suffix = " #Shorts"
        if len(title) + len(suffix) <= YOUTUBE_TITLE_LIMIT:
            title += suffix
    return title[:YOUTUBE_TITLE_LIMIT]


def build_tags(project: Project, settings: Settings) -> list[str]:
    tags: list[str] = []
    for tag in list(project.meta.tags) + list(settings["youtube"].get("default_tags", [])):
        tag = sanitize(str(tag)).strip()
        if tag and tag.lower() not in (t.lower() for t in tags):
            tags.append(tag)
    if project.meta.is_short and "shorts" not in (t.lower() for t in tags):
        tags.insert(0, "Shorts")
    # YouTube zählt Tags mit Leerzeichen inkl. Anführungszeichen
    out, total = [], 0
    for tag in tags:
        cost = len(tag) + (2 if " " in tag else 0) + (1 if out else 0)
        if total + cost > YOUTUBE_TAGS_LIMIT:
            break
        out.append(tag)
        total += cost
    return out
