import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from publisher import render, uploadlog
from publisher.creators import OWN_VOD
from publisher.export import export_project
from publisher.gate import UploadBlocked
from publisher.models import CLIP_REJECTED, CLIP_SELECTED
from publisher.webui.app import create_app
from publisher.youtube import publish_video, revoke_schedule_if_blocked, schedule_video, upload_project


class FakeRequest:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result

    def next_chunk(self):
        return None, self.result


class FakeYouTube:
    def __init__(self):
        self.calls = []
        self.status = {"privacyStatus": "private", "selfDeclaredMadeForKids": False, "uploadStatus": "processed"}

    def videos(self):
        return self

    def thumbnails(self):
        outer = self

        class T:
            def set(self, **kw):
                outer.calls.append(("thumbnails.set", kw))
                return FakeRequest({})

        return T()

    def insert(self, **kw):
        self.calls.append(("videos.insert", kw))
        self.status = dict(self.status, **kw["body"]["status"])
        return FakeRequest({"id": "vid123"})

    def list(self, **kw):
        self.calls.append(("videos.list", kw))
        return FakeRequest({"items": [{"id": kw["id"], "status": dict(self.status)}]})

    def update(self, **kw):
        self.calls.append(("videos.update", kw))
        self.status = kw["body"]["status"]
        return FakeRequest(kw["body"])

    def names(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def rendered(project, registry, settings, sample_video):
    for vod in project.vods.values():
        vod.path = str(sample_video)
        vod.duration = render.probe_duration(sample_video)
    project.apply_review_edit(project.candidates[0].id, {"trim_start": 1, "trim_end": 5})
    project.apply_review_edit(project.candidates[1].id, {"trim_start": 6, "trim_end": 9})
    project.meta.title = "Highlights"
    project.save()
    manifest = export_project(project, registry, settings)
    return project, manifest


def test_export_renders_video_with_positions(rendered, sample_video):
    project, manifest = rendered
    duration = render.probe_duration(manifest["video"])
    # 4 s + 3 s mit 0,5 s Überblendung dazwischen
    assert abs(duration - 6.5) < 0.3
    clips = manifest["clips"]
    assert clips[0]["highlight_start"] == 0
    assert abs(clips[1]["highlight_start"] - 3.5) < 0.2
    assert abs(manifest["duration"] - duration) < 0.3
    assert clips[0]["original_detection"]["vod_start"] == 3723  # Original-Erkennung bleibt erhalten
    assert clips[0]["creator_name"] == "Anna"
    out = project.dir / "exports"
    overlays = sorted(out.glob("*/overlay_*.txt"))
    assert [o.read_text(encoding="utf-8") for o in overlays] == ["Clip von Anna – twitch.tv/anna_live"]
    stored = json.loads(open(project.last_export["manifest"], encoding="utf-8").read())
    assert stored["description"].startswith("Clips mit Genehmigung von Anna")
    assert "Kapitel:\n00:00 Der große Fail\n00:03 Mein Moment" in stored["description"]


def test_export_without_transitions(project, registry, settings, sample_video):
    for vod in project.vods.values():
        vod.path = str(sample_video)
        vod.duration = 12
    project.apply_review_edit(project.candidates[0].id, {"trim_start": 1, "trim_end": 5})
    project.apply_review_edit(project.candidates[1].id, {"trim_start": 6, "trim_end": 9})
    settings.raw["transitions"]["enabled"] = False
    manifest = export_project(project, registry, settings)
    assert abs(render.probe_duration(manifest["video"]) - 7) < 0.3
    assert abs(manifest["clips"][1]["highlight_start"] - 4) < 0.2


def test_split_screen_clip(project, registry, settings, sample_video):
    """Zwei Streamer gleichzeitig: Splitscreen, beide in Credits und Freigabe-Prüfung."""
    from publisher.gate import find_blockers

    for vod in project.vods.values():
        vod.path = str(sample_video)
        vod.duration = 12
    anna_vod, own_vod = list(project.vods)
    v_ben = project.add_vod(str(sample_video), "ben", "Ben", datetime(2026, 10, 12, 20, 14, tzinfo=settings.tz),
                            vod_url="https://www.twitch.tv/videos/777", duration=12)
    clip = project.candidates[0]
    project.apply_review_edit(clip.id, {"trim_start": 2, "trim_end": 6})
    project.apply_review_edit(project.candidates[1].id, {"status": CLIP_REJECTED})
    clip.companions = [{"vod_id": v_ben.id, "creator_id": "ben", "creator_name": "Ben",
                        "vod_url": v_ben.vod_url, "live_start": v_ben.live_start, "offset": 1.0}]
    project.save()
    manifest = export_project(project, registry, settings)
    info = render.probe(manifest["video"])
    assert (info["width"], info["height"]) == (320, 180) and abs(info["duration"] - 4) < 0.3
    assert "Mit im Bild: Ben | Original-VOD: https://www.twitch.tv/videos/777?t=0h0m3s (ab 0:00:03)" in manifest["description"]
    assert "• Ben" in manifest["description"]
    assert any(b.creator_id == "ben" for b in find_blockers(project, registry))  # Ben ist nur „angefragt“


def test_overlay_only_first_seconds(settings):
    vf = render.video_filter(settings, False, "overlay_01.txt")
    assert "enable='between(t,0,3.0)'" in vf and "textfile=overlay_01.txt" in vf
    short = render.video_filter(settings, True, "overlay_01.txt")
    assert "enable" not in short and "crop=" in short


def test_export_allowed_even_when_blocked(project, registry, settings, sample_video):
    v = project.add_vod(str(sample_video), "cara", "Cara", datetime(2026, 10, 1, 12, tzinfo=settings.tz), duration=12)
    c = project.add_candidate(v.id, 0, 2, "Gesperrt")
    project.apply_review_edit(c.id, {"status": CLIP_SELECTED})
    manifest = export_project(project, registry, settings, do_render=False)
    assert manifest["upload_blocked_at_export"]


def test_short_export(project, registry, settings, sample_video):
    for vod in project.vods.values():
        vod.path = str(sample_video)
        vod.duration = 12
    project.apply_review_edit(project.candidates[1].id, {"status": CLIP_REJECTED})
    project.apply_review_edit(project.candidates[0].id, {"trim_start": 0, "trim_end": 3})
    project.meta.is_short = True
    project.meta.title = "Fail"
    manifest = export_project(project, registry, settings)
    info = render.probe(manifest["video"])
    assert (info["width"], info["height"]) == (180, 320) and info["has_audio"]
    assert manifest["title"] == "Fail #Shorts"
    assert "#Shorts" in manifest["description"] and "Clip von Anna" in manifest["description"]


def test_upload_private_with_log(rendered, registry, settings, workdir):
    project, manifest = rendered
    (workdir / "thumb.jpg").write_bytes(b"\xff\xd8\xff")
    project.meta.thumbnail = "thumb.jpg"
    yt = FakeYouTube()
    rec = upload_project(project, registry, settings, service=yt)
    insert = yt.calls[0][1]
    assert insert["body"]["status"]["privacyStatus"] == "private"
    assert "publishAt" not in insert["body"]["status"]
    assert insert["body"]["snippet"]["description"] == manifest["description"]
    assert yt.names() == ["videos.insert", "thumbnails.set"]
    assert rec["video_id"] == "vid123"
    log = uploadlog.read(settings.upload_log_file, project.name)
    assert log[0]["event"] == "upload" and log[0]["creators"] == ["Anna", "Ich"]
    assert log[0]["sources"][0]["vod_url"] == "https://www.twitch.tv/videos/123456"


def test_upload_scheduled(rendered, registry, settings):
    project, _ = rendered
    when = datetime.now(timezone.utc) + timedelta(days=2)
    yt = FakeYouTube()
    rec = upload_project(project, registry, settings, service=yt, publish_at=when)
    status = yt.calls[0][1]["body"]["status"]
    assert status["privacyStatus"] == "private"
    assert re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.000Z", status["publishAt"])
    assert rec["publish_at"] == status["publishAt"]
    with pytest.raises(ValueError):
        upload_project(project, registry, settings, service=yt, publish_at=datetime.now(timezone.utc))


def test_upload_blocked_makes_no_api_calls(rendered, registry, settings):
    project, _ = rendered
    registry.get("anna").permission_status = "angefragt"
    yt = FakeYouTube()
    with pytest.raises(UploadBlocked):
        upload_project(project, registry, settings, service=yt)
    assert yt.calls == []
    assert uploadlog.read(settings.upload_log_file) == []


def test_upload_blocked_by_exported_clip_after_deselect(rendered, registry, settings):
    """Clip aus dem Export entfernt, aber nicht neu exportiert: das Video enthält ihn noch."""
    project, _ = rendered
    registry.get("anna").permission_status = "nicht erlaubt"
    project.apply_review_edit(project.candidates[0].id, {"status": CLIP_REJECTED})
    with pytest.raises(UploadBlocked):
        upload_project(project, registry, settings, service=FakeYouTube())


def test_publish_and_schedule(rendered, registry, settings):
    project, _ = rendered
    yt = FakeYouTube()
    upload_project(project, registry, settings, service=yt)
    when = datetime.now(timezone.utc) + timedelta(hours=5)
    schedule_video(project, registry, settings, when, service=yt)
    assert yt.status["publishAt"].endswith("Z") and yt.status["privacyStatus"] == "private"
    publish_video(project, registry, settings, service=yt)
    assert yt.status["privacyStatus"] == "public" and "publishAt" not in yt.status
    assert "uploadStatus" not in yt.status  # nur beschreibbare Felder zurückschicken
    events = [e["event"] for e in uploadlog.read(settings.upload_log_file)]
    assert events == ["upload", "schedule", "publish"]


def test_publish_blocked_after_revocation(rendered, registry, settings):
    project, _ = rendered
    yt = FakeYouTube()
    upload_project(project, registry, settings, service=yt)
    registry.get("anna").permission_status = "nicht erlaubt"
    n = len(yt.calls)
    with pytest.raises(UploadBlocked):
        publish_video(project, registry, settings, service=yt)
    assert len(yt.calls) == n


def test_revoke_schedule(rendered, registry, settings):
    project, _ = rendered
    yt = FakeYouTube()
    upload_project(project, registry, settings, service=yt, publish_at=datetime.now(timezone.utc) + timedelta(days=1))
    assert not revoke_schedule_if_blocked(project, registry, settings, service=yt)
    registry.get("anna").permission_status = "angefragt"
    assert revoke_schedule_if_blocked(project, registry, settings, service=yt)
    assert "publishAt" not in yt.status and project.last_upload["publish_at"] is None


# ---------- Review-UI ----------

@pytest.fixture
def ui(workdir):
    yt = FakeYouTube()
    app = create_app(str(workdir / "config.yaml"), youtube_service_factory=lambda: yt)
    app.config["TESTING"] = True
    client = app.test_client()
    client.get("/")
    with client.session_transaction() as s:
        token = s["csrf"]
    return client, token, yt


def test_ui_shows_blocking_clip_and_disables_upload(ui, rendered, registry, settings):
    client, token, yt = ui
    project, _ = rendered
    v = project.add_vod("/b.mp4", "ben", "Ben", datetime(2026, 10, 1, 12, tzinfo=settings.tz))
    c = project.add_candidate(v.id, 0, 30, "Bens Clip")
    project.apply_review_edit(c.id, {"status": CLIP_SELECTED})
    project.save()
    html = client.get("/p/test").get_data(as_text=True)
    assert "Upload gesperrt" in html
    assert f'href="#{c.id}">Bens Clip</a> – Ben: „angefragt“' in html
    assert "Dieser Clip blockiert den Upload" in html
    assert re.search(r"<button disabled[^>]*>Privat hochladen</button>", html)
    assert "Clip-Auswahl wurde seit dem letzten Export geändert" in html


def test_ui_upload_post_is_blocked_server_side(ui, rendered, registry):
    client, token, yt = ui
    project, _ = rendered
    text = (project.dir.parents[2] / "creators.yaml").read_text(encoding="utf-8")
    (project.dir.parents[2] / "creators.yaml").write_text(
        text.replace("permission_status: erlaubt", "permission_status: angefragt"), encoding="utf-8"
    )
    resp = client.post("/p/test/upload", data={"csrf": token}, follow_redirects=True)
    assert "Upload gesperrt" in resp.get_data(as_text=True)
    assert yt.calls == []


def test_ui_upload_and_confirmed_publish(ui, rendered):
    client, token, yt = ui
    html = client.get("/p/test").get_data(as_text=True)
    assert re.search(r"<button\s*>Privat hochladen</button>", html)
    client.post("/p/test/upload", data={"csrf": token})
    assert yt.names()[0] == "videos.insert"
    client.post("/p/test/publish", data={"csrf": token})  # ohne Bestätigung
    assert "videos.update" not in yt.names()
    client.post("/p/test/publish", data={"csrf": token, "confirm": "ja"})
    assert yt.status["privacyStatus"] == "public"


def test_ui_csrf_required(ui, project):
    client, token, yt = ui
    assert client.post("/p/test/upload", data={}).status_code == 400


def test_ui_clip_edit_keeps_source(ui, project):
    client, token, _ = ui
    clip = project.candidates[0]
    client.post(
        f"/p/test/clip/{clip.id}",
        data={"csrf": token, "title": "Neu", "status": "verworfen", "order": "3",
              "trim_start": "1:01:00", "trim_end": "1:02:00"},
    )
    from publisher.models import Project

    c = Project.load(project.path).candidate(clip.id)
    assert (c.title, c.status, c.trim_start) == ("Neu", "verworfen", 3660)
    assert c.source == clip.source
