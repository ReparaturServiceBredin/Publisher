from datetime import datetime
from pathlib import Path

import pytest

from publisher.creators import Creator, CreatorRegistry, handle_from_url
from publisher.description import build_description, build_short_description, build_tags, build_title
from publisher.export import assign_positions
from publisher.gate import UploadBlocked, assert_upload_allowed, find_blockers
from publisher.importer import import_vod
from publisher.llm import generate_intro
from publisher.models import CLIP_REJECTED, CLIP_SELECTED, Project, ProjectError
from publisher.timefmt import format_chapter, parse_timestamp, vod_link_at

ROOT = Path(__file__).resolve().parents[1]


# ---------- Creator-Register ----------

def test_example_registry_loads():
    reg = CreatorRegistry.load(ROOT / "creators.yaml")
    assert len(reg) >= 1
    assert all(c.permission_status in ("erlaubt", "angefragt", "nicht erlaubt") for c in reg)


def test_invalid_status_rejected():
    with pytest.raises(ValueError):
        Creator.from_dict({"name": "X", "permission_status": "vielleicht"})


def test_unknown_field_rejected():
    with pytest.raises(ValueError):
        Creator.from_dict({"name": "X", "permission": "erlaubt"})


def test_overlay_text(registry):
    assert registry.get("anna").overlay_text() == "Clip von Anna – twitch.tv/anna_live"
    assert registry.get("cara").overlay_text() == "Clip von Cara"
    assert handle_from_url("https://www.youtube.com/@Foo") == "@Foo"


def test_registry_roundtrip(registry, tmp_path):
    registry.save(tmp_path / "out.yaml")
    again = CreatorRegistry.load(tmp_path / "out.yaml")
    assert [c.to_dict() for c in again] == [c.to_dict() for c in registry]


# ---------- Zeit ----------

@pytest.mark.parametrize("text,sec", [("1:02:03", 3723), ("62:03", 3723), ("90", 90), ("0:00:01.5", 1.5)])
def test_parse_timestamp(text, sec):
    assert parse_timestamp(text) == sec


def test_vod_links():
    assert vod_link_at("https://www.twitch.tv/videos/1", 3723) == "https://www.twitch.tv/videos/1?t=1h2m3s"
    assert vod_link_at("https://youtu.be/abc", 75) == "https://youtu.be/abc?t=75s"
    assert vod_link_at("https://www.youtube.com/watch?v=x&t=5", 75) == "https://www.youtube.com/watch?v=x&t=75s"
    assert vod_link_at("https://example.com/v", 10) == "https://example.com/v"
    assert format_chapter(222) == "03:42" and format_chapter(3723) == "1:02:03"


# ---------- Quellen-Tracking ----------

def test_source_is_preserved_after_review_edits(project):
    clip = project.candidates[0]
    original = clip.source
    project.apply_review_edit(clip.id, {"title": "Neu", "trim_start": 3700, "trim_end": 3790})
    project.apply_review_edit(clip.id, {"status": CLIP_REJECTED})
    project.save()
    reloaded = Project.load(project.path)
    c = reloaded.candidate(clip.id)
    assert c.source == original
    assert c.source.vod_start == 3723 and c.source.vod_end == 3780
    assert c.status == CLIP_REJECTED  # verworfen, aber nicht gelöscht
    assert {h["field"] for h in c.history} >= {"title", "trim_start", "trim_end", "status"}


def test_source_fields_not_editable(project):
    clip = project.candidates[0]
    with pytest.raises(ProjectError):
        project.apply_review_edit(clip.id, {"source": None})
    with pytest.raises(ProjectError):
        project.apply_review_edit(clip.id, {"creator_id": "anna"})
    with pytest.raises(AttributeError):
        clip.source.creator_id = "x"  # frozen


def test_wallclock_times(project):
    clip = project.candidates[0]
    # Live-Start 20:15 + 1:02:03 = 21:17:03
    assert clip.live_start_dt.strftime("%d.%m.%Y %H:%M:%S") == "12.10.2026 21:17:03"
    assert clip.source.live_clip_start.startswith("2026-10-12T21:17:03")


def test_wallclock_crosses_midnight(store, settings):
    p = store.create("nacht")
    v = p.add_vod("/x.mp4", "anna", "Anna", datetime(2026, 10, 12, 23, 50, tzinfo=settings.tz))
    c = p.add_candidate(v.id, 1200, 1260)
    assert c.live_start_dt.strftime("%d.%m. %H:%M") == "13.10. 00:10"


def test_invalid_range(project):
    clip = project.candidates[0]
    with pytest.raises(ProjectError):
        project.apply_review_edit(clip.id, {"trim_end": 10})


def test_project_name_validation(store):
    with pytest.raises(ProjectError):
        store.load("../etc")


# ---------- Import ----------

def test_import_interactive(store, registry, settings, workdir):
    video = workdir / "v.mp4"
    video.write_bytes(b"")
    p = store.create("imp")
    answers = iter(["2", "12.10.2026", "20:15", "https://www.twitch.tv/videos/9", "Titel"])
    out = []
    vod = import_vod(p, registry, settings, str(video), ask=lambda _: next(answers), out=out.append, duration=100)
    assert vod.creator_id == "ben" and vod.vod_url.endswith("/9") and vod.stream_title == "Titel"
    assert vod.live_start_dt.hour == 20 and vod.live_start_dt.utcoffset() is not None
    assert any("Upload bleibt gesperrt" in line for line in out)


def test_import_own_vod(store, registry, settings, workdir):
    video = workdir / "v.mp4"
    video.write_bytes(b"")
    p = store.create("imp2")
    answers = iter(["0", "01.10.2026", "18:00", "", ""])
    vod = import_vod(p, registry, settings, str(video), ask=lambda _: next(answers), out=lambda _: None, duration=1)
    assert vod.is_own and vod.creator_name == "Ich"


def test_import_unknown_creator(store, registry, settings, workdir):
    video = workdir / "v.mp4"
    video.write_bytes(b"")
    with pytest.raises(ProjectError):
        import_vod(store.create("x"), registry, settings, str(video), creator_id="nobody",
                   date="01.10.2026", time="18:00", interactive=False)


# ---------- Sicherheitsregeln ----------

def test_gate_allows_allowed_and_own(project, registry):
    assert find_blockers(project, registry) == []
    assert_upload_allowed(project, registry)


@pytest.mark.parametrize("creator_id,status", [("ben", "angefragt"), ("cara", "nicht erlaubt")])
def test_gate_blocks_not_allowed(project, registry, settings, creator_id, status):
    v = project.add_vod("/b.mp4", creator_id, creator_id, datetime(2026, 10, 1, 12, tzinfo=settings.tz))
    c = project.add_candidate(v.id, 0, 30, "Fremder Clip")
    project.apply_review_edit(c.id, {"status": CLIP_SELECTED})
    blockers = find_blockers(project, registry)
    assert [(b.clip_id, b.status) for b in blockers] == [(c.id, status)]
    with pytest.raises(UploadBlocked) as exc:
        assert_upload_allowed(project, registry)
    assert "Fremder Clip" in str(exc.value)


def test_gate_ignores_rejected_and_candidates(project, registry, settings):
    v = project.add_vod("/b.mp4", "ben", "Ben", datetime(2026, 10, 1, 12, tzinfo=settings.tz))
    project.add_candidate(v.id, 0, 30)  # nur Kandidat
    c = project.add_candidate(v.id, 40, 60)
    project.apply_review_edit(c.id, {"status": CLIP_REJECTED})
    assert find_blockers(project, registry) == []


def test_gate_unknown_creator_blocks(project, registry, settings):
    v = project.add_vod("/b.mp4", "geloescht", "Gelöscht", datetime(2026, 10, 1, 12, tzinfo=settings.tz))
    c = project.add_candidate(v.id, 0, 30)
    project.apply_review_edit(c.id, {"status": CLIP_SELECTED})
    assert find_blockers(project, registry)[0].status.startswith("unbekannt")


def test_gate_status_change_in_registry(project, registry):
    registry.get("anna").permission_status = "nicht erlaubt"
    assert [b.creator_id for b in find_blockers(project, registry)] == ["anna"]


def test_gate_checks_exported_clips_even_if_deselected(project, registry):
    project.exports.append({"manifest": None, "clips": [
        {"clip_id": "clip-alt", "title": "Alt", "creator_id": "cara", "creator_name": "Cara"}]})
    blockers = find_blockers(project, registry)
    assert blockers and blockers[0].origin == "export"


# ---------- Beschreibung ----------

def _positioned(project, offset=0.0):
    clips = project.selected_clips()
    total = assign_positions(clips, [c.duration for c in clips], offset)
    return clips, total


def test_description_structure(project, registry, settings):
    clips, total = _positioned(project)
    clips[1].highlight_start  # eigenes VOD
    desc = build_description(project, clips, registry, settings, total, "Ein wilder Abend <3")
    text = desc.text
    lines = text.splitlines()
    assert lines[0] == "Clips mit Genehmigung von Anna – twitch.tv/anna_live"  # Credit-Vorgabe als erster Satz
    assert "Ein wilder Abend ‹3" in text  # '<' ist bei YouTube verboten
    assert "Kapitel:\n00:00 Der große Fail\n00:57 Mein Moment" in text
    block = (
        "▶ 00:00 – Der große Fail\n"
        "   Creator: Anna | Live am 12.10.2026, 21:17 Uhr\n"
        "   Original-VOD: https://www.twitch.tv/videos/123456?t=1h2m3s (ab 1:02:03)\n"
        "   YouTube: https://www.youtube.com/@Anna | Twitch: https://www.twitch.tv/Anna_Live"
    )
    assert block in text
    assert "▶ 00:57 – Mein Moment\n   Creator: Ich | Live am 13.10.2026, 18:01 Uhr" in text
    assert "Alle Creator in diesem Video:\n• Anna – YouTube:" in text
    assert "• Ich – Twitch: https://twitch.tv/ich" in text
    assert any("3 Marken" in w for w in desc.warnings)


def test_description_intro_chapter(project, registry, settings):
    clips, total = _positioned(project, offset=15)
    desc = build_description(project, clips, registry, settings, total)
    assert desc.chapters[0] == (0.0, "Intro")
    assert "00:00 Intro\n00:15 Der große Fail" in desc.text


def test_description_trim_updates_credit(project, registry, settings):
    clip = project.candidates[0]
    project.apply_review_edit(clip.id, {"trim_start": 3600})
    clips, total = _positioned(project)
    text = build_description(project, clips, registry, settings, total).text
    assert "?t=1h0m0s (ab 1:00:00)" in text and "Live am 12.10.2026, 21:15 Uhr" in text


def test_description_too_long_is_compacted(project, registry, settings):
    vod = next(iter(project.vods.values()))
    for i in range(20):
        c = project.add_candidate(vod.id, i * 60, i * 60 + 30, f"Clip Nummer {i}")
        project.apply_review_edit(c.id, {"status": CLIP_SELECTED})
    clips, total = _positioned(project)
    desc = build_description(project, clips, registry, settings, total, "Intro")
    assert len(desc.text) <= 5000
    assert any("Kanal-Links" in w for w in desc.warnings)
    assert "YouTube: https://www.youtube.com/@Anna" in desc.text  # noch in der Creator-Liste


def test_description_way_too_long_raises(project, registry, settings):
    vod = next(iter(project.vods.values()))
    for i in range(80):
        c = project.add_candidate(vod.id, i * 40, i * 40 + 30, f"Clip {i}")
        project.apply_review_edit(c.id, {"status": CLIP_SELECTED})
    clips, total = _positioned(project)
    with pytest.raises(ValueError, match="YouTube-Limit"):
        build_description(project, clips, registry, settings, total)


def test_description_requires_positions(project, registry, settings):
    with pytest.raises(ValueError):
        build_description(project, project.selected_clips(), registry, settings, 100)


def test_short_description_and_title(project, registry, settings):
    project.meta.is_short = True
    project.meta.title = "Der große Fail"
    clip = project.candidates[0]
    desc = build_short_description(project, [clip], registry, settings)
    assert "Clip von Anna – live am 12.10.2026, 21:17 Uhr" in desc.text
    assert "Twitch: https://www.twitch.tv/Anna_Live" in desc.text
    assert desc.text.rstrip().endswith("#Shorts")
    assert build_title(project) == "Der große Fail #Shorts"
    assert build_tags(project, settings)[0] == "Shorts"


def test_title_limit(project):
    project.meta.title = "x" * 120
    assert len(build_title(project)) == 100


# ---------- LLM ----------

def test_llm_disabled_fallback(project, settings):
    text, warning = generate_intro(project, project.selected_clips(), settings)
    assert "Anna" in text and warning


def test_llm_call(project, settings, monkeypatch):
    import anthropic

    settings.raw["llm"]["enabled"] = True
    calls = {}

    class FakeBlock:
        type = "text"
        text = "  Ein Abend voller Chaos.  "

    class FakeResp:
        stop_reason = "end_turn"
        content = [FakeBlock()]

    class FakeClient:
        def __init__(self):
            self.beta = self
            self.messages = self

        def create(self, **kwargs):
            calls.update(kwargs)
            return FakeResp()

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    (settings.base_dir / "style_references").mkdir()
    (settings.base_dir / "style_references" / "ref.txt").write_text("Heute wieder Wahnsinn!", encoding="utf-8")
    text, warning = generate_intro(project, project.selected_clips(), settings)
    assert text == "Ein Abend voller Chaos." and warning is None
    assert calls["model"] == "claude-opus-5-5"
    assert calls["fallbacks"] == "default"
    assert "Heute wieder Wahnsinn!" in calls["messages"][0]["content"]
