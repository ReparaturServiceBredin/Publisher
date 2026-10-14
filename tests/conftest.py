import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from publisher.config import load_settings
from publisher.creators import OWN_VOD, CreatorRegistry
from publisher.models import CLIP_SELECTED, ProjectStore

CREATORS = {
    "creators": [
        {
            "id": "anna",
            "name": "Anna",
            "youtube_channel_url": "https://www.youtube.com/@Anna",
            "twitch_channel_url": "https://www.twitch.tv/Anna_Live",
            "permission_status": "erlaubt",
            "permission_proof": "Mail vom 01.10.2026",
            "credit": {"first_line": "Clips mit Genehmigung von Anna – twitch.tv/anna_live"},
        },
        {
            "id": "ben",
            "name": "Ben",
            "twitch_channel_url": "https://twitch.tv/benben",
            "permission_status": "angefragt",
        },
        {"id": "cara", "name": "Cara", "permission_status": "nicht erlaubt"},
    ]
}


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    (tmp_path / "creators.yaml").write_text(yaml.safe_dump(CREATORS, allow_unicode=True), encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "own_channel": {"name": "Ich", "twitch_channel_url": "https://twitch.tv/ich"},
                "llm": {"enabled": False},
                "video": {"width": 320, "height": 180, "fps": 25},
                "short_video": {"width": 180, "height": 320, "fps": 25},
                "overlay": {"fontsize": 12},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def settings(workdir):
    return load_settings(workdir / "config.yaml")


@pytest.fixture
def registry(settings):
    return CreatorRegistry.load(settings.creators_file)


@pytest.fixture
def store(settings):
    return ProjectStore(settings.projects_dir)


@pytest.fixture
def project(store, settings):
    """Projekt mit einem Anna-VOD und einem eigenen VOD, je ein ausgewählter Clip."""
    p = store.create("test")
    tz = settings.tz
    va = p.add_vod(
        "/videos/anna.mp4", "anna", "Anna", datetime(2026, 10, 12, 20, 15, tzinfo=tz),
        vod_url="https://www.twitch.tv/videos/123456", stream_title="Chaos-Abend",
    )
    vo = p.add_vod("/videos/ich.mp4", OWN_VOD, "Ich", datetime(2026, 10, 13, 18, 0, tzinfo=tz))
    a = p.add_candidate(va.id, 3723, 3780, "Der große Fail")
    o = p.add_candidate(vo.id, 60, 100, "Mein Moment")
    for c in (a, o):
        p.apply_review_edit(c.id, {"status": CLIP_SELECTED})
    p.save()
    return p


def _have_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


needs_ffmpeg = pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg nicht installiert")


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory) -> Path:
    if not _have_ffmpeg():
        pytest.skip("ffmpeg nicht installiert")
    out = tmp_path_factory.mktemp("media") / "vod.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=12",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(out),
        ],
        check=True,
    )
    return out
