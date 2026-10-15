import subprocess

import pytest

from publisher import learn
from publisher.ffmpeg_bin import ffmpeg_exe

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def _font():
    from pathlib import Path

    return f"fontfile={FONT}:" if Path(FONT).exists() else ""


def make_video(path, *, cuts_every=None, subs=False, music=False, speech=False, dur=12, vertical=False, cta=False):
    w, h = (360, 640) if vertical else (640, 360)
    if cuts_every:
        n = int(dur / cuts_every)
        colors = ["red", "blue", "green", "orange", "purple", "yellow"]
        inputs, labels = [], ""
        for i in range(n):
            inputs += ["-f", "lavfi", "-i", f"color=c={colors[i % len(colors)]}:s={w}x{h}:d={cuts_every}:r=25"]
            labels += f"[{i}:v]"
        vf = f"{labels}concat=n={n}:v=1[v0]"
    else:
        inputs = ["-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:d={dur}:r=25"]
        vf = "[0:v]null[v0]"
        n = 1
    chain = "[v0]"
    extra = []
    if subs:
        extra.append(f"drawtext={_font()}text='Hier steht ein Untertitel':fontsize=22:fontcolor=white:borderw=3:"
                     f"bordercolor=black:x=(w-tw)/2:y=h-60")
    if cta:
        extra.append(f"drawtext={_font()}text='JETZT FOLGEN':fontsize=36:fontcolor=white:box=1:boxcolor=black:"
                     f"x=(w-tw)/2:y=h/3:enable='gt(t,{dur - 2.5})'")
    vf += f";{chain}{','.join(extra) if extra else 'null'}[v]"
    a_idx = n
    audio = []
    if speech:
        audio += ["-f", "lavfi", "-i", f"anoisesrc=d={dur}:c=pink:a=0.5,bandpass=f=900:width_type=h:w=1400,"
                  f"volume='(0.5+0.5*sin(2*PI*4*t))*if(lt(mod(t,3),2.3),1,0.02)':eval=frame"]
    if music:
        audio += ["-f", "lavfi", "-i", f"sine=f=220:d={dur}", "-f", "lavfi", "-i", f"sine=f=330:d={dur}"]
    if not audio:
        audio = ["-f", "lavfi", "-i", f"anullsrc=r=16000:cl=mono:d={dur}"]
    n_a = len(audio) // 4
    af = "".join(f"[{a_idx + i}:a]" for i in range(n_a)) + f"amix=inputs={n_a}:normalize=0[a]" if n_a > 1 else f"[{a_idx}:a]anull[a]"
    subprocess.run([ffmpeg_exe(), "-y", "-loglevel", "error", *inputs, *audio, "-filter_complex", f"{vf};{af}",
                    "-map", "[v]", "-map", "[a]", "-t", str(dur), "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", str(path)], check=True)
    return path


def test_analysis_detects_cuts_text_audio(tmp_path):
    a = learn.analyze_video(make_video(tmp_path / "a.mp4", cuts_every=1.5, subs=True, music=True, vertical=True, cta=True))
    assert 6 <= a["cuts"] <= 8 and 30 <= a["cuts_per_min"] <= 40
    assert a["hook_s"] <= 1.5 and a["has_text_hook"] and a["has_subtitles"] and a["has_cta"]
    assert a["has_music"] and a["vertical"]
    b = learn.analyze_video(make_video(tmp_path / "b.mp4", speech=True))
    assert b["cuts"] == 0 and b["hook_s"] == 3.0 and not b["has_subtitles"] and not b["has_music"]
    assert b["speech_pct"] >= 70 and not b["vertical"]


def test_unsupported_files(tmp_path):
    bad = tmp_path / "kaputt.mp4"
    bad.write_text("kein video")
    with pytest.raises(learn.UnsupportedVideo, match="kein lesbares Video"):
        learn.analyze_video(bad)
    doc = tmp_path / "notizen.docx"
    doc.write_text("x")
    lib = learn.Library(tmp_path / "lib")
    with pytest.raises(learn.UnsupportedVideo, match="nicht unterstützt"):
        lib.add(doc)
    with pytest.raises(learn.UnsupportedVideo):
        lib.add(bad)
    assert lib.videos == []


def _fake(lib, vid, feats, rating="", platform="TikTok", metrics=None, excluded=False):
    v = learn.RefVideo(id=vid, title=vid, file=f"videos/{vid}.mp4", original_name=f"{vid}.mp4", added_at="x",
                       platform=platform, rating=rating, metrics=metrics or {}, excluded=excluded,
                       analysis={**{f.key: 0 for f in learn.FEATURES}, **feats})
    lib.videos.append(v)
    return v


def test_profile_from_ratings_and_recommendations(tmp_path):
    lib = learn.Library(tmp_path)
    for i in range(3):
        _fake(lib, f"g{i}", {"hook_s": 1.0 + 0.2 * i, "cuts_per_min": 30 + i, "has_subtitles": True}, "gut")
        _fake(lib, f"w{i}", {"hook_s": 3.0 - 0.1 * i, "cuts_per_min": 8 + i, "has_subtitles": False}, "schlecht")
    _fake(lib, "aus", {"hook_s": 9.0}, "gut", excluded=True)  # ausgeschlossen → zählt nicht
    lib.save()
    profile = learn.build_profile(learn.Library(tmp_path))
    assert profile.ready and profile.n_good == 3 and profile.n_weak == 3 and profile.n_total == 6
    keys = [i.feature.key for i in profile.key_items(10)]
    assert {"hook_s", "cuts_per_min", "has_subtitles"} <= set(keys)
    hook = next(i for i in profile.items if i.feature.key == "hook_s")
    assert hook.direction < 0 and "Starte schneller" in hook.advice() and "1,2 s" in hook.advice()
    tips = learn.check_video({"hook_s": 2.9, "cuts_per_min": 31, "has_subtitles": False}, profile)
    assert any("Hook" in t for t in tips) and any("Untertitel" in t for t in tips)
    assert not any("Schnittfrequenz" in t for t in tips)  # liegt schon bei den guten
    opts = learn.suggested_options(profile)
    assert opts["pre_roll"] == 10.0 and opts["max_clip"] == 40.0


def test_profile_from_metrics_within_platform(tmp_path):
    lib = learn.Library(tmp_path)
    for i, (views, ret) in enumerate([(100000, 70), (80000, 65), (60000, 60), (2000, 20), (1500, 15), (1000, 10)]):
        _fake(lib, f"v{i}", {"cuts_per_min": 40 - 5 * i}, metrics={"views": views, "retention_pct": ret})
    scores = learn.success_scores(lib.learning_set())
    assert scores["v0"] > scores["v5"]
    profile = learn.build_profile(lib)
    cut = next(i for i in profile.items if i.feature.key == "cuts_per_min")
    assert cut.direction > 0 and cut.strength == "deutlich"


def test_not_enough_data_message(tmp_path):
    lib = learn.Library(tmp_path)
    _fake(lib, "a", {}, "gut")
    _fake(lib, "b", {}, "schlecht")
    profile = learn.build_profile(lib)
    assert not profile.ready and "zu wenig" in profile.message


def test_library_persistence_and_management(tmp_path):
    src = make_video(tmp_path / "clip.mp4", cuts_every=2, dur=6)
    lib = learn.Library(tmp_path / "lib")
    v = lib.add(src)
    lib.analyze(v)
    lib.update(v, title="Mein bester Clip", platform="YouTube", rating="gut",
               metrics={"views": 5000}, overrides={"has_cta": True}, excluded=True)
    again = learn.Library(tmp_path / "lib")
    w = again.get(v.id)
    assert w.title == "Mein bester Clip" and w.analysis["cuts"] >= 2 and w.features()["has_cta"] is True
    assert w.excluded and again.learning_set() == []
    with pytest.raises(ValueError):
        again.update(w, analysis={})
    again.remove(w)
    assert not (tmp_path / "lib" / w.file).exists() and learn.Library(tmp_path / "lib").videos == []


def test_parse_number_formats():
    pytest.importorskip("customtkinter")
    from publisher.gui_refs import parse_number

    assert parse_number("12.345") == 12345
    assert parse_number("12,5") == 12.5
    assert parse_number("1,2k") == 1200
    assert parse_number("3 Mio") == 3_000_000
    assert parse_number("72%") == 72
    assert parse_number("") is None
    with pytest.raises(ValueError):
        parse_number("viele")
