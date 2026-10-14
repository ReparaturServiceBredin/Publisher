from datetime import timezone

import numpy as np
import pytest

from publisher import detect, fetch, updater
from publisher.auto import _merge_ranges, make_highlights, safe_filename
from publisher.ffmpeg_bin import ffmpeg_exe


def test_pick_highlights_finds_peaks_and_respects_target():
    curve = np.zeros(1200, dtype=np.float32)
    for t, v in ((300, 6.0), (700, 9.0), (1000, 4.0)):
        curve[t:t + 5] = v
    picks = detect.pick_highlights(curve, target_seconds=10_000)
    assert [round(p.peak) // 100 for p in picks] == [3, 7, 10]
    assert all(p.start < p.peak < p.end for p in picks)
    # Ziel kleiner → nur der stärkste Moment
    assert [round(p.peak) // 100 for p in detect.pick_highlights(curve, target_seconds=20)] == [7]


def test_pick_highlights_ignores_flat_signal():
    assert detect.pick_highlights(np.zeros(600, dtype=np.float32), 300) == []


def test_target_length():
    assert detect.target_length(4 * 3600, 12) == 720
    assert detect.target_length(600, 12) == 180


def test_merge_ranges():
    assert _merge_ranges([(50, 80), (0, 30), (33, 40)]) == [(0, 40), (50, 80)]


def test_safe_filename():
    assert safe_filename('Wahnsinn: "Clip" <1>?') == "Wahnsinn Clip 1"


def test_source_metadata_twitch():
    meta = fetch.source_metadata({
        "extractor_key": "TwitchVod", "uploader": "Anna", "uploader_id": "Anna_Live",
        "timestamp": 1760292900, "webpage_url": "https://www.twitch.tv/videos/1", "title": "Chaos", "duration": 3600,
    })
    assert meta["twitch_channel_url"] == "https://www.twitch.tv/anna_live"
    assert meta["live_start"].tzinfo == timezone.utc and meta["duration"] == 3600


def test_url_detection():
    assert fetch.is_url("https://youtu.be/x") and fetch.is_url("www.twitch.tv/videos/1")
    assert not fetch.is_url(r"C:\Videos\stream.mp4")


def test_update_version_compare(monkeypatch):
    assert updater.parse_version("v1.0.12") > updater.parse_version("1.0.9")
    assert not updater.can_self_update()  # nur in der Windows-.exe


def test_make_highlights_from_file(tmp_path, settings):
    import subprocess

    video = tmp_path / "stream.mp4"
    loud = "if(between(t,40,46)+between(t,90,95),8,0)"
    subprocess.run(
        [ffmpeg_exe(), "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25:duration=120",
         "-f", "lavfi", "-i", "anoisesrc=d=120:c=pink:a=0.05",
         "-f", "lavfi", "-i", "sine=f=500:d=120",
         "-filter_complex", f"[2]volume='{loud}':eval=frame[s];[1][s]amix=inputs=2:normalize=0[a]",
         "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(video)],
        check=True,
    )
    steps = []
    result = make_highlights(str(video), settings, minutes=1, progress=lambda m, f: steps.append(m),
                             output_root=tmp_path / "Fertig")
    assert len(result.clips) == 2 and result.video.exists()
    assert result.folder.parent == tmp_path / "Fertig"
    text = result.description.read_text(encoding="utf-8")
    assert "Kapitel:" in text and "Highlight 2" in text
    assert any("Schneide Clip" in s for s in steps)


def test_make_highlights_missing_file(settings):
    with pytest.raises(FileNotFoundError):
        make_highlights("C:/gibt/es/nicht.mp4", settings)


def test_make_highlights_from_link_uses_sections(tmp_path, settings, registry, monkeypatch):
    """Simuliert Twitch: erst Tonspur, dann nur die Highlight-Abschnitte."""
    import subprocess

    full = tmp_path / "remote.mp4"
    loud = "if(between(t,60,66),8,0)"
    subprocess.run(
        [ffmpeg_exe(), "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25:duration=150",
         "-f", "lavfi", "-i", "anoisesrc=d=150:c=pink:a=0.05",
         "-f", "lavfi", "-i", "sine=f=500:d=150",
         "-filter_complex", f"[2]volume='{loud}':eval=frame[s];[1][s]amix=inputs=2:normalize=0[a]",
         "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(full)],
        check=True,
    )
    info = {"extractor_key": "TwitchVod", "uploader": "Neuer Streamer", "uploader_id": "neuer",
            "timestamp": 1760292900, "webpage_url": "https://www.twitch.tv/videos/42", "title": "Abend", "duration": 150}
    sections = []

    def fake_sections(url, ranges, dest, progress=None):
        out = []
        for i, (s, e) in enumerate(ranges):
            p = dest / f"abschnitt_{i}.mp4"
            subprocess.run([ffmpeg_exe(), "-y", "-loglevel", "error", "-ss", str(s), "-i", str(full),
                            "-t", str(e - s), "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(p)], check=True)
            out.append({"start": s, "end": e, "path": str(p)})
        sections.extend(ranges)
        return out

    def fake_audio(url, dest, progress=None):
        dest.mkdir(parents=True, exist_ok=True)
        p = dest / "ton.m4a"
        subprocess.run([ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(full), "-vn", "-c:a", "aac", str(p)], check=True)
        return p

    monkeypatch.setattr(fetch, "fetch_info", lambda url: info)
    monkeypatch.setattr(fetch, "download_audio", fake_audio)
    monkeypatch.setattr(fetch, "download_sections", fake_sections)
    monkeypatch.setattr(fetch, "download_full", lambda *a, **k: pytest.fail("komplettes VOD nicht nötig"))

    result = make_highlights("https://www.twitch.tv/videos/42", settings, minutes=1, output_root=tmp_path / "Fertig")
    assert len(result.clips) == 1 and sections and sections[0][1] - sections[0][0] < 150
    text = result.description.read_text(encoding="utf-8")
    assert "Creator: Neuer Streamer" in text and "https://www.twitch.tv/videos/42?t=" in text
    # neuer Creator automatisch im Register, Upload-Freigabe noch offen
    from publisher.creators import CreatorRegistry

    reg = CreatorRegistry.load(settings.creators_file)
    assert reg.get("neuer-streamer").permission_status == "angefragt"
