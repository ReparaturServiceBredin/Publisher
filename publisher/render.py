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
