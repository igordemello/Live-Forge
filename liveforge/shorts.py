"""Agent 2 — Short editor: approved candidate -> cut -> edit plan -> HyperFrames (or FFmpeg/ASS) -> review folder."""
import datetime
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

import numpy as np

from .core import ROOT, cut_clip, log, probe, run, slugify
from .core import hashtags as all_hashtags
from .guard import kill_children

SFX = ROOT / "assets" / "sfx"


class Deferred(Exception):
    """Media or resources not ready; retry later without consuming an attempt."""


def media_pending(end_ms, grace_ms=180_000):
    """The last recording file closes up to ~closed_after_seconds after OBS stops: until then, wait instead of
    truncating a clip/episode that ends near the end of the recording."""
    return time.time() * 1000 - end_ms < grace_ms


def clean_word(t):
    return re.sub(r"[^\w!?À-ÿ'-]", "", t).upper()


def caption_groups(words, dur_ms, max_words=5, max_chars=22, gap_ms=450):
    """words = [(start_ms, end_ms, text)] clip-relative -> TikTok-style groups of 2-5 words."""
    groups, cur = [], []
    for s, e, t in words:
        t = clean_word(t)
        if not t or e <= 0 or s >= dur_ms:
            continue
        w = {"text": t, "start_ms": max(0, int(s)), "end_ms": min(dur_ms, int(e))}
        if cur and (len(cur) >= max_words or s - cur[-1]["end_ms"] > gap_ms
                    or len(" ".join(x["text"] for x in cur)) + 1 + len(t) > max_chars
                    or cur[-1]["text"][-1] in "!?"):
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    out = []
    for i, g in enumerate(groups):
        nxt = groups[i + 1][0]["start_ms"] if i + 1 < len(groups) else dur_ms
        start, end = g[0]["start_ms"], min(g[-1]["end_ms"] + 250, nxt, dur_ms)
        if end - start < 200 and out and start - out[-1]["end_ms"] <= 150 and len(out[-1]["words"]) + len(g) <= 7:
            prev = out[-1]  # too short to read (collapsed timings): join it to the previous line
            prev["words"] += g
            prev["text"] = " ".join(w["text"] for w in prev["words"])
            prev["end_ms"] = max(prev["end_ms"], end, prev["start_ms"] + 200)
            continue
        if end <= start:  # never emit a zero-length line: a renderer may show it until the end of the clip
            end = min(start + 300, dur_ms)
            if end <= start:
                continue
        out.append({"text": " ".join(w["text"] for w in g), "start_ms": start, "end_ms": end, "words": g})
    return out


def keep_intervals(start, end, speech, protect, motion, cfg):
    """Absolute [(a, b)] to keep. Drops only pauses >= trim_min_gap_ms with no speech, no protected moment
    (loudness peak, punch) and a picture stiller than half the clip's typical motion — never the comedic beat."""
    sh = cfg["shorts"]
    if not sh.get("trim_pauses") or not motion:
        return [(start, end)]
    pad = sh["trim_pad_ms"]
    busy = sorted([(a - pad, b + pad) for a, b in speech + protect] + [(start, start + 1000), (end - 1000, end)])
    merged = []
    for a, b in busy:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    still = 0.5 * float(np.median([v for _, v in motion])) + 0.002
    keep, cur = [], start
    for (_, gap_a), (gap_b, _) in zip(merged, merged[1:]):
        if gap_b - gap_a >= sh["trim_min_gap_ms"]:
            gm = [v for t, v in motion if gap_a <= t < gap_b]
            if gm and float(np.mean(gm)) <= still:
                keep.append((cur, gap_a))
                cur = gap_b
    keep.append((cur, end))
    keep = [(max(start, a), min(end, b)) for a, b in keep if min(end, b) > max(start, a)]
    return keep if sum(b - a for a, b in keep) >= 10000 else [(start, end)]


def abs_time(t, keep):
    """Ms in the trimmed clip -> absolute ms (inverse of clip_time)."""
    acc = 0
    for a, b in keep:
        if t <= acc + (b - a):
            return a + (t - acc)
        acc += b - a
    return None


def clip_time(t, keep):
    """Absolute ms -> ms in the trimmed clip, None if t falls in a removed pause."""
    acc = 0
    for a, b in keep:
        if t < a:
            return None
        if t <= b:
            return acc + t - a
        acc += b - a
    return None


_MISSING = object()


def layout_for(tpl, cfg, webcam=_MISSING):
    """Editable template (config.yaml `templates` + panel overrides) as the renderers' layout."""
    t = cfg["templates"]
    webcam = cfg["shorts"]["webcam_box"] if webcam is _MISSING else webcam
    if tpl not in ("split", "reaction", "gameplay"):
        tpl = cfg["shorts"]["template"]
    if not webcam and tpl != "gameplay":
        tpl = "gameplay"  # no webcam in the scene: nothing to put in a face panel
    lay = {k: dict(t[tpl][k]) for k in ("cam", "game", "caption", "hook")}
    lay["background"] = dict(t["background"])
    if not webcam:
        lay["cam"]["visible"] = False
    return tpl, lay


def frame_for(data, cfg):
    """(template, layout, webcam_box, game_box, source_size). Imported videos have their own size and no OBS scene:
    fit = whole video over the blurred background; fill = center crop filling the screen; template = the live
    layout (face + game), boxes scaled to the video's size."""
    sh = cfg["shorts"]
    tpl = data.get("template") or sh["template"]
    if "import_id" not in data:
        return (*layout_for(tpl, cfg), sh["webcam_box"], sh["game_box"], [1920, 1080])
    w, h = data["frame"]
    if data.get("layout_mode") == "template":
        sx, sy = w / 1920, h / 1080
        scale = (lambda b: [round(b[0] * sx), round(b[1] * sy), round(b[2] * sx), round(b[3] * sy)] if b else None)
        cam = scale(sh["webcam_box"])
        return (*layout_for(tpl, cfg, cam), cam, scale(sh["game_box"]), [w, h])
    tpl, lay = layout_for("gameplay", cfg, None)  # keeps the user's caption/hook style from "Só o jogo"
    if data.get("layout_mode") == "fill":
        lay["game"] = {"x": 0, "y": 0, "w": 1080, "h": 1920, "fit": "cover", "visible": True}
    else:  # fit: the WHOLE video, centered over the blurred background — even if "Só o jogo" was edited to fill
        gh = min(1920, round(1080 * h / w))
        lay["game"] = {"x": 0, "y": (1920 - gh) // 2, "w": 1080, "h": gh, "fit": "contain", "visible": True}
    return tpl, lay, None, [0, 0, w, h], [w, h]


def refine_punch(p, audio, sr=16000, window_ms=1500):
    """The model guesses the climax from the transcript and can be seconds off. The reaction is where the voice
    peaks: move the punch (clip ms) to the loudest 10 ms of voice within ±window_ms, if that peak stands out."""
    if audio is None or len(audio) < sr:
        return p
    hop = sr // 100
    n = len(audio) // hop
    env = np.sqrt(np.mean(audio[: n * hop].reshape(n, hop) ** 2, axis=1))
    a, b = max(0, (p - window_ms) // 10), min(n, (p + window_ms) // 10)
    if b - a < 5:
        return p
    i = a + int(np.argmax(env[a:b]))
    return int(i * 10) if env[i] >= 2.5 * (float(np.median(env)) + 1e-6) else p


def anchors_for(lay, plan_webcam):
    """Zoom focus presets (normalized canvas coords): face panel, game panel, center."""
    def center(r):
        return [round((r["x"] + r["w"] / 2) / 1080, 3), round((r["y"] + r["h"] / 2) / 1920, 3)]
    cam = lay.get("cam")
    return {"rosto": center(cam) if cam and cam.get("visible", True) and plan_webcam else None,
            "jogo": center(lay["game"]) if lay.get("game") else [0.5, 0.5], "centro": [0.5, 0.5]}


def build_plan(data, src, words, dur_ms, keep, cfg, voice=None):
    sh = cfg["shorts"]
    tpl, lay, webcam_box, game_box, size = frame_for(data, cfg)
    presets = anchors_for(lay, webcam_box)
    face = presets["rosto"] and data.get("category") in ("reacao", "humor")
    anchor = presets["rosto"] if face else presets["jogo"]  # zoom toward the face on reactions, else the game
    zooms = []
    for p in sorted(refine_punch(x, voice) for x in (clip_time(p, keep) for p in data.get("punch_ms", [])) if x is not None):
        if 600 < p < dur_ms - 600 and all(abs(p - z) >= 3000 for z in zooms):
            zooms.append(p)
    zooms = zooms[:2]
    effects = [{"type": "zoom", "start_ms": p - 250, "duration_ms": 700, "scale": 1.18, "anchor": anchor} for p in zooms]
    hook = (data.get("hook_text") or "").strip().upper()[:28] or None
    sfx = []
    if hook:
        sfx.append(("whoosh.wav", 0, -16))
    if zooms:
        sfx.append(("impact_soft.wav", zooms[0] - 250, -12))
    if len(zooms) > 1:
        sfx.append(("boing.wav" if data.get("category") in ("humor", "falha") else "pop.wav", zooms[1] - 250, -14))
    effects += [{"type": "sfx", "start_ms": max(0, t), "asset": str(SFX / f), "gain_db": g} for f, t, g in sfx[: sh["max_sfx"]]]
    return {
        "version": 1, "canvas": {"width": 1080, "height": 1920, "fps": sh["fps"]},
        "source": str(src), "source_size": size, "duration_ms": dur_ms, "template": tpl,
        "webcam_box": webcam_box, "game_box": game_box, "hook_text": hook, "layout": lay,
        "captions": caption_groups(words, dur_ms), "effects": effects, "workers": sh["hf_workers"],
    }


def srt(groups):
    def ts(ms):
        return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"
    return "\n".join(f"{i}\n{ts(g['start_ms'])} --> {ts(g['end_ms'])}\n{g['text']}\n" for i, g in enumerate(groups, 1))


def _guarded(fn, guard, *args):
    """Run a heavy step in a thread; abort it (our children only) if the live starts suffering. Returns fn's result."""
    box = {}

    def target():
        try:
            box["ret"] = fn(*args)
        except Exception as e:  # noqa: BLE001 — re-raised in caller thread
            box["err"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    while t.is_alive():
        t.join(3)
        reason = guard.trouble() if t.is_alive() else None
        if reason:
            guard.backoff(reason)
            box["aborted"] = reason
            kill_children()
            t.join(60)
    if "aborted" in box:
        raise Deferred(box["aborted"])
    if "err" in box:
        raise box["err"]
    return box.get("ret")


def _render(plan, out, wd, cfg, guard):
    order = [cfg["shorts"]["renderer"], cfg["shorts"]["fallback"]]
    for name in dict.fromkeys(r for r in order if r):
        try:
            if name == "hyperframes":
                from .render_hf import render
            else:
                from .render_ass import render
            _guarded(render, guard, plan, str(out), str(wd))
            return name
        except Deferred:
            raise
        except Exception as e:  # noqa: BLE001 — try the next renderer
            log.warning("renderizador %s falhou: %s", name, str(e)[-400:])
    raise RuntimeError("todos os renderizadores falharam")


PLANS = ROOT / "data" / "plans"  # one edit plan per finished clip: lets the user fix captions and re-render


def align_words(words, audio, sr=16000):
    """Whisper often starts a word (esp. the first after a pause) before the voice actually starts, so captions pop
    up early. Snap each word that starts in silence to the next voice onset (10 ms RMS envelope of the voice track)."""
    hop = sr // 100
    n = len(audio) // hop
    if n < 10 or not words:
        return words
    env = 20 * np.log10(np.sqrt(np.mean(audio[: n * hop].reshape(n, hop) ** 2, axis=1)) + 1e-9)
    voiced = env > max(float(np.percentile(env, 15)) + 8, -55)
    out, last_start = [], 0.0
    for i, (s, e, t, p) in enumerate(words):
        nxt = words[i + 1][0] if i + 1 < len(words) else n / 100
        a = int(s * 100)
        if a < n and not voiced[a]:
            lim = min(n, int(max(e, s + 1.5) * 100), max(a + 1, int(nxt * 100)))
            j = next((k for k in range(a, lim) if voiced[k]), None)
            if j is not None:
                s = j / 100
        s = max(s, last_start + 0.04)  # strictly increasing: collapsed timings would stack captions
        e = max(e, s + 0.08)
        out.append((s, e, t, p))
        last_start = s
    return out


def clip_words(wav, cfg):
    """Transcribe the final clip's (voice) audio: words land directly on the clip timeline, no remapping drift."""
    from .core import read_wav
    from .monitor import transcribe
    audio = read_wav(wav)
    _, words = transcribe(audio, cfg)
    words = [w for w in words if w[1] - w[0] >= 0.03]  # zero-length words are Whisper glitches (often repeats)
    words = align_words(words, audio)
    return [(int(s * 1000), int(e * 1000), t) for s, e, t, _ in words], [p for *_, p in words], audio


def render_candidate(con, cand, cfg, guard, recording_active):
    data = json.loads(cand["data"])
    sh, cap = cfg["shorts"], cfg["capture"]
    plan_file = PLANS / f"c{cand['id']}.json"
    wd = ROOT / "work" / f"c{cand['id']}"
    wd.mkdir(parents=True, exist_ok=True)
    src, voice = wd / "source.mp4", wd / "voice.wav"
    saved = json.loads(plan_file.read_text(encoding="utf-8")) if plan_file.exists() else None
    rerender = bool(data.get("rerender")) and saved is not None
    # cut moved, template/pause trimming changed: cut again; otherwise an edit only re-renders the same cut
    rebuild = rerender and bool(saved.get("recut") or saved.get("rebuild"))
    # mic not on the full-mix track (see Configuração → Áudio): mix the voice track into the clip soundtrack
    mixv = cap["transcribe_stream"] if cap.get("mix_add_voice") and cap.get("transcribe_stream") is not None else None
    imported = "import_id" in data
    if imported:  # an edited video: one already-mixed soundtrack, its own game, its own pacing
        from .imports import _icfg
        cap, mixv, cfg = {**cap, "audio_stream": 0, "transcribe_stream": None}, None, _icfg(cfg, data.get("game"))

    if rerender and not rebuild:  # captions/effects edited in the panel: same cut, new text/effects
        parts, keep, start, end, plan, probs = saved["parts"], saved["keep"], saved["start"], saved["end"], saved["plan"], []
        _guarded(cut_clip, guard, parts, str(src), sh["fps"], cap["audio_stream"], None, mixv)
        plan["source"] = str(src)
        dur_ms = plan["duration_ms"]
    else:
        start, end = (saved.get("recut") or (saved["start"], saved["end"])) if rebuild else (cand["start_ms"], cand["end_ms"])
        segs = con.execute("select * from segments where start_ms < ? and start_ms + duration_ms > ? order by start_ms",
                           (end, start)).fetchall()
        have_end = max((s["start_ms"] + s["duration_ms"] for s in segs), default=0)
        if have_end < end and (recording_active or media_pending(end)):
            raise Deferred("fim do clipe ainda não gravado/ingerido")
        if not segs:
            raise RuntimeError("sem mídia para o intervalo")
        start, end = max(start, segs[0]["start_ms"]), min(end, have_end)
        if end - start < 8000:
            raise RuntimeError("intervalo disponível curto demais")
        abs_words = con.execute("select start_ms, end_ms, text, prob from words where start_ms >= ? and end_ms <= ? "
                                "order by start_ms", (start, end)).fetchall()
        protect = [(r[0] - 600, r[0] + 600) for r in con.execute("select at_ms from loud where at_ms between ? and ?", (start, end))]
        protect += [(p - 1500, p + 800) for p in data.get("punch_ms", [])]
        motion = con.execute("select at_ms, value from motion where at_ms between ? and ?", (start, end)).fetchall()
        trim = data["trim"] if "trim" in data else (not data.get("no_trim") and sh.get("trim_pauses"))  # per-clip choice wins
        keep = keep_intervals(start, end, [(r[0], r[1]) for r in abs_words], protect, motion,
                              {**cfg, "shorts": {**sh, "trim_pauses": True}}) if trim else [(start, end)]
        parts = [(s["path"], (max(a, s["start_ms"]) - s["start_ms"]) / 1000,
                  (min(b, s["start_ms"] + s["duration_ms"]) - max(a, s["start_ms"])) / 1000)
                 for a, b in keep for s in segs]
        parts = [p for p in parts if p[2] > 0.05]
        vstream = cap["audio_stream"] if cap.get("transcribe_stream") is None else cap["transcribe_stream"]
        try:  # video + full mix for the clip, plus the voice track alone for captions
            _guarded(cut_clip, guard, parts, str(src), sh["fps"], cap["audio_stream"], (vstream, str(voice)), mixv)
        except Deferred:
            raise
        except RuntimeError:  # configured mic track missing in this recording: caption from the mix
            log.warning("trilha do microfone (%s) não encontrada; legendando pela trilha completa", vstream)
            _guarded(cut_clip, guard, parts, str(src), sh["fps"], cap["audio_stream"], (cap["audio_stream"], str(voice)))
        dur_ms = int(float(probe(src)["format"]["duration"]) * 1000)
        try:
            words, probs, voice_audio = clip_words(voice, cfg)
        except Exception as e:  # noqa: BLE001 — fall back to the segment transcript remapped onto the cut
            log.warning("transcrição do clipe falhou (%s); usando a do segmento", e)
            words = [(clip_time(s, keep), clip_time(e, keep), t) for s, e, t, _ in abs_words]
            words = [(s, e, t) for s, e, t in words if s is not None and e is not None]
            probs, voice_audio = [r[3] for r in abs_words], None
        plan = build_plan(data, src, words, dur_ms, keep, cfg, voice_audio)
        if rebuild:  # carry the user's work over to the new cut
            old, okeep = saved["plan"], saved["keep"]
            if not saved.get("recut") and [list(k) for k in keep] == [list(k) for k in okeep]:
                plan["captions"] = old["captions"]  # same cut: keep edited captions (new cut = fresh transcription)
            fx = []
            for e in old["effects"]:  # zooms/sounds stay on the same moment of the recording...
                a = abs_time(e["start_ms"], okeep)
                t = 0 if e["start_ms"] <= 100 else clip_time(a, keep) if a is not None else None  # ...except the
                # opening sound, which belongs to the hook text at the start of the clip
                if t is not None and 0 <= t < dur_ms - 100:
                    fx.append(dict(e, start_ms=int(t)))
            plan["effects"], plan["hook_text"] = fx, old.get("hook_text")
        if data.get("caption_style"):  # per-clip caption position/size from the editor
            plan["layout"]["caption"].update(data["caption_style"])

    raw = wd / "render.mp4"
    renderer = _render(plan, raw, wd, cfg, guard)
    when = datetime.datetime.fromtimestamp(start / 1000)
    pos = int(data.get("pos_s", 0))
    stamp = f"{data['import_slug']}_{pos // 60:02}m{pos % 60:02}s" if imported else f"{when:%Y%m%d_%H%M%S}"
    out = Path(cand["out_path"]) if rerender and cand["out_path"] else \
        ROOT / "output" / "review" / f"{stamp}_{cand['score']}_{slugify(data.get('title', 'clip')) or 'clip'}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".novo.mp4")
    from . import intro
    vin = intro.short_variant(cfg) if (cfg.get("branding") or {}).get("short_end") and data.get("intro", True) else None
    intro_ms = int(float(probe(vin)["format"]["duration"]) * 1000) if vin else 0
    if vin:  # your intro at the end of every Short (one re-encode joins and normalizes everything)
        _guarded(intro.append_to_short, guard, raw, vin, tmp, sh["loudness_lufs"])
    else:
        _guarded(run, guard, ["ffmpeg", "-y", "-v", "error", "-i", raw, "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy",
                              "-af", f"loudnorm=I={sh['loudness_lufs']}:TP=-1.5:LRA=11,aresample=48000",
                              "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", tmp])
    p = probe(tmp)
    v = next((s for s in p["streams"] if s["codec_type"] == "video"), None) if p else None
    if not v or (v["width"], v["height"]) != (1080, 1920) or not any(s["codec_type"] == "audio" for s in p["streams"]) \
            or abs(float(p["format"]["duration"]) * 1000 - dur_ms - intro_ms) > 1500:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("validação do MP4 final falhou")
    for i in range(10):  # the panel may be streaming the old file for a moment
        try:
            os.replace(tmp, out)
            break
        except PermissionError:
            time.sleep(1)
    else:
        raise RuntimeError("não consegui substituir o vídeo (arquivo em uso)")

    tags = {}
    for t in data.get("hashtags_final") or (all_hashtags(cfg) + data.get("hashtags", [])):  # editor's list wins
        t = "#" + t.lstrip("#").replace(" ", "")
        tags.setdefault(t.lower(), t)  # case-insensitive dedupe (#picopark == #PicoPark), first spelling wins
    tags = list(tags.values())[:15 if data.get("hashtags_final") else 8]
    meta = {
        "title": data.get("title"), "description": (data.get("description") or "").strip() + "\n\n" + " ".join(tags),
        "hashtags": tags, "score": cand["score"], "category": data.get("category"), "reason": data.get("reason"),
        "detected_by": data.get("source"), "renderer": renderer, "template": plan["template"],
        "live_time": None if imported else f"{when:%Y-%m-%d %H:%M:%S}", "duration_s": round(dur_ms / 1000, 1),
        "source_video": f"{data['import_name']} @ {pos // 60}:{pos % 60:02}" if imported else None,
        "pauses_cut_s": round((end - start - sum(b - a for a, b in keep)) / 1000, 1), "captions_edited": rerender,
        "intro_s": round(intro_ms / 1000, 1),
        "review": {"captions_uncertain": bool(probs) and float(np.mean(probs)) < 0.6,  # numpy bool isn't JSON
                   "spoiler_risk": data.get("spoiler_risk", False), "no_speech": not plan["captions"]},
    }
    out.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    out.with_suffix(".srt").write_text(srt(plan["captions"]), encoding="utf-8")
    zoom = data.get("thumb_ms") if data.get("thumb_ms") is not None and data["thumb_ms"] < dur_ms else \
        next((e["start_ms"] + 250 for e in plan["effects"] if e["type"] == "zoom"), dur_ms * 0.4)
    try:  # thumbnail at the climax, or where the user chose (for the panel and as a cover suggestion)
        run(["ffmpeg", "-y", "-v", "error", "-ss", f"{zoom / 1000:.2f}", "-i", out, "-frames:v", "1",
             "-vf", "scale=540:-2", "-q:v", "4", out.with_suffix(".jpg")], 60)
    except RuntimeError as e:
        log.warning("thumbnail falhou: %s", e)
    PLANS.mkdir(parents=True, exist_ok=True)
    plan["source"] = None  # the cut is temporary; parts re-create it
    plan_file.write_text(json.dumps({"parts": parts, "keep": keep, "start": start, "end": end, "plan": plan},
                                    ensure_ascii=False), encoding="utf-8")
    if data.pop("rerender", None):
        con.execute("update candidates set data=? where id=?", (json.dumps(data, ensure_ascii=False), cand["id"]))
    shutil.rmtree(wd, ignore_errors=True)
    log.info("Agente 2: clipe %s (%s, %.0fs, %s) -> %s", "refeito" if rerender else "pronto", renderer, dur_ms / 1000,
             plan["template"], out.name)
    return str(out)


def _sounds():
    return sorted(f.stem for f in SFX.glob("*.wav"))


CTX_PAD_MS = 45_000  # how much of the recording around the cut the editor can show / extend into
LIVE_TPL = {"split": "Rosto + jogo", "reaction": "Rosto grande", "gameplay": "Só o jogo"}
IMPORT_TPL = {"fit": "Inteiro + fundo desfocado", "fill": "Preencher a tela", "template": "Igual à live (rosto em cima)"}


def _media_bounds(con, start, end, pad=CTX_PAD_MS):
    """How far the cut can move: the recording available around [start, end] (±pad)."""
    segs = con.execute("select start_ms, duration_ms from segments where start_ms < ? and start_ms + duration_ms > ? "
                       "order by start_ms", (end + pad, start - pad)).fetchall()
    if not segs:
        return start, end
    return max(start - pad, segs[0][0]), min(end + pad, max(s[0] + s[1] for s in segs))


def _row_and_plan(con, cand_id):
    row = con.execute("select status, data, out_path from candidates where id=?", (cand_id,)).fetchone()
    f = PLANS / f"c{cand_id}.json"
    if not row or not f.exists():
        raise ValueError("Este clipe foi feito antes do editor existir — refaça-o para poder editar.")
    return row, json.loads(row["data"]), f, json.loads(f.read_text(encoding="utf-8"))


def load_edit(con, cand_id, cfg):
    """Everything the panel's clip editor needs for a finished clip."""
    from . import intro
    row, data, _, saved = _row_and_plan(con, cand_id)
    plan, fx = saved["plan"], saved["plan"].get("effects", [])
    imported = "import_id" in data
    lo, hi = _media_bounds(con, saved["start"], saved["end"])
    try:
        meta = json.loads(Path(row["out_path"]).with_suffix(".json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        meta = {}
    return {
        "duration_ms": plan["duration_ms"], "hook_text": plan.get("hook_text") or "",
        "captions": [{"text": g["text"], "start_ms": g["start_ms"], "end_ms": g["end_ms"]} for g in plan["captions"]],
        "zooms": [{"start_ms": e["start_ms"], "duration_ms": e["duration_ms"], "scale": e["scale"], "anchor": e["anchor"]}
                  for e in fx if e["type"] == "zoom"],
        "sfx": [{"start_ms": e["start_ms"], "sound": Path(e["asset"]).stem, "gain_db": e.get("gain_db", -14)}
                for e in fx if e["type"] == "sfx"],
        "sounds": _sounds(), "anchors": anchors_for(plan.get("layout") or {}, plan.get("webcam_box")),
        # the cut on the recording's timeline (absolute ms) and how far it can move; context video = [min_ms, max_ms]
        "cut": {"start_ms": saved["start"], "end_ms": saved["end"], "min_ms": lo, "max_ms": hi,
                "pauses_cut_ms": (saved["end"] - saved["start"]) - sum(b - a for a, b in saved["keep"])},
        "options": {
            "template": data.get("layout_mode", "fit") if imported else plan.get("template"),
            "templates": IMPORT_TPL if imported else LIVE_TPL,
            "trim_pauses": None if imported else bool(data.get("trim", cfg["shorts"].get("trim_pauses"))),
            "intro": bool(data.get("intro", True)) if intro.source(cfg) else None,
            "caption": {k: plan.get("layout", {}).get("caption", {}).get(k) for k in ("y", "size")},
        },
        "meta": {"title": data.get("title") or meta.get("title") or "", "description": (data.get("description") or "").strip(),
                 "hashtags": meta.get("hashtags") or data.get("hashtags_final") or []},
        "thumbnail_ms": data.get("thumb_ms"),
    }


def context_preview(con, cand_id, cfg):
    """Low-res video of the recording around the cut ([min_ms, max_ms] of load_edit), cached in work/ctx."""
    row, data, _, saved = _row_and_plan(con, cand_id)
    lo, hi = _media_bounds(con, saved["start"], saved["end"])
    out = ROOT / "work" / "ctx" / f"c{cand_id}_{lo}_{hi}.mp4"
    if out.exists():
        return out
    segs = con.execute("select path, start_ms, duration_ms from segments where start_ms < ? and start_ms + duration_ms > ? "
                       "order by start_ms", (hi, lo)).fetchall()
    stream = 0 if "import_id" in data else cfg["capture"]["audio_stream"]
    args, n = ["ffmpeg", "-y", "-v", "error"], 0
    for path, s0, d in segs:
        a, b = max(lo, s0), min(hi, s0 + d)
        if b - a > 50:
            args += ["-ss", f"{(a - s0) / 1000:.3f}", "-t", f"{(b - a) / 1000:.3f}", "-i", path]
            n += 1
    if not n:
        raise ValueError("A gravação original deste clipe não está mais disponível.")
    chain = "".join(f"[{i}:v:0][{i}:a:{stream}]" for i in range(n))
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.mp4")
    run(args + ["-filter_complex", f"{chain}concat=n={n}:v=1:a=1[v][a];[v]scale=-2:360,fps=30[vs]", "-map", "[vs]",
                "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-threads", "2", "-c:a", "aac",
                "-b:a", "96k", "-movflags", "+faststart", tmp], 600)
    tmp.replace(out)
    for old in out.parent.glob(f"c{cand_id}_*.mp4"):  # one cached context per clip
        if old != out:
            old.unlink(missing_ok=True)
    return out


def _num(v, lo, hi, what):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
        raise ValueError(f"{what}: valor fora de {lo}–{hi}.")
    return v


def _captions_from(groups, old, dur):
    caps = []
    for g in groups:
        try:
            text, s, e = str(g["text"])[:160], int(g["start_ms"]), int(g["end_ms"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("Cada legenda precisa de texto, início e fim.") from None
        s, e = max(0, min(s, dur)), max(0, min(e, dur))
        toks = [w for w in (clean_word(t) for t in text.split()) if w]
        if not toks or e - s < 100:
            continue
        same = next((o for o in old if o["start_ms"] == s and o["end_ms"] == e and len(o["words"]) == len(toks)), None)
        if same:  # same word count and times: keep Whisper's (aligned) word timings, only the text changes
            ws = [{"text": t, "start_ms": w["start_ms"], "end_ms": w["end_ms"]} for t, w in zip(toks, same["words"])]
        else:  # moved or re-worded: spread the words over the line by length
            total, acc, ws = sum(len(t) + 1 for t in toks), 0, []
            for t in toks:
                a = s + (e - s) * acc // total
                acc += len(t) + 1
                ws.append({"text": t, "start_ms": a, "end_ms": s + (e - s) * acc // total})
        caps.append({"text": " ".join(toks), "start_ms": s, "end_ms": e, "words": ws})
    return sorted(caps, key=lambda c: c["start_ms"])


def _sig(effects):
    """Comparable form of effects (sound file path -> sound name)."""
    return [{k: v for k, v in e.items() if k != "asset"} | ({"sound": Path(e["asset"]).stem} if "asset" in e else {})
            for e in effects]


def save_edit(con, cand_id, body, cfg):
    """Apply the editor: metadata/cover right away; captions, hook, effects, caption style and intro by re-rendering
    the same cut; a moved cut, a template or pause-trimming change by cutting the recording again."""
    row, data, f, saved = _row_and_plan(con, cand_id)
    if row["status"] != "done":
        raise ValueError("Só dá para editar clipes prontos.")
    if not isinstance(body, dict):
        raise ValueError("Formato inválido.")
    plan, dur = saved["plan"], saved["plan"]["duration_ms"]
    imported = "import_id" in data
    render, rebuild, notes = False, False, []

    # 1) title / description / hashtags: written to the sidecar now (no re-render)
    meta_in = body.get("meta")
    if meta_in is not None:
        title, desc, tags = meta_in.get("title", ""), meta_in.get("description", ""), meta_in.get("hashtags", [])
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 100:
            raise ValueError("Título: 1 a 100 caracteres.")
        if not isinstance(desc, str) or len(desc) > 2000:
            raise ValueError("Descrição: até 2000 caracteres.")
        if not isinstance(tags, list) or len(tags) > 15 or not all(isinstance(t, str) and 1 < len(t.strip()) <= 40 for t in tags):
            raise ValueError("Hashtags: até 15, cada uma com até 40 caracteres.")
        tags = list(dict.fromkeys("#" + t.strip().lstrip("#").replace(" ", "") for t in tags))
        changed = (title.strip(), desc.strip(), tags) != (data.get("title"), (data.get("description") or "").strip(),
                                                          data.get("hashtags_final"))
        data.update(title=title.strip(), description=desc.strip(), hashtags_final=tags)
        try:
            side = Path(row["out_path"]).with_suffix(".json")
            m = json.loads(side.read_text(encoding="utf-8"))
            m.update(title=data["title"], hashtags=tags, description=(data["description"] + "\n\n" + " ".join(tags)).strip())
            side.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
            if changed:
                notes.append("título/descrição")
        except (OSError, ValueError, TypeError):
            pass

    # 2) the cut itself (absolute ms on the recording)
    cut = body.get("cut")
    if cut is not None:
        lo, hi = _media_bounds(con, saved["start"], saved["end"])
        try:
            s, e = int(cut["start_ms"]), int(cut["end_ms"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("Corte inválido.") from None
        if s < lo or e > hi:
            raise ValueError("O corte saiu da gravação disponível.")
        if not 5000 <= e - s <= 180_000:
            raise ValueError("O clipe precisa ter entre 5 s e 3 min.")
        if abs(s - saved["start"]) > 50 or abs(e - saved["end"]) > 50:
            ds, de = (saved["start"] - s) / 1000, (e - saved["end"]) / 1000
            saved["recut"], rebuild = [s, e], True
            notes.append(f"corte: início {ds:+.1f}s, fim {de:+.1f}s (legenda refeita)")

    # 3) per-clip options
    opts = body.get("options") or {}
    choices = IMPORT_TPL if imported else LIVE_TPL
    if opts.get("template") is not None:
        if opts["template"] not in choices:
            raise ValueError("Modelo inválido.")
        key = "layout_mode" if imported else "template"
        if opts["template"] != (data.get(key, "fit") if imported else plan.get("template")):
            data[key], rebuild = opts["template"], True
            notes.append(f"modelo: {choices[opts['template']]}")
    if opts.get("trim_pauses") is not None and not imported:
        if not isinstance(opts["trim_pauses"], bool):
            raise ValueError("Cortar pausas: sim ou não.")
        if opts["trim_pauses"] != bool(data.get("trim", cfg["shorts"].get("trim_pauses"))):
            data["trim"], rebuild = opts["trim_pauses"], True
            notes.append("cortar pausas " + ("ligado" if opts["trim_pauses"] else "desligado"))
    if opts.get("intro") is not None:
        if not isinstance(opts["intro"], bool):
            raise ValueError("Vinheta: sim ou não.")
        if opts["intro"] != data.get("intro", True):
            data["intro"], render = opts["intro"], True
            notes.append("vinheta " + ("ligada" if opts["intro"] else "desligada"))
    capst = opts.get("caption")
    if capst:
        y = float(_num(capst.get("y"), 0.05, 0.95, "Posição da legenda"))
        size = int(_num(capst.get("size"), 32, 160, "Tamanho da legenda"))
        cur = plan.get("layout", {}).get("caption", {})
        if abs((cur.get("y") or 0) - y) > 0.001 or cur.get("size") != size:
            data["caption_style"] = {"y": round(y, 3), "size": size}
            plan["layout"]["caption"].update(data["caption_style"])
            render = True
            notes.append("estilo da legenda")

    # 4) captions, hook, zooms, sounds (on the current clip timeline)
    caps, zooms, sfx, hook = body.get("captions"), body.get("zooms"), body.get("sfx"), body.get("hook_text")
    if caps is not None or zooms is not None or sfx is not None or hook is not None:
        caps = caps if caps is not None else [{"text": c["text"], "start_ms": c["start_ms"], "end_ms": c["end_ms"]}
                                              for c in plan["captions"]]
        zooms = zooms if zooms is not None else [e for e in plan["effects"] if e["type"] == "zoom"]
        sfx = sfx if sfx is not None else [{"start_ms": e["start_ms"], "sound": Path(e["asset"]).stem, "gain_db": e["gain_db"]}
                                           for e in plan["effects"] if e["type"] == "sfx"]
        hook = hook if hook is not None else (plan.get("hook_text") or "")
        if not (isinstance(caps, list) and len(caps) <= 500 and isinstance(zooms, list) and len(zooms) <= 12
                and isinstance(sfx, list) and len(sfx) <= 12 and isinstance(hook, str) and len(hook) <= 60):
            raise ValueError("Formato inválido (máx. 12 zooms e 12 sons).")
        sounds, fx = set(_sounds()), []
        for z in sorted(zooms, key=lambda z: z.get("start_ms", 0) if isinstance(z, dict) else 0):
            try:
                s = int(_num(z["start_ms"], 0, dur, "Início do zoom"))
                d = int(_num(z["duration_ms"], 200, 4000, "Duração do zoom"))
                sc = round(float(_num(z["scale"], 1.02, 1.6, "Força do zoom")), 3)
                ax, ay = (round(float(_num(v, 0, 1, "Foco do zoom")), 3) for v in z["anchor"])
            except (KeyError, TypeError, ValueError) as e:
                raise ValueError(str(e) if isinstance(e, ValueError) and str(e) else "Zoom incompleto.") from None
            if fx and s < fx[-1]["start_ms"] + fx[-1]["duration_ms"]:
                raise ValueError("Dois zooms não podem se sobrepor — afaste-os na timeline.")
            fx.append({"type": "zoom", "start_ms": s, "duration_ms": min(d, dur - s), "scale": sc, "anchor": [ax, ay]})
        for x in sfx:
            try:
                name = str(x["sound"])
                if name not in sounds:
                    raise ValueError(f"Som desconhecido: {name}")
                s = int(_num(x["start_ms"], 0, dur, "Início do som"))
                g = round(float(_num(x["gain_db"], -40, 0, "Volume do som")), 1)
            except (KeyError, TypeError):
                raise ValueError("Som incompleto.") from None
            fx.append({"type": "sfx", "start_ms": s, "asset": str(SFX / f"{name}.wav"), "gain_db": g})
        new_caps, new_hook = _captions_from(caps, plan["captions"], dur), hook.strip().upper()[:28] or None
        caps_changed = [(c["text"], c["start_ms"], c["end_ms"]) for c in new_caps] != \
            [(c["text"], c["start_ms"], c["end_ms"]) for c in plan["captions"]]
        if _sig(fx) != _sig(plan["effects"]) or new_hook != plan.get("hook_text") or (caps_changed and not saved.get("recut")):
            plan["effects"], plan["hook_text"], render = fx, new_hook, True
            if not saved.get("recut"):  # a new cut gets a fresh transcription anyway
                plan["captions"] = new_caps
            notes.append("legenda/efeitos")

    # 5) cover frame
    th = body.get("thumbnail_ms")
    if th is not None and th != data.get("thumb_ms"):
        data["thumb_ms"] = int(_num(th, 0, dur, "Capa"))
        if not (render or rebuild) and row["out_path"]:
            run(["ffmpeg", "-y", "-v", "error", "-ss", f"{data['thumb_ms'] / 1000:.2f}", "-i", row["out_path"], "-frames:v",
                 "1", "-vf", "scale=540:-2", "-q:v", "4", Path(row["out_path"]).with_suffix(".jpg")], 60)
        notes.append("capa")

    if rebuild:
        saved["rebuild"] = True
    f.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
    if render or rebuild:
        data["rerender"] = True
        con.execute("update candidates set status='pending', priority=1, next_at=0, attempts=0, error=null, data=? where id=?",
                    (json.dumps(data, ensure_ascii=False), cand_id))
        return "Salvo (" + ", ".join(notes) + ") — o clipe vai ser refeito em instantes."
    con.execute("update candidates set data=? where id=?", (json.dumps(data, ensure_ascii=False), cand_id))
    return ("Salvo: " + ", ".join(notes) + ".") if notes else "Nada mudou."


def delete_clip(con, table, row_id):
    """Delete a clip/episode's files (only inside output/) and hide it; the moment won't be re-created."""
    if table not in ("candidates", "episodes"):
        raise ValueError("tipo inválido")
    row = con.execute(f"select status, out_path from {table} where id=?", (row_id,)).fetchone()
    if not row:
        raise ValueError("não encontrado")
    if row["status"] == "running":
        raise ValueError("Está sendo editado agora — tente de novo em instantes.")
    freed, out_root = 0, (ROOT / "output").resolve()
    if row["out_path"]:
        base = Path(row["out_path"]).resolve()
        if out_root in base.parents:
            for f in (base.with_suffix(s) for s in (".mp4", ".json", ".srt", ".jpg")):
                if f.exists():
                    freed += f.stat().st_size
                    f.unlink()
    if table == "candidates":
        (PLANS / f"c{row_id}.json").unlink(missing_ok=True)
    con.execute(f"update {table} set status='deleted', out_path=null, error=null where id=?", (row_id,))
    return f"Excluído ({freed / 1e6:.0f} MB liberados)."
