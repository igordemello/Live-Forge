"""Shared plumbing: config, logging, ffmpeg/ffprobe helpers."""
import datetime
import json
import logging
import logging.handlers
import os
import re
import subprocess
import wave
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parent.parent
LOW_PRIORITY = subprocess.BELOW_NORMAL_PRIORITY_CLASS if os.name == "nt" else 0
log = logging.getLogger("liveforge")


def load_config(path=None) -> dict:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            k, sep, v = line.partition("=")
            if sep and not k.strip().startswith("#") and v.strip():
                os.environ.setdefault(k.strip(), v.strip().strip('"'))
    with open(path or ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def setup_logging():
    (ROOT / "logs").mkdir(exist_ok=True)
    import sys
    for s in (sys.stdout, sys.stderr):  # Windows consoles (cp1252/cp850) choke on characters like "→"
        try:
            s.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(), logging.handlers.RotatingFileHandler(
            ROOT / "logs" / "liveforge.log", maxBytes=10_000_000, backupCount=5, encoding="utf-8")],  # 24 h lives
    )
    # obsws_python logs the OBS password at INFO and dumps tracebacks on refused connections: keep it silent.
    for name, level in (("obsws_python", logging.CRITICAL + 1), ("httpx", logging.WARNING),
                        ("faster_whisper", logging.WARNING), ("huggingface_hub", logging.ERROR)):
        logging.getLogger(name).setLevel(level)


def run(args, timeout=900, cwd=None) -> str:
    """Run a media tool at below-normal priority; raise RuntimeError with stderr tail on failure."""
    r = subprocess.run([str(a) for a in args], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, cwd=cwd, creationflags=LOW_PRIORITY)
    if r.returncode:
        raise RuntimeError(f"{Path(str(args[0])).name} falhou ({r.returncode}): {r.stderr[-1200:]}")
    return r.stdout


def probe(path) -> dict | None:
    try:
        return json.loads(run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", path], 60))
    except (RuntimeError, ValueError, subprocess.TimeoutExpired):
        return None


def duration_ms(path) -> int | None:
    p = probe(path)
    try:
        return int(float(p["format"]["duration"]) * 1000)
    except (TypeError, KeyError, ValueError):
        return None


def game_name(cfg) -> str:
    """Manual name from the panel, or (session.game = auto) the game the OBS capture is pointed at."""
    s = cfg["session"]
    return (s.get("current_game") or "") if (s.get("game") or "auto") == "auto" else s["game"]


def hashtags(cfg) -> list:
    """Session hashtags with the current game's hashtag first (#sonicpicopark)."""
    g = re.sub(r"[^0-9a-zà-ÿ]", "", game_name(cfg).lower())
    return ([f"#{g}"] if g else []) + [h for h in cfg["session"]["hashtags"] if h.lower() != f"#{g}"]


def whisper_prompt(cfg) -> str:
    """Primes Whisper with the game and the user's vocabulary (friends' names, game terms) so it spells them right."""
    vocab = ", ".join(cfg["session"].get("vocabulary") or [])
    g = game_name(cfg)
    return f"Live de {g or 'games'} em português do Brasil." + (f" Nomes e termos: {vocab}." if vocab else "")


def slugify(text, n=40) -> str:
    import unicodedata
    ascii_text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text)[:n].strip("-")


def start_ms_from_name(path) -> int:
    """OBS names files by start time (local clock), e.g. '2026-11-16 21-04-33.mkv'."""
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})[ _T](\d{2})[-_.:](\d{2})[-_.:](\d{2})", Path(path).stem)
    if m:
        return int(datetime.datetime(*map(int, m.groups())).timestamp() * 1000)
    return int(os.path.getctime(path) * 1000)


def extract_wav(src, dst, stream=0):
    run(["ffmpeg", "-y", "-v", "error", "-i", src, "-map", f"0:a:{stream}", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", dst])


def read_wav(path) -> np.ndarray:
    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768


def motion_series(path, fps=4) -> np.ndarray:
    """Per-frame visual change (0..1): mean abs difference of consecutive 64x36 gray frames at `fps`."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-threads", "2", "-i", str(path), "-an", "-vf",
                        f"fps={fps},scale=64:36,format=gray", "-f", "rawvideo", "-"],
                       capture_output=True, timeout=900, creationflags=LOW_PRIORITY)
    if r.returncode:
        raise RuntimeError(f"ffmpeg (movimento) falhou: {r.stderr[-400:].decode(errors='replace')}")
    frames = np.frombuffer(r.stdout, np.uint8)
    frames = frames[: len(frames) // 2304 * 2304].reshape(-1, 2304).astype(np.float32)
    if len(frames) < 2:
        return np.zeros(0, np.float32)
    return np.abs(np.diff(frames, axis=0)).mean(axis=1) / 255


_amf = None


def video_encoder_args() -> list:
    """AMD hardware encoder when present (keeps the CPU free for the live), else x264."""
    global _amf
    if _amf is None:
        try:
            _amf = "h264_amf" in run(["ffmpeg", "-hide_banner", "-encoders"], 30)
        except RuntimeError:
            _amf = False
    if _amf:
        return ["-c:v", "h264_amf", "-quality", "balanced", "-rc", "cqp", "-qp_i", "18", "-qp_p", "18"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-threads", "4"]


def cut_clip(parts, out, fps=30, audio_stream=0, voice=None, add_voice=None):
    """parts = [(path, offset_s, dur_s), ...] in order; re-encodes to one continuous 1080p clip.
    voice = (stream_index, wav_path): also writes that audio track (e.g. mic only) as 16 kHz mono WAV, same timeline.
    add_voice = stream index mixed into the clip audio (when the mic isn't on the full-mix track)."""
    args = ["ffmpeg", "-y", "-v", "error"]
    for path, off, dur in parts:
        args += ["-ss", f"{off:.3f}", "-t", f"{dur:.3f}", "-i", path]
    n, fc = len(parts), []

    def base(i):  # the clip soundtrack of input i, with the voice track mixed in if asked
        if add_voice is None:
            return f"[{i}:a:{audio_stream}]"
        fc.append(f"[{i}:a:{audio_stream}][{i}:a:{add_voice}]amix=inputs=2:normalize=0:duration=longest[m{i}]")
        return f"[m{i}]"

    if n == 1:
        b = base(0)
        maps, vo = ["-map", "0:v:0", "-map", b if add_voice is not None else f"0:a:{audio_stream}"], voice and f"0:a:{voice[0]}"
    else:  # 15 ms audio fades at every join so trimmed pauses don't click
        for i, (_, _, d) in enumerate(parts):
            fc.append(f"{base(i)}afade=t=in:d=0.015,afade=t=out:st={max(0, d - 0.015):.3f}:d=0.015[a{i}]")
        fc.append("".join(f"[{i}:v:0][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[v][a]")
        if voice:
            fc.append("".join(f"[{i}:a:{voice[0]}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[vo]")
        maps, vo = ["-map", "[v]", "-map", "[a]"], voice and "[vo]"
    if fc:
        args += ["-filter_complex", ";".join(fc)]
    args += [*maps, "-r", str(fps), *video_encoder_args(), "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-ar", "48000", "-b:a", "192k", out]
    if voice:
        args += ["-map", vo, "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", voice[1]]
    run(args)
