"""Smallest checks for the non-trivial logic. Run: .venv\\Scripts\\python -m tests.test_logic"""
import numpy as np

from liveforge.core import load_config, start_ms_from_name
from liveforge.monitor import heuristic, loud_peaks, normalize
from liveforge.shorts import caption_groups, srt

cfg = load_config()

# captions: <=5 words, break on pauses and on !/?, uppercase, clamped to clip
words = [(0, 300, "mano,"), (300, 600, "olha"), (600, 900, "isso!"), (900, 1200, "não"), (1200, 1500, "acredito"),
         (3000, 3300, "que"), (3300, 3600, "ele"), (3600, 3900, "fez"), (3900, 4200, "isso"), (4200, 4500, "de"),
         (4500, 4800, "novo"), (9900, 10500, "fim")]
g = caption_groups(words, 10000)
assert [x["text"] for x in g] == ["MANO OLHA ISSO!", "NÃO ACREDITO", "QUE ELE FEZ ISSO DE", "NOVO", "FIM"], g
assert all(len(x["words"]) <= 5 for x in g) and g[-1]["end_ms"] == 10000 and g[1]["end_ms"] <= 3000
assert srt(g).startswith("1\n00:00:00,000 --> ")

# loudness: a loud burst in quiet noise is found near its time
a = np.random.default_rng(0).normal(0, 0.01, 16000 * 20).astype(np.float32)
a[16000 * 12: 16000 * 13] *= 30
p = loud_peaks(a)
assert p and abs(p[0][0] - 12.25) < 1, p

# normalize: too long gets trimmed around the punch, too short gets padded
c = normalize({"start_s": 0, "end_s": 120, "punch_s": [100], "score": 90}, cfg, 180)
assert c["end_s"] - c["start_s"] == cfg["shorts"]["duration_max"] and c["start_s"] <= 100 <= c["end_s"]
c = normalize({"start_s": 50, "end_s": 55, "punch_s": [], "score": 90}, cfg, 180)
assert c["end_s"] - c["start_s"] == cfg["shorts"]["duration_min"]

# heuristic: a marker + shouting produces a candidate around it
h = heuristic([(29, 31, "NÃO MANO SOCORRO kkkkk")], [(30, 15)], [30], 180, cfg)
assert h and h[0]["start_s"] < 30 < h[0]["end_s"], h

assert start_ms_from_name("2026-11-16 21-04-33.mkv") > 0

# pause trimming: a long still, silent gap is cut; speech, punches and the edges are kept
from liveforge.shorts import clip_time, keep_intervals
motion = [(t, 0.05 if not 8000 <= t < 14000 else 0.001) for t in range(0, 30000, 500)]
keep = keep_intervals(0, 30000, [(1000, 7500), (14500, 29000)], [(20000, 21000)], motion, cfg)
assert keep[0][0] == 0 and keep[-1][1] == 30000 and len(keep) == 2, keep
assert 7500 < keep[0][1] < 9000 and 13000 < keep[1][0] < 14500, keep
assert clip_time(1000, keep) == 1000 and clip_time(11000, keep) is None
assert clip_time(keep[1][0], keep) == keep[0][1]
moving = [(t, 0.05) for t in range(0, 30000, 500)]  # same gap but the picture moves -> nothing cut
assert keep_intervals(0, 30000, [(1000, 7500), (14500, 29000)], [], moving, cfg) == [(0, 30000)]

# episodes: breaks removed, chapters follow YouTube rules
from liveforge.episodes import clean_chapters, keep_ranges
assert keep_ranges(0, 100, [(10, 20), (50, 60)]) == [(0, 10), (20, 50), (60, 100)]
ch = clean_chapters([{"t_s": 5, "title": "A"}, {"t_s": 30, "title": "x"}, {"t_s": 200, "title": "B"}, {"t_s": 400, "title": "C"}], 600)
assert [c["t_s"] for c in ch] == [0, 200, 400], ch

# visual jolts: a single abrupt change stands out
from liveforge.monitor import visual_events
m = np.full(400, 0.02, np.float32)
m[200] = 0.4
assert visual_events(m, 4) and abs(visual_events(m, 4)[0][0] - 50.25) < 0.01
# caption sync: a word Whisper starts in silence snaps to the real voice onset; voiced starts stay put
from liveforge.shorts import align_words
sr = 16000
voice = np.random.default_rng(1).normal(0, 0.001, sr * 4).astype(np.float32)
voice[int(1.5 * sr):int(2.5 * sr)] += 0.3 * np.sin(np.arange(sr) * 2 * np.pi * 220 / sr).astype(np.float32)
al = align_words([(0.6, 2.0, "MANO", 0.9), (2.0, 2.4, "OLHA", 0.9)], voice)
assert 1.45 <= al[0][0] <= 1.6 and al[0][1] >= al[0][0] + 0.08, al
assert al[1][0] == 2.0, al

# game name from the OBS capture target + automatic hashtag
from liveforge.core import hashtags
from liveforge.guard import game_from_window
assert game_from_window("SONIC PICO PARK [Streaming]:SDL_app:streaming_client.exe") == "Sonic Pico Park"
c2 = {"session": {"game": "auto", "current_game": "Sonic Pico Park", "hashtags": ["#shorts", "#sonicpicopark"]}}
assert hashtags(c2) == ["#sonicpicopark", "#shorts"], hashtags(c2)
# collapsed Whisper timings never produce zero-length caption lines (they'd stay on screen in HyperFrames)
col = [(10700, 10900, "fazendo?"), (10900, 10900, "você"), (10900, 10900, "está"), (10900, 10900, "fazendo"),
       (10900, 10900, "o"), (10900, 10900, "que"), (11000, 11400, "isso")]
cg = caption_groups(col, 20000)
assert all(g["end_ms"] - g["start_ms"] >= 200 for g in cg), [(g["text"], g["start_ms"], g["end_ms"]) for g in cg]
assert all(a["end_ms"] <= b["start_ms"] for a, b in zip(cg, cg[1:])), cg

# zoom placement: the model's punch guess moves to the voice peak (shout) within ±1.5 s
from liveforge.shorts import refine_punch
shout = np.random.default_rng(2).normal(0, 0.005, sr * 10).astype(np.float32)
shout[int(6.2 * sr):int(6.6 * sr)] += 0.5
assert 6150 <= refine_punch(5000, shout) <= 6600, refine_punch(5000, shout)
assert refine_punch(1000, shout) == 1000  # no clear peak nearby: keep the guess

# clip editor: valid edit re-queues the clip; overlaps, unknown sounds and out-of-range values are refused
import json as _json
import sqlite3
import tempfile
from pathlib import Path as _P
from liveforge import shorts
tmp = _P(tempfile.mkdtemp())
shorts.PLANS = tmp
con = sqlite3.connect(":memory:")
con.row_factory = sqlite3.Row
con.execute("create table candidates(id integer primary key, status text, data text, priority int, next_at real, attempts int, error text)")
con.execute("insert into candidates values (7, 'done', '{}', 0, 0, 0, null)")
plan = {"duration_ms": 10000, "hook_text": None, "webcam_box": None, "layout": {"game": {"x": 0, "y": 656, "w": 1080, "h": 608}},
        "captions": [{"text": "OI", "start_ms": 500, "end_ms": 900, "words": [{"text": "OI", "start_ms": 500, "end_ms": 900}]}],
        "effects": [{"type": "zoom", "start_ms": 2000, "duration_ms": 700, "scale": 1.18, "anchor": [0.5, 0.5]},
                    {"type": "sfx", "start_ms": 2000, "asset": "assets/sfx/pop.wav", "gain_db": -14}]}
(tmp / "c7.json").write_text(_json.dumps({"parts": [], "keep": [[0, 10000]], "start": 0, "end": 10000, "plan": plan}))
con.execute("alter table candidates add column out_path text")
con.execute("create table segments(path text, start_ms integer, duration_ms integer)")
con.execute("insert into segments values ('rec.mkv', -60000, 120000)")  # recording around the clip: [-60 s, +60 s]
ed = shorts.load_edit(con, 7, cfg)
assert ed["cut"]["min_ms"] == -45000 and ed["cut"]["max_ms"] == 55000, ed["cut"]  # ±45 s, clamped to the recording
assert ed["options"]["templates"]["split"] and ed["options"]["trim_pauses"] is not None
assert ed["zooms"][0]["start_ms"] == 2000 and ed["sfx"][0]["sound"] == "pop" and "boing" in ed["sounds"]
ed["zooms"][0]["start_ms"] = 6000
ed["sfx"] = [{"start_ms": 6000, "sound": "impact_soft", "gain_db": -10}]
shorts.save_edit(con, 7, {k: ed[k] for k in ("captions", "zooms", "sfx", "hook_text")}, cfg)
row = con.execute("select status, priority, data from candidates where id=7").fetchone()
assert row["status"] == "pending" and row["priority"] == 1 and _json.loads(row["data"])["rerender"]
fx = _json.loads((tmp / "c7.json").read_text())["plan"]["effects"]
assert fx[0]["start_ms"] == 6000 and fx[1]["asset"].endswith("impact_soft.wav")
con.execute("update candidates set status='done' where id=7")
for bad in ({"zooms": [{"start_ms": 1000, "duration_ms": 900, "scale": 1.2, "anchor": [.5, .5]},
                       {"start_ms": 1500, "duration_ms": 500, "scale": 1.2, "anchor": [.5, .5]}]},
            {"sfx": [{"start_ms": 100, "sound": "../../config", "gain_db": -10}]},
            {"zooms": [{"start_ms": 100, "duration_ms": 500, "scale": 9, "anchor": [.5, .5]}]}):
    try:
        shorts.save_edit(con, 7, bad, cfg)
        raise AssertionError(bad)
    except ValueError:
        pass
# extending the cut 3 s earlier marks a re-cut; effects are remapped onto the recording by the renderer
shorts.save_edit(con, 7, {"cut": {"start_ms": -3000, "end_ms": 10000}}, cfg)
sv = _json.loads((tmp / "c7.json").read_text())
assert sv["recut"] == [-3000, 10000] and sv["rebuild"], sv
for bad in ({"cut": {"start_ms": -50000, "end_ms": 10000}}, {"cut": {"start_ms": 0, "end_ms": 2000}}, {"options": {"template": "x"}}):
    con.execute("update candidates set status='done' where id=7")
    try:
        shorts.save_edit(con, 7, bad, cfg)
        raise AssertionError(bad)
    except ValueError:
        pass
from liveforge.shorts import abs_time
kp = [(0, 1000), (3000, 6000)]
assert abs_time(1500, kp) == 3500 and clip_time(abs_time(3200, kp), kp) == 3200 and abs_time(4200, kp) is None
print("ok")
