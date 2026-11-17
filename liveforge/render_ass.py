"""FFmpeg-only fallback renderer for vertical shorts: render(plan, out_path, work_dir)."""
import functools
import json
import os
import shutil
import struct
import subprocess
import sys

W, H = 1080, 1920
HERE = os.path.dirname(os.path.abspath(__file__))
ANTON = os.path.join(HERE, "..", "assets", "fonts", "Anton-Regular.ttf")
IMPACT = r"C:\Windows\Fonts\impact.ttf"


@functools.lru_cache(maxsize=1)
def _has_amf() -> bool:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True)
    return "h264_amf" in r.stdout


def _ts(ms: int) -> str:
    cs = round(ms / 10)
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _bgr(hex_rgb: str) -> str:
    h = hex_rgb.lstrip("#")
    return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def _wrap(words: list[str], fs: int, max_w: int = 886):
    """Split into <=2 balanced lines; shrink font until the longest line fits (~0.55em/char)."""
    k = len(words)
    if k > 1 and sum(len(w) + 1 for w in words) * 0.55 * fs > max_w:
        best = min(range(1, k), key=lambda i: abs(len(" ".join(words[:i])) - len(" ".join(words[i:]))))
    else:
        best = k
    longest = max(len(" ".join(words[:best])), len(" ".join(words[best:])))
    fs = min(fs, int(max_w / (0.55 * max(longest, 1))))
    return best, fs


def _em_scale(font_path: str) -> float:
    """libass Fontsize is the win ascent+descent cell height; CSS px is the em. Returns cell/em."""
    d = open(font_path, "rb").read()
    tabs = {d[12 + 16 * i:16 + 16 * i]: struct.unpack(">I", d[20 + 16 * i:24 + 16 * i])[0]
            for i in range(struct.unpack(">H", d[4:6])[0])}
    upm = struct.unpack(">H", d[tabs[b"head"] + 18:tabs[b"head"] + 20])[0]
    asc, desc = struct.unpack(">HH", d[tabs[b"OS/2"] + 74:tabs[b"OS/2"] + 78])
    return (asc + desc) / upm


def _ass(plan: dict, family: str, k: float) -> str:
    cs, hk = plan["layout"]["caption"], plan["layout"]["hook"]
    fs0 = round(cs["size"] * k)
    cy = int(cs["y"] * H)
    out = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 0", "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,"
        "Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
        f"Style: Cap,{family},{fs0},{_bgr(cs['color'])},&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,8,3,5,90,90,0,1",
        f"Style: Hook,{family},{round(hk['size'] * k)},{_bgr(hk['color'])},&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,8,3,5,90,90,0,1",
        "", "[Events]", "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    for g in plan.get("captions", []):
        ws = g["words"]
        texts = [w["text"].upper() for w in ws]
        brk, fs = _wrap(texts, cs["size"])
        # segments: (start, end, active word index or None)
        segs, t = [], g["start_ms"]
        for i, w in enumerate(ws):
            a, b = max(w["start_ms"], t), min(w["end_ms"], g["end_ms"])
            if a > t:
                segs.append((t, a, None))
            if b > a:
                segs.append((a, b, i))
                t = b
        if t < g["end_ms"]:
            segs.append((t, g["end_ms"], None))
        for a, b, act in segs:
            parts = []
            for i, tx in enumerate(texts):
                if i == act:
                    tx = r"{\t(0,100,\fscx115\fscy115)}" + tx + r"{\fscx100\fscy100}"
                parts.append(("\\N" if i == brk else " " if i else "") + tx)
            out.append(f"Dialogue: 0,{_ts(a)},{_ts(b)},Cap,,0,0,0,,{{\\pos({W // 2},{cy})\\fs{round(fs * k)}}}{''.join(parts)}")
    if plan.get("hook_text"):
        out.append(
            f"Dialogue: 1,{_ts(0)},{_ts(1800)},Hook,,0,0,0,,{{\\pos({W // 2},{int(hk['y'] * H)})"
            r"\fscx60\fscy60\alpha&HFF&\t(0,160,\fscx100\fscy100\alpha&H00&)\t(1500,1800,\alpha&HFF&)}"
            + plan["hook_text"].upper()
        )
    return "\n".join(out) + "\n"


def _zoom_expr(zooms: list[dict]) -> str:
    terms = []
    for z in zooms:
        s, d, sc = z["start_ms"] / 1000, z["duration_ms"] / 1000, z["scale"] - 1
        u = f"((t-{s})/{d})"
        terms.append(
            f"if(between(t,{s},{s + d}),{sc}*if(lt({u},0.35),sin(PI/2*{u}/0.35),0.5*(1+cos(PI*({u}-0.35)/0.65))),0)"
        )
    return "1+" + "+".join(terms)


def _graph(plan: dict) -> str:
    fps = plan["canvas"]["fps"]
    lay = plan["layout"]
    bg = lay.get("background", {})
    blur, dim = bg.get("blur", 24), bg.get("dim", 0.35)
    # draw order: game, then cam on top
    panels = [(n, plan[b], lay[n]) for n, b in (("game", "game_box"), ("cam", "webcam_box"))
              if plan.get(b) and lay[n].get("visible", True)]
    f = [f"[0:v]fps={fps},split={len(panels) + 1}" + "".join(f"[s{i}]" for i in range(len(panels) + 1))]
    # cheap blur: cover-crop, shrink 10x, box blur, enlarge, darken (RGB scale ~ luma and chroma-around-128 scale)
    k = 1 - dim
    chain = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
    if blur:
        chain += f",scale=108:192,boxblur={max(1, round(blur / 8))}:2,scale={W}:{H}:flags=bilinear"
    f.append(f"[s0]{chain},lutyuv=y='val*{k}':u='128+(val-128)*{k}':v='128+(val-128)*{k}'[c0]")
    for i, (n, (x, y, w, h), r) in enumerate(panels, 1):
        s = (max if r["fit"] == "cover" else min)(r["w"] / w, r["h"] / h)
        pw, ph = round(w * s), round(h * s)
        crop = f",crop={r['w']}:{r['h']}" if r["fit"] == "cover" else ""
        ox, oy = r["x"] + (0 if crop else (r["w"] - pw) // 2), r["y"] + (0 if crop else (r["h"] - ph) // 2)
        if crop:
            pw, ph = max(pw, r["w"]), max(ph, r["h"])
        f.append(f"[s{i}]crop={w}:{h}:{x}:{y},scale={pw}:{ph}{crop}[p{i}]")
        f.append(f"[c{i - 1}][p{i}]overlay={ox}:{oy}[c{i}]")
    f.append(f"[c{len(panels)}]null[comp0]")
    zooms = [e for e in plan.get("effects", []) if e["type"] == "zoom"]
    last = "comp0"
    if zooms:
        ax = zooms[0]["anchor"]
        ex = _zoom_expr(zooms)
        # ponytail: one shared anchor (first zoom's) for all punch-ins, per-zoom anchors need chained scale/crop
        f.append(
            f"[comp0]scale=w='2*trunc({W // 2}*({ex}))':h='2*trunc({H // 2}*({ex}))':eval=frame,"
            f"crop={W}:{H}:'(iw-ow)*{ax[0]}':'(ih-oh)*{ax[1]}'[zoomed]"
        )
        last = "zoomed"
    f.append(f"[{last}]ass=cap.ass:fontsdir=fonts,format=yuv420p[v]")
    # audio
    fmt = "aformat=sample_rates=48000:channel_layouts=stereo"
    sfx = [e for e in plan.get("effects", []) if e["type"] == "sfx"]
    f.append(f"[0:a]{fmt}[a0]")
    for i, e in enumerate(sfx, 1):
        f.append(f"[{i}:a]{fmt},adelay={e['start_ms']}|{e['start_ms']},volume={e.get('gain_db', 0)}dB[a{i}]")
    ins = "".join(f"[a{i}]" for i in range(len(sfx) + 1))
    f.append(f"{ins}amix=inputs={len(sfx) + 1}:duration=first:normalize=0,alimiter=limit=0.89[aout]")
    return ";".join(f)


def render(plan: dict, out_path: str, work_dir: str) -> None:
    work = os.path.abspath(work_dir)
    os.makedirs(work, exist_ok=True)
    os.makedirs(os.path.join(work, "fonts"), exist_ok=True)
    font, family = (ANTON, "Anton") if os.path.exists(ANTON) else (IMPACT, "Impact")
    shutil.copy(font, os.path.join(work, "fonts", os.path.basename(font)))
    with open(os.path.join(work, "cap.ass"), "w", encoding="utf-8-sig") as fh:
        fh.write(_ass(plan, family, _em_scale(font)))
    sfx = [e for e in plan.get("effects", []) if e["type"] == "sfx"]
    dur = plan["duration_ms"] / 1000
    base = ["ffmpeg", "-hide_banner", "-y", "-i", os.path.abspath(plan["source"])]
    for e in sfx:
        base += ["-i", os.path.abspath(e["asset"])]
    base += ["-filter_complex", _graph(plan), "-map", "[v]", "-map", "[aout]", "-t", f"{dur:.3f}"]
    encoders = []
    if _has_amf():
        encoders.append(["-c:v", "h264_amf", "-quality", "balanced", "-rc", "vbr_peak", "-b:v", "10M", "-maxrate", "14M"])
    encoders.append(["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-threads", "4"])
    tail = ["-r", str(plan["canvas"]["fps"]), "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart", os.path.abspath(out_path)]
    err = ""
    for enc in encoders:
        r = subprocess.run(base + enc + tail, cwd=work, capture_output=True, text=True, timeout=1800,
                           creationflags=subprocess.BELOW_NORMAL_PRIORITY_CLASS)
        if r.returncode == 0:
            return
        err = r.stderr[-2000:]
    raise RuntimeError(f"ffmpeg failed:\n{err}")


if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf-8") as fh:
        render(json.load(fh), sys.argv[2], os.path.join(os.path.dirname(os.path.abspath(sys.argv[2])), "ass_work"))
