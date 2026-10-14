import hashlib
import json
import threading
import urllib.parse
import urllib.request

import pytest

from publisher import tiktok, videobox
from publisher.auto import Options, make_highlights
from publisher.gate import UploadBlocked


# ---------------------------------------------------------------- TikTok

def test_chunk_plan_rules():
    mb = 1024 * 1024
    assert tiktok.chunk_plan(3 * mb) == (3 * mb, 1)  # < 5 MB: am Stück
    size, count = tiktok.chunk_plan(47 * mb)
    assert size == 10 * mb and count == 4  # Rest (7 MB) hängt am letzten Chunk
    assert tiktok.chunk_plan(6 * mb) == (6 * mb, 1)


def test_pkce_hex_and_base64():
    v, c = tiktok.pkce_pair("hex")
    assert 43 <= len(v) <= 128 and c == hashlib.sha256(v.encode()).hexdigest()
    v, c = tiktok.pkce_pair("base64url")
    assert "=" not in c and len(c) == 43


def test_connect_flow_with_local_callback(settings, monkeypatch):
    settings.raw["tiktok"] = {"redirect_port": 34567}
    tiktok.save_app_credentials(settings, "key123", "secret456")
    sent = {}

    def fake_form(url, fields):
        sent.update(fields)
        return {"access_token": "AT", "refresh_token": "RT", "open_id": "u1", "expires_in": 86400}

    monkeypatch.setattr(tiktok, "_form", fake_form)
    monkeypatch.setattr(tiktok, "display_name", lambda s: "Mein TikTok")

    def browser(url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        assert q["redirect_uri"] == "http://127.0.0.1:34567/callback/" and q["code_challenge_method"] == "S256"
        assert q["scope"] == "user.info.basic,video.publish"
        cb = f"{q['redirect_uri']}?code=CODE1&state={q['state']}"
        threading.Thread(target=lambda: urllib.request.urlopen(cb, timeout=5).read(), daemon=True).start()

    assert tiktok.connect(settings, open_browser=browser, timeout=10) == "Mein TikTok"
    assert sent["code"] == "CODE1" and sent["grant_type"] == "authorization_code" and sent["code_verifier"]
    assert tiktok.is_connected(settings) and tiktok.access_token(settings) == "AT"


def test_upload_video_chunks(settings, tmp_path, monkeypatch):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * (12 * 1024 * 1024))
    calls, puts = [], []

    def fake_post(s, url, body):
        calls.append((url, body))
        if url == tiktok.CREATOR_INFO_URL:
            return {"privacy_level_options": ["SELF_ONLY", "PUBLIC_TO_EVERYONE"]}
        if url == tiktok.INIT_URL:
            return {"publish_id": "p1", "upload_url": "https://upload.example/x?y=1"}
        return {"status": "PUBLISH_COMPLETE"}

    class Resp:
        def read(self):
            return b""

    def fake_urlopen(req, timeout=0):
        puts.append(req.headers["Content-range"])
        return Resp()

    monkeypatch.setattr(tiktok, "_post_json", fake_post)
    monkeypatch.setattr(tiktok.urllib.request, "urlopen", fake_urlopen)
    res = tiktok.upload_video(video, settings, title="Test", privacy_level="SELF_ONLY")
    init = calls[1][1]
    assert init["source_info"] == {"source": "FILE_UPLOAD", "video_size": 12582912, "chunk_size": 10485760, "total_chunk_count": 1}
    assert puts == ["bytes 0-12582911/12582912"]
    assert res["status"] == "PUBLISH_COMPLETE"
    with pytest.raises(tiktok.TikTokError):
        tiktok.upload_video(video, settings, title="T", privacy_level="FOLLOWER_OF_CREATOR")


# ---------------------------------------------------------------- Videobox

@pytest.fixture
def box_item(tmp_path, settings):
    from tests.test_auto import _pov

    a = _pov(tmp_path, "anna", "red", 0, [100, 180], dur=200)
    make_highlights([str(a)], settings, options=Options(minutes=1), output_root=tmp_path / "Fertig")
    items = videobox.list_items(tmp_path / "Fertig")
    assert len(items) == 1
    return items[0]


def test_videobox_lists_files_and_thumbs(box_item):
    kinds = [f.kind for f in box_item.files]
    assert kinds[0] == "video" and "clip" in kinds and "vertical" in kinds
    assert videobox.thumbnail(box_item.video.path).exists()


def test_videobox_youtube_upload_records(box_item, settings):
    from tests.test_pipeline import FakeYouTube

    yt = FakeYouTube()
    vertical = next(f for f in box_item.files if f.kind == "vertical")
    title, desc = videobox.default_texts(box_item, vertical, settings, "youtube")
    assert title.endswith("#Shorts") and "#Shorts" in desc
    entry = videobox.upload_youtube(box_item, vertical, settings, title=title, description=desc, service=yt)
    assert yt.calls[0][1]["body"]["status"]["privacyStatus"] == "private"
    assert entry["file"].startswith("Hochformat/") and box_item.uploads_for(vertical.path)
    again = videobox.list_items(box_item.folder.parent)[0]
    assert again.info["uploads"][0]["video_id"] == "vid123"


def test_videobox_upload_blocked_for_unapproved_creator(tmp_path, settings, monkeypatch):
    """Clip eines fremden Creators (Status „angefragt“) darf nicht hochgeladen werden."""
    from tests.test_auto import _pov
    from tests.test_pipeline import FakeYouTube
    from publisher import fetch

    a = _pov(tmp_path, "ben", "blue", 0, [100], dur=160)
    info = {"extractor_key": "TwitchVod", "uploader": "Ben", "uploader_id": "ben", "timestamp": 1760292900,
            "webpage_url": "https://www.twitch.tv/videos/5", "title": "Abend", "duration": 160}
    monkeypatch.setattr(fetch, "fetch_info", lambda url: info)
    import shutil

    def fake_audio(url, dest, progress=None):
        dest.mkdir(parents=True, exist_ok=True)
        return shutil.copy(a, dest / "ton.mp4") and dest / "ton.mp4"

    monkeypatch.setattr(fetch, "download_audio", fake_audio)
    monkeypatch.setattr(fetch, "download_sections", lambda url, ranges, dest, progress=None:
                        [{"start": 0.0, "end": 160.0, "path": str(a)}])
    make_highlights("https://www.twitch.tv/videos/5", settings, options=Options(minutes=1, vertical=False),
                    output_root=tmp_path / "Fertig")
    item = videobox.list_items(tmp_path / "Fertig")[0]
    yt = FakeYouTube()
    with pytest.raises(UploadBlocked):
        videobox.upload_youtube(item, item.video, settings, title="t", description="d", service=yt)
    assert yt.calls == []
    # Freigabe erteilen → Upload klappt
    from publisher.creators import CreatorRegistry

    reg = CreatorRegistry.load(settings.creators_file)
    reg.get("ben").permission_status = "erlaubt"
    reg.save()
    videobox.upload_youtube(item, item.video, settings, title="t", description="d", service=yt)
    assert yt.calls
