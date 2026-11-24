"""Settings changed from the panel live in data/settings.json and override config.yaml (which keeps the defaults).
Only allow-listed keys with validated values are accepted: the panel is local, but this is still a trust boundary."""
import copy
import json
import os
import re
from pathlib import Path

from .core import ROOT, load_config

PATH = ROOT / "data" / "settings.json"
COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
TEMPLATES = ("split", "reaction", "gameplay")


def _num(lo, hi, cast=float):
    def check(v):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise ValueError(f"precisa estar entre {lo} e {hi}")
        return cast(v)
    return check


def _bool(v):
    if not isinstance(v, bool):
        raise ValueError("precisa ser verdadeiro/falso")
    return v


def _enum(*opts):
    def check(v):
        if v not in opts:
            raise ValueError(f"opções: {', '.join(opts)}")
        return v
    return check


def _color(v):
    if not isinstance(v, str) or not COLOR.match(v):
        raise ValueError("cor no formato #RRGGBB")
    return v.upper()


def _box(w, h):
    def check(v):
        if v is None:
            return None
        if not (isinstance(v, list) and len(v) == 4 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v)):
            raise ValueError("caixa [x, y, largura, altura]")
        x, y, bw, bh = (int(round(n)) for n in v)
        if bw < 16 or bh < 16 or x < 0 or y < 0 or x + bw > w or y + bh > h:
            raise ValueError(f"caixa fora do quadro {w}x{h}")
        return [x, y, bw, bh]
    return check


def _text(maxlen):
    def check(v):
        if not isinstance(v, str) or len(v) > maxlen:
            raise ValueError(f"texto de até {maxlen} caracteres")
        return v.strip()
    return check


def _channel(v):
    v = (v or "").strip().lower().lstrip("#")
    if v and not re.fullmatch(r"[a-z0-9_]{3,25}", v):
        raise ValueError("nome de canal da Twitch inválido")
    return v


def _words(v):
    if not isinstance(v, list) or len(v) > 60 or not all(isinstance(t, str) and 0 < len(t.strip()) <= 40 for t in v):
        raise ValueError("lista de até 60 palavras/nomes")
    return list(dict.fromkeys(t.strip() for t in v))


def _stream(v):
    return None if v is None else _num(0, 5, int)(v)


def _video_path(v):
    if v in (None, ""):
        return None
    if not isinstance(v, str) or len(v) > 400:
        raise ValueError("caminho inválido")
    f = Path(v) if Path(v).is_absolute() else ROOT / v
    if f.suffix.lower() not in (".mp4", ".mov", ".mkv", ".webm") or not f.is_file():
        raise ValueError("arquivo de vídeo não encontrado (mp4, mov, mkv ou webm)")
    return v


def _hashtags(v):
    if not isinstance(v, list) or len(v) > 15 or not all(isinstance(t, str) and 1 < len(t) <= 40 for t in v):
        raise ValueError("lista de até 15 hashtags")
    return ["#" + t.strip().lstrip("#").replace(" ", "") for t in v]


RECT = {"x": _num(0, 1080, int), "y": _num(0, 1920, int), "w": _num(64, 1080, int), "h": _num(64, 1920, int),
        "fit": _enum("cover", "contain"), "visible": _bool}
TEXT = {"y": _num(0.03, 0.97), "size": _num(32, 160, int), "color": _color}
SCHEMA = {
    "templates": {t: {"cam": RECT, "game": RECT, "caption": TEXT, "hook": TEXT} for t in TEMPLATES}
    | {"background": {"blur": _num(0, 60, int), "dim": _num(0, 0.9)}},
    "shorts": {"template": _enum(*TEMPLATES), "webcam_box": _box(1920, 1080), "game_box": _box(1920, 1080),
               "trim_pauses": _bool, "renderer": _enum("hyperframes", "ffmpeg_ass")},
    "session": {"game": _text(60), "hashtags": _hashtags, "vocabulary": _words},
    "capture": {"audio_stream": _num(0, 5, int), "transcribe_stream": _stream, "mix_add_voice": _bool},
    "chat": {"enabled": _bool, "channel": _channel},
    "branding": {"intro": _video_path, "short_end": _bool, "long_start": _bool},
    "models": {"pause_at_5h_pct": _num(10, 100, int), "pause_at_7d_pct": _num(10, 100, int), "max_calls_hour": _num(0, 200, int)},
}


def _validate(patch, schema, path=""):
    out = {}
    for k, v in patch.items():
        if k not in schema:
            raise ValueError(f"campo não editável: {path}{k}")
        rule = schema[k]
        if isinstance(rule, dict) and not isinstance(v, dict):
            raise ValueError(f"{path}{k} precisa ser um objeto")
        try:
            out[k] = _validate(v, rule, f"{path}{k}.") if isinstance(rule, dict) else rule(v)
        except ValueError as e:
            raise ValueError(e if isinstance(rule, dict) else f"{path}{k}: {e}") from None
    return out


def _merge(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = copy.deepcopy(v)
    return base


def overlay() -> dict:
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def mtime():
    try:
        return PATH.stat().st_mtime
    except OSError:
        return 0


def effective(base=None) -> dict:
    """config.yaml + data/settings.json."""
    return _merge(copy.deepcopy(base) if base is not None else load_config(), overlay())


def refresh(cfg, base):
    """Re-apply the overlay into the shared cfg dict in place (the worker and the panel hold the same object)."""
    new = effective(base)
    cfg.update(new)  # replace sections one by one: readers never see a missing key (no clear())
    for k in [k for k in cfg if k not in new]:
        del cfg[k]


def save(patch) -> dict:
    """Validate and merge a partial patch into data/settings.json (atomic write). Raises ValueError with a message."""
    if not isinstance(patch, dict):
        raise ValueError("formato inválido")
    clean = _validate(patch, SCHEMA)
    data = _merge(overlay(), clean)
    PATH.parent.mkdir(exist_ok=True)
    tmp = PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, PATH)
    return clean


def reset(section):
    """Drop app overrides for one top-level section (e.g. 'templates') — back to config.yaml defaults."""
    data = overlay()
    data.pop(section, None)
    PATH.parent.mkdir(exist_ok=True)
    PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    ok = _validate({"templates": {"split": {"cam": {"x": 0, "y": 0, "w": 1080, "h": 700}, "caption": {"color": "#ffdf00"}}},
                    "chat": {"channel": "#MeuCanal_1"}, "capture": {"transcribe_stream": None}}, SCHEMA)
    assert ok["templates"]["split"]["caption"]["color"] == "#FFDF00" and ok["chat"]["channel"] == "meucanal_1"
    for bad in ({"capture": {"audio_stream": 9}}, {"shorts": {"webcam_box": [1900, 0, 100, 100]}}, {"x": 1},
                {"templates": {"split": {"cam": {"w": 5000}}}}, {"models": {"pause_at_5h_pct": 0}}):
        try:
            _validate(bad, SCHEMA)
            raise AssertionError(bad)
        except ValueError:
            pass
    assert _merge({"a": {"b": 1, "c": 2}}, {"a": {"b": 3}}) == {"a": {"b": 3, "c": 2}}
    print("ok")
