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
        out.append({"text": " ".join(w["text"] for w in g), "start_ms": g[0]["start_ms"],
                    "end_ms": min(g[-1]["end_ms"] + 250, nxt, dur_ms), "words": g})
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


def layout_for(tpl, cfg):
    """Editable template (config.yaml `templates` + panel overrides) as the renderers' layout."""
    t = cfg["templates"]
    if tpl not in ("split", "reaction", "gameplay"):
        tpl = cfg["shorts"]["template"]
    if not cfg["shorts"]["webcam_box"] and tpl != "gameplay":
        tpl = "gameplay"  # no webcam in the scene: nothing to put in a face panel
    lay = {k: dict(t[tpl][k]) for k in ("cam", "game", "caption", "hook")}
    lay["background"] = dict(t["background"])
    if not cfg["shorts"]["webcam_box"]:
        lay["cam"]["visible"] = False
    return tpl, lay


def build_plan(data, src, words, dur_ms, keep, cfg):
    sh = cfg["shorts"]
    tpl, lay = layout_for(data.get("template") or sh["template"], cfg)
    cam, game = lay["cam"], lay["game"]
    face = cam["visible"] and data.get("category") in ("reacao", "humor")
    r = cam if face else game  # zoom toward the face on reactions, else toward the game
    anchor = [round((r["x"] + r["w"] / 2) / 1080, 3), round((r["y"] + r["h"] / 2) / 1920, 3)]
    zooms = []
    for p in sorted(x for x in (clip_time(p, keep) for p in data.get("punch_ms", [])) if x is not None):
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
        "source": str(src), "source_size": [1920, 1080], "duration_ms": dur_ms, "template": tpl,
        "webcam_box": sh["webcam_box"], "game_box": sh["game_box"], "hook_text": hook, "layout": lay,
        "captions": caption_groups(words, dur_ms), "effects": effects, "workers": sh["hf_workers"],
    }


def srt(groups):
    def ts(ms):
        return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"
    return "\n".join(f"{i}\n{ts(g['start_ms'])} --> {ts(g['end_ms'])}\n{g['text']}\n" for i, g in enumerate(groups, 1))


def _guarded(fn, guard, *args):
    """Run a heavy step in a thread; abort it (our children only) if the live starts suffering."""
    box = {}

    def target():
        try:
            fn(*args)
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
        s = max(s, last_start)
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
    words = align_words(words, audio)
    return [(int(s * 1000), int(e * 1000), t) for s, e, t, _ in words], [p for *_, p in words]


def render_candidate(con, cand, cfg, guard, recording_active):
    data = json.loads(cand["data"])
    sh, cap = cfg["shorts"], cfg["capture"]
    plan_file = PLANS / f"c{cand['id']}.json"
    wd = ROOT / "work" / f"c{cand['id']}"
    wd.mkdir(parents=True, exist_ok=True)
    src, voice = wd / "source.mp4", wd / "voice.wav"
    rerender = bool(data.get("rerender")) and plan_file.exists()

    if rerender:  # captions edited in the panel: same cut, same effects, new text
        saved = json.loads(plan_file.read_text(encoding="utf-8"))
        parts, keep, start, end, plan, probs = saved["parts"], saved["keep"], saved["start"], saved["end"], saved["plan"], []
        _guarded(cut_clip, guard, parts, str(src), sh["fps"], cap["audio_stream"])
        plan["source"] = str(src)
        dur_ms = plan["duration_ms"]
    else:
        start, end = cand["start_ms"], cand["end_ms"]
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
        keep = keep_intervals(start, end, [(r[0], r[1]) for r in abs_words], protect, motion, cfg)
        parts = [(s["path"], (max(a, s["start_ms"]) - s["start_ms"]) / 1000,
                  (min(b, s["start_ms"] + s["duration_ms"]) - max(a, s["start_ms"])) / 1000)
                 for a, b in keep for s in segs]
        parts = [p for p in parts if p[2] > 0.05]
        vstream = cap["audio_stream"] if cap.get("transcribe_stream") is None else cap["transcribe_stream"]
        try:  # video + full mix for the clip, plus the voice track alone for captions
            _guarded(cut_clip, guard, parts, str(src), sh["fps"], cap["audio_stream"], (vstream, str(voice)))
        except Deferred:
            raise
        except RuntimeError:  # configured mic track missing in this recording: caption from the mix
            log.warning("trilha do microfone (%s) não encontrada; legendando pela trilha completa", vstream)
            _guarded(cut_clip, guard, parts, str(src), sh["fps"], cap["audio_stream"], (cap["audio_stream"], str(voice)))
        dur_ms = int(float(probe(src)["format"]["duration"]) * 1000)
        try:
            words, probs = clip_words(voice, cfg)
        except Exception as e:  # noqa: BLE001 — fall back to the segment transcript remapped onto the cut
            log.warning("transcrição do clipe falhou (%s); usando a do segmento", e)
            words = [(clip_time(s, keep), clip_time(e, keep), t) for s, e, t, _ in abs_words]
            words = [(s, e, t) for s, e, t in words if s is not None and e is not None]
            probs = [r[3] for r in abs_words]
        plan = build_plan(data, src, words, dur_ms, keep, cfg)

    raw = wd / "render.mp4"
    renderer = _render(plan, raw, wd, cfg, guard)
    when = datetime.datetime.fromtimestamp(start / 1000)
    out = Path(cand["out_path"]) if rerender and cand["out_path"] else \
        ROOT / "output" / "review" / f"{when:%Y%m%d_%H%M%S}_{cand['score']}_{slugify(data.get('title', 'clip')) or 'clip'}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".novo.mp4")
    _guarded(run, guard, ["ffmpeg", "-y", "-v", "error", "-i", raw, "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy",
                          "-af", f"loudnorm=I={sh['loudness_lufs']}:TP=-1.5:LRA=11,aresample=48000",
                          "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", tmp])
    p = probe(tmp)
    v = next((s for s in p["streams"] if s["codec_type"] == "video"), None) if p else None
    if not v or (v["width"], v["height"]) != (1080, 1920) or not any(s["codec_type"] == "audio" for s in p["streams"]) \
            or abs(float(p["format"]["duration"]) * 1000 - dur_ms) > 1500:
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
    for t in all_hashtags(cfg) + data.get("hashtags", []):
        t = "#" + t.lstrip("#").replace(" ", "")
        tags.setdefault(t.lower(), t)  # case-insensitive dedupe (#picopark == #PicoPark), first spelling wins
    tags = list(tags.values())[:8]
    meta = {
        "title": data.get("title"), "description": (data.get("description") or "").strip() + "\n\n" + " ".join(tags),
        "hashtags": tags, "score": cand["score"], "category": data.get("category"), "reason": data.get("reason"),
        "detected_by": data.get("source"), "renderer": renderer, "template": plan["template"],
        "live_time": f"{when:%Y-%m-%d %H:%M:%S}", "duration_s": round(dur_ms / 1000, 1),
        "pauses_cut_s": round((end - start - sum(b - a for a, b in keep)) / 1000, 1), "captions_edited": rerender,
        "review": {"captions_uncertain": bool(probs) and float(np.mean(probs)) < 0.6,  # numpy bool isn't JSON
                   "spoiler_risk": data.get("spoiler_risk", False), "no_speech": not plan["captions"]},
    }
    out.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    out.with_suffix(".srt").write_text(srt(plan["captions"]), encoding="utf-8")
    zoom = next((e["start_ms"] + 250 for e in plan["effects"] if e["type"] == "zoom"), dur_ms * 0.4)
    try:  # thumbnail at the climax (for the panel and as a cover suggestion)
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


def load_captions(con, cand_id):
    """Caption groups of a finished clip for the panel editor."""
    f = PLANS / f"c{cand_id}.json"
    if not f.exists():
        raise ValueError("Este clipe foi feito antes da edição de legendas existir — refaça-o para poder editar.")
    plan = json.loads(f.read_text(encoding="utf-8"))["plan"]
    return {"groups": [{"text": g["text"], "start_ms": g["start_ms"], "end_ms": g["end_ms"]} for g in plan["captions"]],
            "hook_text": plan.get("hook_text") or "", "duration_ms": plan["duration_ms"]}


def edit_captions(con, cand_id, groups, hook_text=""):
    """Save edited caption text/timing and queue the clip to be re-rendered with the same cut and effects."""
    row = con.execute("select status, data from candidates where id=?", (cand_id,)).fetchone()
    if not row or row["status"] != "done":
        raise ValueError("Só dá para editar a legenda de clipes prontos.")
    f = PLANS / f"c{cand_id}.json"
    if not f.exists():
        raise ValueError("Plano deste clipe não encontrado.")
    saved = json.loads(f.read_text(encoding="utf-8"))
    plan, dur = saved["plan"], saved["plan"]["duration_ms"]
    if not isinstance(groups, list) or len(groups) > 500 or not isinstance(hook_text, str) or len(hook_text) > 60:
        raise ValueError("Formato de legenda inválido.")
    old, caps = plan["captions"], []
    for g in groups:
        try:
            text, s, e = str(g["text"])[:160], int(g["start_ms"]), int(g["end_ms"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("Cada linha precisa de texto, início e fim.") from None
        s, e = max(0, min(s, dur)), max(0, min(e, dur))
        toks = [w for w in (clean_word(t) for t in text.split()) if w]
        if not toks or e - s < 100:
            continue
        same = next((o for o in old if o["start_ms"] == s and o["end_ms"] == e and len(o["words"]) == len(toks)), None)
        if same:  # same words count and times: keep Whisper's (aligned) word timings, only the text changes
            ws = [{"text": t, "start_ms": w["start_ms"], "end_ms": w["end_ms"]} for t, w in zip(toks, same["words"])]
        else:  # retimed or re-worded: spread the words over the line by length
            total, acc, ws = sum(len(t) + 1 for t in toks), 0, []
            for t in toks:
                a = s + (e - s) * acc // total
                acc += len(t) + 1
                ws.append({"text": t, "start_ms": a, "end_ms": s + (e - s) * acc // total})
        caps.append({"text": " ".join(toks), "start_ms": s, "end_ms": e, "words": ws})
    plan["captions"] = sorted(caps, key=lambda c: c["start_ms"])
    hook = hook_text.strip().upper()[:28] or None
    plan["hook_text"] = hook
    fx = [x for x in plan["effects"] if not (x["type"] == "sfx" and x["asset"].endswith("whoosh.wav"))]
    if hook:
        fx.append({"type": "sfx", "start_ms": 0, "asset": str(SFX / "whoosh.wav"), "gain_db": -16})
    plan["effects"] = fx
    f.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
    data = json.loads(row["data"])
    data["rerender"] = True
    con.execute("update candidates set status='pending', priority=1, next_at=0, attempts=0, error=null, data=? where id=?",
                (json.dumps(data, ensure_ascii=False), cand_id))
    return "Legenda salva — o clipe vai ser refeito em instantes (fila com prioridade)."


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
