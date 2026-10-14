"""ffmpeg-Aufrufe: Clips schneiden, Credit einblenden, zusammenfügen.

Kommt ohne ffprobe aus (nur ffmpeg), damit die Windows-.exe ein einziges Binary mitbringt.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .config import Settings
from .ffmpeg_bin import ffmpeg_exe, popen_kwargs


class RenderError(RuntimeError):
    pass


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_RE = re.compile(r"Stream #\S+.*?: Video: .*?(\d{2,5})x(\d{2,5})")


def probe(path: str | Path) -> dict:
    """Liest Dauer, Auflösung und Audio-Spur aus der Ausgabe von `ffmpeg -i`."""
    if not Path(path).exists():
        raise RenderError(f"Datei nicht gefunden: {path}")
    out = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        **popen_kwargs(),
    )
    text = out.stderr
    m = _DURATION_RE.search(text)
    if not m:
        raise RenderError(f"ffmpeg konnte {path} nicht lesen:\n{text.strip()[-500:]}")
    h, mnt, s = m.groups()
    info = {"duration": int(h) * 3600 + int(mnt) * 60 + float(s), "has_audio": " Audio: " in text}
    v = _VIDEO_RE.search(text)
    info["width"], info["height"] = (int(v.group(1)), int(v.group(2))) if v else (None, None)
    return info


def probe_duration(path: str | Path) -> float:
    return probe(path)["duration"]


def has_audio(path: str | Path) -> bool:
    return probe(path)["has_audio"]


def _fmt(seconds: float) -> str:
    return f"{seconds:.3f}"


def video_filter(settings: Settings, is_short: bool, overlay_file: str | None, font_file: str | None = None) -> str:
    """Normalisiert Auflösung/FPS (damit concat ohne Neukodierung klappt) und blendet den Credit ein."""
    ov = settings["overlay"]
    if is_short:
        v = settings["short_video"]
        chain = [
            "crop='min(iw,ih*9/16)':ih",
            f"scale={v['width']}:{v['height']}",
        ]
    else:
        v = settings["video"]
        chain = [
            f"scale={v['width']}:{v['height']}:force_original_aspect_ratio=decrease",
            f"pad={v['width']}:{v['height']}:(ow-iw)/2:(oh-ih)/2",
        ]
    chain += ["setsar=1", f"fps={v['fps']}"]
    if overlay_file:
        # Dateinamen ohne Sonderzeichen; ffmpeg läuft im Export-Ordner (relative Pfade, kein Escaping nötig).
        font = f"fontfile={font_file}" if font_file else f"font='{ov['font']}'"
        text = (
            f"drawtext=textfile={overlay_file}:{font}:fontsize={ov['fontsize']}"
            ":fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=14"
        )
        if is_short:
            text += ":x=(w-tw)/2:y=h*0.12"
            if not ov.get("short_full_duration", True):
                text += f":enable='between(t,0,{ov['duration']})'"
        else:
            text += f":x=40:y=h-th-60:enable='between(t,0,{ov['duration']})'"
        chain.append(text)
    return ",".join(chain)


def cut_command(
    vod_path: str,
    start: float,
    duration: float,
    output: str,
    vf: str,
    with_audio: bool,
) -> list[str]:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", _fmt(start), "-i", str(vod_path)]
    if not with_audio:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    cmd += ["-t", _fmt(duration), "-vf", vf, "-map", "0:v:0", "-map", "0:a:0" if with_audio else "1:a:0"]
    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart", output,
    ]
    return cmd


def concat_command(list_file: str, output: str) -> list[str]:
    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", list_file, "-c", "copy", "-movflags", "+faststart", output,
    ]


def run(cmd: list[str], cwd: Path) -> None:
    if cmd[0] == "ffmpeg":
        cmd = [ffmpeg_exe()] + cmd[1:]
    result = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", **popen_kwargs()
    )
    if result.returncode != 0:
        raise RenderError(f"ffmpeg fehlgeschlagen:\n{' '.join(cmd)}\n{result.stderr.strip()}")


# ---------------------------------------------------------------- Übergänge, Splitscreen, Hochformat

ENCODE = [
    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
    "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart",
]


def transition_filter(durations: list[float], fade: float, fps: int, edge_fade: float = 0.4) -> str:
    """Weiche Überblendung (Bild + Ton) zwischen allen Clips, dazu Ein-/Ausblenden am Anfang/Ende."""
    n = len(durations)
    lines = [f"[{i}:v]settb=AVTB,fps={fps},format=yuv420p[v{i}in]" for i in range(n)]
    lines += [f"[{i}:a]aresample=48000,asettb=AVTB[a{i}in]" for i in range(n)]
    v_prev, a_prev = "v0in", "a0in"
    elapsed = durations[0]
    for i in range(1, n):
        offset = elapsed - fade
        lines.append(f"[{v_prev}][v{i}in]xfade=transition=fade:duration={fade:.3f}:offset={offset:.3f}[vx{i}]")
        lines.append(f"[{a_prev}][a{i}in]acrossfade=d={fade:.3f}:c1=tri:c2=tri[ax{i}]")
        v_prev, a_prev = f"vx{i}", f"ax{i}"
        elapsed = offset + durations[i]
    out_start = max(0.0, elapsed - edge_fade)
    lines.append(
        f"[{v_prev}]fade=t=in:st=0:d={edge_fade},fade=t=out:st={out_start:.3f}:d={edge_fade}[vout]"
    )
    lines.append(
        f"[{a_prev}]afade=t=in:st=0:d={edge_fade},afade=t=out:st={out_start:.3f}:d={edge_fade}[aout]"
    )
    return ";\n".join(lines)


def transition_command(clip_files: list[str], script_file: str, output: str) -> list[str]:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for f in clip_files:
        cmd += ["-i", f]
    return cmd + ["-filter_complex_script", script_file, "-map", "[vout]", "-map", "[aout]", *ENCODE, output]


def split_filter(
    n: int, settings: Settings, duration: float, labels: list[str | None],
    overlay_file: str | None, font_file: str | None, primary_audio: bool,
) -> str:
    """2 Streamer nebeneinander, 3–4 im 2×2-Raster. Ton vom Haupt-POV (die anderen sind im selben Call)."""
    v = settings["video"]
    w, h, fps = v["width"], v["height"], v["fps"]
    ov = settings["overlay"]
    font = f"fontfile={font_file}" if font_file else f"font='{ov['font']}'"
    lines = []
    if n == 2:
        tw, th = w // 2, h
    else:
        tw, th = w // 2, h // 2
    for i in range(n):
        lines.append(
            f"[{i}:v]scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},setsar=1,fps={fps}[t{i}]"
        )
    tiles = [f"[t{i}]" for i in range(n)]
    if n == 2:
        lines.append(f"{''.join(tiles)}hstack=inputs=2[grid]")
    else:
        if n == 3:
            lines.append(f"color=c=black:s={tw}x{th}:r={fps}:d={duration:.3f}[t3]")
            tiles.append("[t3]")
        lines.append(f"{''.join(tiles)}xstack=inputs=4:layout=0_0|w0_0|0_h0|w0_h0[grid]")
    last = "grid"
    small = max(14, int(ov["fontsize"] * 0.7))
    positions = ["10:10", f"{tw}+10:10", f"10:{th}+10", f"{tw}+10:{th}+10"]
    for i, label in enumerate(labels[:n]):
        if not label:
            continue
        x, y = positions[i].split(":")
        lines.append(
            f"[{last}]drawtext=textfile={label}:{font}:fontsize={small}:fontcolor=white:"
            f"box=1:boxcolor=black@0.55:boxborderw=8:x={x}:y={y}[l{i}]"
        )
        last = f"l{i}"
    if overlay_file:
        lines.append(
            f"[{last}]drawtext=textfile={overlay_file}:{font}:fontsize={ov['fontsize']}:fontcolor=white:"
            f"box=1:boxcolor=black@0.6:boxborderw=14:x=(w-tw)/2:y=h-th-40:enable='between(t,0,{ov['duration']})'[ov]"
        )
        last = "ov"
    lines.append(f"[{last}]format=yuv420p[vout]")
    lines.append("[0:a]aresample=48000[aout]" if primary_audio else f"anullsrc=r=48000:cl=stereo,atrim=0:{duration:.3f}[aout]")
    return ";\n".join(lines)


def split_command(inputs: list[tuple[str, float]], duration: float, script_file: str, output: str) -> list[str]:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for path, start in inputs:
        cmd += ["-ss", _fmt(start), "-t", _fmt(duration), "-i", str(path)]
    return cmd + [
        "-filter_complex_script", script_file, "-map", "[vout]", "-map", "[aout]",
        "-t", _fmt(duration), *ENCODE, output,
    ]


def vertical_command(src: str, output: str, settings: Settings) -> list[str]:
    """9:16 für TikTok/Shorts: Video mittig in voller Breite, darüber/darunter unscharfer Hintergrund."""
    v = settings["short_video"]
    w, h = v["width"], v["height"]
    graph = (
        f"[0:v]split=2[bgsrc][fgsrc];"
        f"[bgsrc]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=24:2,eq=brightness=-0.08[bg];"
        f"[fgsrc]scale={w}:-2[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p[vout]"
    )
    return [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", src,
        "-filter_complex", graph, "-map", "[vout]", "-map", "0:a?", *ENCODE, output,
    ]
