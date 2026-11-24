"""Branding intro: appended to the END of every Short and the START of every episode.
Each variant is rendered once and cached in data/intro_cache (re-made when the intro file or the format changes)."""
import hashlib
import json
from pathlib import Path

from .core import ROOT, log, probe, run, video_encoder_args

CACHE = ROOT / "data" / "intro_cache"


def source(cfg):
    """The configured intro video (path relative to the project or absolute), or None when disabled/missing."""
    p = (cfg.get("branding") or {}).get("intro")
    if not p:
        return None
    f = Path(p) if Path(p).is_absolute() else ROOT / p
    if not f.is_file():
        log.warning("vinheta configurada não encontrada: %s", f)
        return None
    return f


def _cached(src, key, build):
    st = src.stat()
    h = hashlib.sha1(json.dumps([str(src), st.st_size, st.st_mtime, key], sort_keys=True).encode()).hexdigest()[:12]
    out = CACHE / f"{key['kind']}_{h}.mp4"
    if not out.exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp.mp4")
        build(tmp)
        tmp.replace(out)
    return out


def _inputs(src):
    """ffmpeg inputs for the intro; a silent track is synthesized if the intro has no audio (joins need one)."""
    p = probe(src)
    if any(s["codec_type"] == "audio" for s in p["streams"]):
        return ["-i", src], "[0:a]"
    return ["-i", src, "-f", "lavfi", "-t", p["format"]["duration"], "-i", "anullsrc=r=48000:cl=stereo"], "[1:a]"


def short_variant(cfg):
    """1080x1920 at the Shorts fps: center crop to 9:16 (intros are usually centered logos), audio at -14 LUFS."""
    src = source(cfg)
    if not src:
        return None
    fps = cfg["shorts"]["fps"]
    ins, a = _inputs(src)
    return _cached(src, {"kind": "short", "fps": fps, "v": 1}, lambda tmp: run(
        ["ffmpeg", "-y", "-v", "error", *ins, "-filter_complex",
         f"[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps={fps},format=yuv420p[v];"
         f"{a}loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,aformat=channel_layouts=stereo[a]",
         "-map", "[v]", "-map", "[a]", *video_encoder_args(), "-c:a", "aac", "-b:a", "192k", "-ar", "48000", tmp]))


def long_variant(cfg, ref):
    """Same video format as the episode file `ref` (size, fps, H.264 profile/level AND MP4 track timescale) so the
    episode can be joined by stream copy — re-encoding hours of video for a 6 s intro would be absurd. A timescale
    mismatch silently breaks the join (durations explode), so it is copied from `ref` too."""
    src = source(cfg)
    p = probe(ref) if src else None
    if not p:
        return None
    v = next(s for s in p["streams"] if s["codec_type"] == "video")
    if v.get("codec_name") != "h264":
        return None
    fps = v["r_frame_rate"]
    prof = {"High": "high", "Main": "main", "Constrained Baseline": "constrained_baseline"}.get(v.get("profile"), "high")
    level = f"{v['level'] / 10:.1f}" if v.get("level") else "auto"
    ts = int(str(v.get("time_base", "1/1000")).split("/")[1])
    key = {"kind": "long", "w": v["width"], "h": v["height"], "fps": fps, "pix": v["pix_fmt"], "prof": prof, "lvl": level, "ts": ts, "v": 2}

    def build(tmp):
        ins, a = _inputs(src)
        args = ["ffmpeg", "-y", "-v", "error", *ins, "-filter_complex",
                f"[0:v]scale={v['width']}:{v['height']}:force_original_aspect_ratio=decrease,"
                f"pad={v['width']}:{v['height']}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps={fps},format={v['pix_fmt']}[v];"
                f"{a}loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000,aformat=channel_layouts=stereo[a]",
                "-map", "[v]", "-map", "[a]"]
        enc = video_encoder_args()
        if "h264_amf" in enc:
            enc += ["-profile:v", prof, "-level", level]
        else:
            enc += ["-profile:v", prof] + ([] if level == "auto" else ["-level:v", level])
        run(args + enc + ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-video_track_timescale", str(ts), tmp])
    return _cached(src, key, build)


def append_to_short(clip, intro, out, loudness):
    """clip + intro → out: one re-encode (frame-exact join), whole soundtrack normalized."""
    run(["ffmpeg", "-y", "-v", "error", "-i", clip, "-i", intro, "-filter_complex",
         "[0:v]setsar=1,format=yuv420p[v0];[1:v]setsar=1,format=yuv420p[v1];"
         "[0:a]aresample=48000,aformat=channel_layouts=stereo[a0];[1:a]aresample=48000,aformat=channel_layouts=stereo[a1];"
         f"[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a];[a]loudnorm=I={loudness}:TP=-1.5:LRA=11,aresample=48000[an]",
         "-map", "[v]", "-map", "[an]", *video_encoder_args(), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
         "-ar", "48000", "-movflags", "+faststart", out])


def compatible(a, b):
    """Can two files be joined by stream copy? Same codec/profile/size/fps/pixel format (video) and audio format."""
    pa, pb = probe(a), probe(b)
    if not pa or not pb:
        return False
    keys = {"video": ("codec_name", "profile", "width", "height", "pix_fmt", "r_frame_rate"),
            "audio": ("codec_name", "sample_rate", "channels")}
    for kind, ks in keys.items():
        sa = next((s for s in pa["streams"] if s["codec_type"] == kind), None)
        sb = next((s for s in pb["streams"] if s["codec_type"] == kind), None)
        if not sa or not sb or any(sa.get(k) != sb.get(k) for k in ks):
            return False
    return True


def prepend_to_episode(intro, episode, out, wd):
    """intro + episode → out by stream copy, then decode the join to make sure players won't glitch there."""
    lst = wd / "intro.ffconcat"
    q = lambda p: str(Path(p).resolve()).replace("\\", "/").replace("'", "'\\''")  # noqa: E731
    lst.write_text(f"ffconcat version 1.0\nfile '{q(intro)}'\nfile '{q(episode)}'\n", encoding="utf-8")
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst.name, "-map", "0:v:0", "-map", "0:a:0",
         "-c", "copy", "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", Path(out).name], 3600, wd)
    intro_s = float(probe(intro)["format"]["duration"])
    r = __import__("subprocess").run(["ffmpeg", "-v", "error", "-ss", f"{max(0, intro_s - 3):.2f}", "-t", "8", "-i", str(out),
                                      "-f", "null", "-"], capture_output=True, text=True, timeout=120)
    if r.returncode or r.stderr.strip():
        raise RuntimeError(f"emenda da vinheta com erros de decodificação: {r.stderr[-300:]}")
    return intro_s
