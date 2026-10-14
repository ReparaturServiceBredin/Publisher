"""ffmpeg-Aufrufe: Clips schneiden, Credit einblenden, zusammenfügen."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .config import Settings


class RenderError(RuntimeError):
    pass


def _require(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise RenderError(f"{tool} wurde nicht gefunden – bitte ffmpeg installieren.")
    return path


def probe(path: str | Path) -> dict:
    out = subprocess.run(
        [_require("ffprobe"), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise RenderError(f"ffprobe konnte {path} nicht lesen: {out.stderr.strip()}")
    return json.loads(out.stdout)


def probe_duration(path: str | Path) -> float:
    return float(probe(path)["format"]["duration"])


def has_audio(path: str | Path) -> bool:
    return any(s.get("codec_type") == "audio" for s in probe(path).get("streams", []))


def _fmt(seconds: float) -> str:
    return f"{seconds:.3f}"


def video_filter(settings: Settings, is_short: bool, overlay_file: str | None) -> str:
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
        # Dateiname ohne Sonderzeichen; ffmpeg läuft im Export-Ordner (siehe cut_command).
        text = (
            f"drawtext=textfile={overlay_file}:font='{ov['font']}':fontsize={ov['fontsize']}"
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
    cmd = [_require(cmd[0])] + cmd[1:]
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RenderError(f"ffmpeg fehlgeschlagen:\n{' '.join(cmd)}\n{result.stderr.strip()}")
