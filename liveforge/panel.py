"""Local control panel: stdlib HTTP server on 127.0.0.1, read-mostly view of the DB + a few safe actions.
Never touches OBS; pausing only pauses the editing worker."""
import json
import mimetypes
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import db
from .core import ROOT

OUT = (ROOT / "output").resolve()
HTML = ROOT / "liveforge" / "panel.html"
LOG = ROOT / "logs" / "liveforge.log"
KINDS = ("clip", "episode_start", "episode_end", "break_start", "break_end")
FOLDERS = {"review": OUT / "review", "episodes": OUT / "episodes", "logs": ROOT / "logs"}
TABLES = {"candidates", "episodes"}
MIME = {".mp4": "video/mp4", ".jpg": "image/jpeg", ".json": "application/json", ".srt": "text/plain; charset=utf-8"}
_sidecar = {}  # (path, mtime) -> parsed json
MAX_BODY = 65536
FONTS = (ROOT / "assets" / "fonts" / "Anton-Regular.ttf", Path("C:/Windows/Fonts/impact.ttf"))


class _Err(Exception):
    def __init__(self, code, msg):
        self.code, self.msg = code, msg


def _rel(p):
    """URL under /media for a file inside output/, else None. ?v=<mtime> makes browsers reload re-rendered files."""
    try:
        q = Path(p).resolve()
        return "/media/" + quote(q.relative_to(OUT).as_posix()) + f"?v={q.stat().st_mtime_ns // 1_000_000}"
    except (ValueError, OSError, TypeError):
        return None


def _side(path, ext):
    p = Path(path).with_suffix(ext)
    return p if p.exists() else None


def _read_json(p):
    try:
        key = (str(p), p.stat().st_mtime)
        if key not in _sidecar:
            if len(_sidecar) > 400:
                _sidecar.clear()
            _sidecar[key] = json.loads(p.read_text(encoding="utf-8"))
        return _sidecar[key]
    except (OSError, ValueError):
        return None


def _item(r, table):
    try:
        d = json.loads(r["data"] or "{}")
    except ValueError:
        d = {}
    it = {k: r[k] for k in ("id", "session", "status", "error", "created", "start_ms", "attempts")}
    it["duration_s"] = round(((r["end_ms"] or 0) - (r["start_ms"] or 0)) / 1000, 1)
    it["live_time"] = time.strftime("%H:%M:%S", time.localtime(r["start_ms"] / 1000)) if r["start_ms"] else ""
    it["title"] = d.get("title")
    if table == "candidates":
        it.update(score=r["score"], priority=r["priority"], hook=d.get("hook_text"),
                  category=d.get("category"), reason=d.get("reason"), source=d.get("source"))
    if r["status"] == "done" and r["out_path"]:
        it["mp4"] = _rel(r["out_path"])
        try:
            it["size"] = os.path.getsize(r["out_path"])
        except OSError:
            it["size"] = None
        j = _side(r["out_path"], ".jpg")
        it["jpg"] = _rel(j) if j else None
        j = _side(r["out_path"], ".json")
        it["meta"] = _read_json(j) if j else None
    return it


def _tail(path, n=40, size=16384):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - size))
            return f.read().decode("utf-8", errors="replace").splitlines()[-n:]
    except OSError:
        return []


def _open_since(con, session, start, end):
    r = con.execute("select kind, at_ms from markers where session=? and kind in (?,?) order by at_ms desc, id desc limit 1",
                    (session, start, end)).fetchone()
    return r["at_ms"] if r and r["kind"] == start else None


def build_state(ctx):
    con = db.connect()
    try:
        s, now = ctx.session, time.time()

        def counts(t):  # all sessions: clips survive restarts
            return {r[0]: r[1] for r in con.execute(f"select status, count(*) from {t} where status!='deleted' group by status")}

        cols = "id,session,status,error,created,start_ms,end_ms,data,out_path,attempts"
        cands = con.execute(f"select {cols},score,priority from candidates where status!='deleted' order by id desc limit 300").fetchall()
        eps = con.execute(f"select {cols} from episodes where status!='deleted' order by id desc limit 100").fetchall()
        act = dict(getattr(ctx, "activity", None) or {})
        try:
            met = ctx.metrics() or {}
        except Exception as e:  # metrics must never break the panel
            met = {"error": str(e)}
        return {
            "now": now, "session": s, "agents": ctx.agents, "ai_mode": ctx.ai_mode, "paused": bool(ctx.paused["on"]),
            "activity": {"what": act.get("what"), "elapsed_s": int(now - act["since"]) if act.get("since") else None},
            "metrics": met,
            "queue": {"segments": counts("segments"), "candidates": counts("candidates"), "episodes": counts("episodes")},
            "claude": {"session": con.execute("select count(*) from llm_calls where session=?", (s,)).fetchone()[0],
                       "hour": con.execute("select count(*) from llm_calls where ts>?", (now - 3600,)).fetchone()[0]},
            "episode_open_ms": _open_since(con, s, "episode_start", "episode_end"),
            "break_open_ms": _open_since(con, s, "break_start", "break_end"),
            "candidates": [_item(r, "candidates") for r in cands],
            "episodes": [_item(r, "episodes") for r in eps],
            "log": _tail(LOG),
        }
    finally:
        con.close()


def _subset(d, schema):
    """The editable part of a config dict (keys named in settings.SCHEMA)."""
    return {k: _subset((d or {}).get(k) or {}, r) if isinstance(r, dict) else (d or {}).get(k) for k, r in schema.items()}


def accounts(ctx):
    from liveforge import chat
    c = ctx.cfg.get("chat") or {}
    return {"twitch": {"channel": c.get("channel") or "", "enabled": bool(c.get("enabled")),
                       "connected": bool(chat.state.get("connected")), "messages": chat.state.get("messages", 0)}}


# table, action -> (SET clause, statuses it is allowed from)
RULES = {
    ("candidates", "approve"): ("status='pending', priority=1, next_at=0, attempts=0, error=NULL",
                                ("rejected", "failed", "deferred", "pending", "retry")),
    ("candidates", "reject"): ("status='rejected', error='descartado no painel'", ("pending", "retry", "deferred", "failed")),
    ("candidates", "retry"): ("status='retry', attempts=0, next_at=0", ("failed",)),
    ("episodes", "retry"): ("status='retry', attempts=0, next_at=0", ("failed",)),
}


def make_handler(ctx, allowed):
    origins = {"http://" + h for h in allowed}

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, body, ctype, code=200):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8", code)

        def _err(self, code, msg):
            self._json({"error": msg}, code)

        def _guard(self, post):
            if self.headers.get("Host", "") not in allowed:
                self._err(403, "host não permitido")
            elif post and self.headers.get("Origin") not in (None, *origins):
                self._err(403, "origem não permitida")
            else:
                return True
            return False

        def do_GET(self):
            if not self._guard(False):
                return
            u = urlparse(self.path)
            path, qs = u.path, parse_qs(u.query)
            try:
                if path == "/":
                    self._send(HTML.read_bytes(), "text/html; charset=utf-8")
                elif path == "/api/state":
                    self._json(build_state(ctx))
                elif path == "/api/setup":
                    from liveforge import setup_checks
                    self._json({"items": setup_checks.run_checks(ctx.cfg, ctx.guard, force="force" in qs)})
                elif path == "/api/usage":
                    from liveforge.llm import usage_summary
                    con = db.connect()
                    try:
                        self._json(usage_summary(con, ctx.cfg, ctx.session))
                    finally:
                        con.close()
                elif path == "/api/settings":
                    from liveforge import settings
                    out = {"effective": _subset(ctx.cfg, settings.SCHEMA), "defaults": _subset(ctx.base, settings.SCHEMA)}
                    if "game" in qs:  # what OBS' game capture shows now (only the Sessão card asks)
                        try:
                            out["detected_game"] = (ctx.metrics() or {}).get("obs_game")
                        except Exception:  # noqa: BLE001 — display only
                            out["detected_game"] = None
                    self._json(out)
                elif path == "/api/frame.jpg":
                    from liveforge import setup_checks
                    f = setup_checks.latest_frame(force="force" in qs)
                    if f:
                        self._send(Path(f).read_bytes(), "image/jpeg")
                    else:
                        self._err(404, "sem imagem")
                elif path == "/api/font.ttf":
                    f = next((x for x in FONTS if x.is_file()), None)
                    if f:
                        self._send(f.read_bytes(), "font/ttf")
                    else:
                        self._err(404, "fonte não encontrada")
                elif path == "/api/accounts":
                    self._json(accounts(ctx))
                elif (m := re.fullmatch(r"/api/candidates/(\d+)/captions", path)):
                    from liveforge import shorts
                    con = db.connect()
                    try:
                        self._json(shorts.load_captions(con, int(m[1])))
                    finally:
                        con.close()
                elif path.startswith("/media/"):
                    self._media(unquote(path[7:]))
                else:
                    self._err(404, "não encontrado")
            except ConnectionError:
                pass  # browser aborted (seek / closed tab)
            except ValueError as e:  # backend validation messages are already pt-BR
                self._err(400, str(e))
            except Exception as e:
                try:
                    self._err(500, str(e))
                except OSError:
                    pass

        def _media(self, rel):
            try:
                p = (OUT / rel).resolve()
                p.relative_to(OUT)
                if not p.is_file():
                    raise ValueError
            except (ValueError, OSError):
                return self._err(404, "não encontrado")
            size = p.stat().st_size
            a, b, code = 0, size - 1, 200
            rg = self.headers.get("Range")
            if rg:
                m = re.fullmatch(r"bytes=(\d*)-(\d*)", rg.strip())
                if not m or not (m[1] or m[2]):
                    return self._err(416, "range inválido")
                if m[1]:
                    a = int(m[1])
                    b = min(int(m[2]), size - 1) if m[2] else size - 1
                else:
                    a = max(0, size - int(m[2]))
                if a > b or a >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                code = 206
            self.send_response(code)
            self.send_header("Content-Type", MIME.get(p.suffix.lower()) or mimetypes.guess_type(p.name)[0] or "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(b - a + 1))
            if code == 206:
                self.send_header("Content-Range", f"bytes {a}-{b}/{size}")
            self.end_headers()
            left = b - a + 1
            with open(p, "rb") as f:
                f.seek(a)
                while left > 0:
                    chunk = f.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)

        def do_POST(self):
            if not self._guard(True):
                return
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                self.close_connection = True  # body left unread
                return self._err(400, "corpo grande demais")
            body = self.rfile.read(n) if n else b""
            try:
                self._json(self._act(urlparse(self.path).path.strip("/").split("/"), body))
            except _Err as e:
                self._err(e.code, e.msg)
            except (ValueError, RuntimeError) as e:  # backend validation messages are already pt-BR
                self._err(400, str(e))
            except Exception as e:
                self._err(500, str(e))

        def _json_body(self, body):
            try:
                return json.loads(body.decode("utf-8") or "{}")
            except ValueError:
                raise _Err(400, "JSON inválido") from None

        def _save(self, patch):
            from liveforge import settings
            clean = settings.save(patch)
            settings.refresh(ctx.cfg, ctx.base)
            return clean

        def _account(self, who, act, body):
            from liveforge import chat, settings
            if (who, act) == ("twitch", "connect"):
                name = settings.SCHEMA["chat"]["channel"](self._json_body(body).get("channel"))
                if not name:
                    raise _Err(400, "Informe o nome do canal")
                if not chat.test_channel(name):
                    raise _Err(400, "Canal não encontrado na Twitch")
                self._save({"chat": {"enabled": True, "channel": name}})
                return {"message": "Twitch conectada: " + name}
            if (who, act) == ("twitch", "disconnect"):
                self._save({"chat": {"enabled": False}})
                return {"message": "Twitch desconectada"}
            raise _Err(404, "não encontrado")

        def _act(self, p, body=b""):
            if len(p) < 2 or p[0] != "api":
                raise _Err(404, "não encontrado")
            if p[1] == "setup" and len(p) == 4 and p[2] == "action":
                from liveforge import settings, setup_checks
                msg = setup_checks.action(p[3], ctx.cfg, ctx.guard)
                settings.refresh(ctx.cfg, ctx.base)  # webcam_from_obs writes settings.json itself
                return {"message": msg}
            if p[1] == "settings" and len(p) == 2:
                return {"message": "Salvo", "saved": self._save(self._json_body(body))}
            if p[1] == "settings" and len(p) == 4 and p[2:] == ["reset", "templates"]:
                from liveforge import settings
                settings.reset("templates")
                settings.refresh(ctx.cfg, ctx.base)
                return {"message": "Modelos restaurados"}
            if p[1] == "accounts" and len(p) == 4:
                return self._account(p[2], p[3], body)
            if p[1] in ("pause", "resume") and len(p) == 2:
                ctx.paused["on"] = p[1] == "pause"
                return {"message": "Edição pausada" if ctx.paused["on"] else "Edição retomada"}
            if p[1] == "mark" and len(p) == 3 and p[2] in KINDS:
                ctx.mark(p[2])
                return {"message": "Marcador registrado"}
            if p[1] == "open" and len(p) == 3 and p[2] in FOLDERS:
                FOLDERS[p[2]].mkdir(parents=True, exist_ok=True)
                os.startfile(FOLDERS[p[2]])
                return {"message": "Pasta aberta"}
            if len(p) == 4 and p[1] in TABLES and p[2].isdigit():
                t, i, a = p[1], int(p[2]), p[3]
                if a == "delete":
                    from liveforge import shorts
                    con = db.connect()
                    try:
                        return {"message": shorts.delete_clip(con, t, i)}
                    except ValueError as e:
                        raise _Err(409, str(e)) from None
                    finally:
                        con.close()
                if a == "captions" and t == "candidates":
                    from liveforge import shorts
                    d = self._json_body(body)
                    if not isinstance(d, dict):
                        raise _Err(400, "JSON inválido")
                    con = db.connect()
                    try:
                        return {"message": shorts.edit_captions(con, i, d.get("groups"), d.get("hook_text") or "")}
                    finally:
                        con.close()
                if (t, a) not in RULES:
                    raise _Err(404, "ação desconhecida")
                sets, frm = RULES[(t, a)]
                con = db.connect()
                try:
                    cur = con.execute(f"update {t} set {sets} where id=? and status in ({','.join('?' * len(frm))})", (i, *frm))
                finally:
                    con.close()
                if not cur.rowcount:
                    raise _Err(409, "status atual não permite esta ação")
                return {"message": "Ok"}
            raise _Err(404, "não encontrado")

    return H


class _Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # Browsers drop connections all the time (video seeks, tab closes): not worth a traceback in the live log.
        import sys
        if not isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            super().handle_error(request, client_address)


def start_panel(ctx, host="127.0.0.1", port=8765):
    allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
    srv = _Server((host, port), make_handler(ctx, allowed))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="panel", daemon=True).start()
    return srv  # callers may ignore it; tests use it to shut down
