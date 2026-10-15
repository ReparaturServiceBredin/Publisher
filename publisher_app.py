"""Einstiegspunkt für die Windows-.exe (PyInstaller).

`Publisher.exe --selftest ergebnis.txt` prüft ohne Fenster, ob ffmpeg, Erkennung, Schnitt und
yt-dlp in der fertigen .exe funktionieren (wird beim Bauen auf GitHub ausgeführt).
"""

import multiprocessing
import sys


def selftest(out_file: str) -> int:
    import os
    import subprocess
    import tempfile
    import traceback
    from pathlib import Path

    try:
        home = Path(tempfile.mkdtemp(prefix="publisher_selftest_"))
        os.environ["PUBLISHER_HOME"] = str(home)
        from publisher.auto import make_highlights
        from publisher.config import load_app_settings
        from publisher.ffmpeg_bin import ffmpeg_exe, popen_kwargs

        video = home / "test.mp4"
        loud = "if(between(t,40,46)+between(t,90,95),8,0)"
        subprocess.run(
            [ffmpeg_exe(), "-y", "-loglevel", "error",
             "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=120",
             "-f", "lavfi", "-i", "anoisesrc=d=120:c=pink:a=0.05",
             "-f", "lavfi", "-i", "sine=f=500:d=120",
             "-filter_complex", f"[2]volume='{loud}':eval=frame[s];[1][s]amix=inputs=2:normalize=0[a]",
             "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
             "-shortest", str(video)],
            check=True, **popen_kwargs(),
        )
        result = make_highlights(str(video), load_app_settings(), minutes=1)

        # Credit-Einblendung (drawtext + Systemschrift) mit dem mitgelieferten ffmpeg prüfen
        from publisher import render
        from publisher.export import _font_file

        work = home / "overlay"
        work.mkdir()
        (work / "overlay_01.txt").write_text("Clip von Test – twitch.tv/test", encoding="utf-8")
        vf = render.video_filter(load_app_settings(), False, "overlay_01.txt", _font_file(work))
        render.run(render.cut_command(str(video), 40, 2, "clip.mp4", vf, True), cwd=work)
        import yt_dlp
        from yt_dlp.extractor import gen_extractor_classes

        names = {e.IE_NAME for e in gen_extractor_classes()}

        # neue Oberfläche + Upload-Module müssen in der .exe stecken
        import customtkinter

        customtkinter.set_default_color_theme("blue")  # lädt die mitgelieferten Theme-Dateien
        import googleapiclient.discovery  # noqa: F401
        from PIL import Image  # noqa: F401

        import publisher.gui  # noqa: F401
        from publisher import tiktok, videobox, youtube  # noqa: F401

        assert tiktok.chunk_plan(50 * 1024 * 1024)[1] == 5

        # Lernmodul: Referenzvideo analysieren + Drag-&-Drop-Bibliothek muss mitgeliefert sein
        from publisher import learn

        feats = learn.analyze_video(video)
        assert feats["duration_s"] > 100 and "cuts_per_min" in feats
        import tkinterdnd2

        assert (Path(tkinterdnd2.__file__).parent / "tkdnd").is_dir(), "tkdnd fehlt"
        import publisher.gui_refs  # noqa: F401
        assert "twitch:vod" in names and "youtube" in names, "yt-dlp Extraktoren fehlen"
        msg = f"OK clips={len(result.clips)} video={result.video.stat().st_size} yt-dlp={yt_dlp.version.__version__}"
        ok = len(result.clips) == 2 and len(result.vertical) == 2
    except Exception:
        msg, ok = "FEHLER\n" + traceback.format_exc(), False
    Path(out_file).write_text(msg, encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    multiprocessing.freeze_support()
    if len(sys.argv) >= 3 and sys.argv[1] == "--selftest":
        sys.exit(selftest(sys.argv[2]))
    from publisher.gui import main

    main()
