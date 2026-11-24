"""LiveForge — autonomous live-stream clip editor.

  python main.py doctor                               diagnóstico (não altera nada)
  python main.py run [--agents monitor,shorts]         sessão ao vivo (vigia a pasta de gravação do OBS)
  python main.py resume                                retoma a última sessão
  python main.py process ARQUIVO_OU_PASTA              processa gravações já existentes e sai
  python main.py status                                fila, custo e clipes prontos
"""
import argparse
import datetime
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import psutil

from liveforge import db
from liveforge.core import ROOT, duration_ms, load_config, log, setup_logging, start_ms_from_name
from liveforge.guard import Guard, obs_running

VIDEO_EXT = {".mkv", ".mp4", ".mov", ".flv"}
PID_FILE = ROOT / "data" / "liveforge.pid"
STOP_FILE = ROOT / "data" / "STOP"


# ---------------------------------------------------------------- ingest
class Ingest:
    """Registers only closed recording files: a newer file exists, or size stopped changing."""

    def __init__(self, con, folder, session, cfg):
        self.con, self.folder, self.session, self.cfg = con, Path(folder).resolve(), session, cfg
        self.sizes = {}  # path -> (size, first_seen_at_this_size)

    def scan(self, recording=None):
        c = self.cfg["capture"]
        files = sorted((f for f in self.folder.iterdir() if f.suffix.lower() in VIDEO_EXT), key=start_ms_from_name)
        known = {r[0] for r in self.con.execute("select path from segments")}
        since = (time.time() - c["lookback_hours"] * 3600) * 1000
        now = time.time()
        for i, f in enumerate(files):
            if str(f) in known or start_ms_from_name(f) < since:
                continue
            size = f.stat().st_size
            prev = self.sizes.get(f)
            if not prev or prev[0] != size:
                self.sizes[f] = (size, now)
                prev = self.sizes[f]
            newest = i == len(files) - 1
            stable_for = now - prev[1]
            closed = (not newest and stable_for >= 3) or (stable_for >= c["closed_after_seconds"] and recording is not True)
            if closed:
                self.add(f)

    def add(self, f):
        d = duration_ms(f)
        if not d:
            log.warning("ignorando %s (ffprobe não conseguiu ler)", f.name)
            known_bad = self.sizes.get(f)
            self.sizes[f] = (known_bad[0] if known_bad else -1, time.time())
            return
        st = start_ms_from_name(f)
        last = self.con.execute("select start_ms + duration_ms from segments order by start_ms desc limit 1").fetchone()
        if last and abs(st - last[0]) < 2500:  # OBS split: filename has 1 s precision; snap to continuous timeline
            st = last[0]
        self.con.execute("insert or ignore into segments(session, path, start_ms, duration_ms) values (?,?,?,?)",
                         (self.session, str(f), st, d))
        log.info("Ingestão: %s (%.0fs) na fila", f.name, d / 1000)


# ---------------------------------------------------------------- worker loop
def worker(cfg, session, agents, folder, once=False, base=None):
    """cfg = config.yaml + panel overrides (data/settings.json); base = config.yaml alone (to re-merge live)."""
    from types import SimpleNamespace

    from liveforge import chat, episodes, imports, monitor, settings, shorts
    from liveforge.llm import LLM

    base = base or load_config()
    con = db.connect()
    db.recover(con)
    guard, llm = Guard(cfg), LLM(cfg, con, session)
    guard.rec_dir = folder
    ingest = Ingest(con, folder, session, cfg) if folder else None
    ai = f"Claude Code/assinatura ({llm.m['cheap']})" if llm.enabled else "heurística local"
    log.info("Sessão %s | agentes: %s | pasta: %s | IA: %s", session, ",".join(agents), folder or "-", ai)
    ctx = SimpleNamespace(cfg=cfg, base=base, guard=guard, session=session, agents=agents, ai_mode=ai, paused={"on": False},
                          activity={"what": "iniciando", "since": time.time()}, metrics=guard.metrics,
                          mark=lambda kind: db.add_marker(session, kind))
    if not once:
        hotkeys(cfg, session, ctx.paused)
        chat.start(cfg)  # idles until a channel is connected in the panel
        if cfg["panel"].get("enabled"):
            from liveforge.panel import start_panel
            start_panel(ctx, port=cfg["panel"]["port"])
            url = f"http://127.0.0.1:{cfg['panel']['port']}/"
            log.info("Painel: %s", url)
            if cfg["panel"].get("open_browser"):
                import webbrowser
                webbrowser.open(url)

    def doing(what):
        ctx.activity = {"what": what, "since": time.time()}

    STOP_FILE.unlink(missing_ok=True)
    last_status, seen, look_at = 0, settings.mtime(), 0
    while not STOP_FILE.exists():
        if settings.mtime() != seen:  # panel saved new settings: apply without restarting
            seen = settings.mtime()
            settings.refresh(cfg, base)
            log.info("Ajustes do painel aplicados")
        cfg["session"]["current_game"] = guard.metrics().get("obs_game")  # game the OBS capture points at
        recording = guard.recording()
        if not ingest and not once and time.time() >= look_at:  # started without OBS: attach when it shows up
            look_at = time.time() + 15
            if "monitor" in agents and (folder := resolve_folder(cfg, guard)):
                guard.rec_dir, ingest = folder, Ingest(con, folder, session, cfg)
                log.info("OBS encontrado: vigiando a gravação em %s", folder)
        if ingest:
            ingest.scan(recording)
        if "episodes" in agents:
            episodes.sync(con, session)
        if "imports" in agents and not once:
            imports.scan_folder(con)  # videos dropped into LiveForge\imports
        if time.time() - last_status > 300:
            last_status = time.time()
            print_status(con, session)
        if ctx.paused["on"]:
            doing("pausado (F11 / painel)")
            time.sleep(1)
            continue
        active = not once and recording is not False  # None = OBS open but unreachable: assume still recording
        job = None
        if "monitor" in agents and (job := db.claim(con, "segments", "start_ms")):
            table, label = "segments", f"analisando segmento #{job['id']} ({Path(job['path']).name})"
            fn = lambda j: (monitor.process_segment(con, j, cfg, llm, session), db.finish(con, "segments", j["id"], "done"))
        elif "episodes" in agents and (job := db.claim(con, "episodes", "start_ms")):
            table, label = "episodes", f"montando episódio #{job['id']}"
            fn = lambda j: db.finish(con, "episodes", j["id"], "done",
                                     out_path=episodes.render_episode(con, j, cfg, guard, active, llm))
        # live clips (priority >= 0) first, then one step of an imported video, then the imported videos' clips
        elif "shorts" in agents and (job := db.claim(con, "candidates", "priority desc, score desc, id", "priority >= 0")):
            table, label = "candidates", f"editando clipe #{job['id']} (nota {job['score']}, {cfg['shorts']['renderer']})"
            fn = lambda j: db.finish(con, "candidates", j["id"], "done",
                                     out_path=shorts.render_candidate(con, j, cfg, guard, active))
        elif "imports" in agents and (job := db.claim(con, "imports", "id")):
            table, label = "imports", f"importando “{job['name']}” ({job['stage'] or 'iniciando'})"
            fn = lambda j: db.finish(con, "imports", j["id"], "done" if imports.step(con, j, cfg, guard, llm) else "pending")
        elif "shorts" in agents and (job := db.claim(con, "candidates", "priority desc, score desc, id")):
            table, label = "candidates", f"editando clipe #{job['id']} (vídeo importado, nota {job['score']})"
            fn = lambda j: db.finish(con, "candidates", j["id"], "done",
                                     out_path=shorts.render_candidate(con, j, cfg, guard, active))
        if not job:
            busy = " in ('pending','retry','deferred','running')"  # only queues an enabled agent will ever drain
            q = [f"select 1 from {t} where status{busy}" for a, t in
                 (("monitor", "segments"), ("shorts", "candidates"), ("episodes", "episodes"), ("imports", "imports"))
                 if a in agents]
            if once and not (q and con.execute(" union all ".join(q)).fetchone()):
                break
            doing("aguardando gravação" if ingest else "ocioso")
            time.sleep(2)
            continue
        wait = guard.ok_to_start()
        if wait:
            db.finish(con, table, job["id"], "deferred", wait, delay=20)
            doing(f"aguardando: {wait}")
            time.sleep(5)
            continue
        doing(label)
        try:
            fn(job)
        except shorts.Deferred as e:
            db.finish(con, table, job["id"], "deferred", str(e), delay=cfg["resources"]["resume_stable_seconds"])
        except Exception as e:  # noqa: BLE001 — job-level isolation: one bad clip never stops the worker
            log.exception("job %s#%s falhou", table, job["id"])
            db.finish(con, table, job["id"], "retry", str(e)[-500:])
    print_status(con, session)
    log.info("LiveForge encerrado (a live e o OBS não foram tocados).")


def hotkeys(cfg, session, paused):
    keys = cfg.get("hotkeys") or {}
    try:
        import keyboard
    except ImportError:
        log.warning("biblioteca 'keyboard' ausente: atalhos desativados")
        return

    def mark(kind):
        db.add_marker(session, kind)
        log.info("Marcador %s registrado", kind)

    def toggle():
        paused["on"] = not paused["on"]
        db.add_marker(session, "pause" if paused["on"] else "resume")
        log.info("Edição %s (F11)", "PAUSADA" if paused["on"] else "retomada")

    for kind, key in keys.items():
        keyboard.add_hotkey(key, toggle if kind == "pause" else (lambda k=kind: mark(k)), suppress=False)
    log.info("Atalhos: %s", ", ".join(f"{v}={k}" for k, v in keys.items()))


def print_status(con, session):
    seg = dict(con.execute("select status, count(*) from segments group by status").fetchall())
    cand = dict(con.execute("select status, count(*) from candidates where session=? group by status", (session,)).fetchall())
    eps = dict(con.execute("select status, count(*) from episodes where session=? group by status", (session,)).fetchall())
    calls = con.execute("select count(*) from llm_calls where session=?", (session,)).fetchone()[0]
    log.info("Status | segmentos %s | clipes %s | episódios %s | Claude: %d chamadas (assinatura)",
             seg or "-", cand or "-", eps or "-", calls)


# ---------------------------------------------------------------- commands
def resolve_folder(cfg, guard):
    """Recording folder: config.yaml > OBS over WebSocket > OBS's saved profile (works with OBS closed) > None."""
    d = cfg["capture"]["directory"]
    if d and d != "auto":
        return d if Path(d).is_dir() else None
    d = guard.record_directory() if obs_running() else None
    if not d:
        from liveforge.setup_checks import _get, obs_profile
        _, basic = obs_profile()
        d = _get(basic, "AdvOut", "RecFilePath") if basic is not None else None
    return d if d and Path(d).is_dir() else None


def cmd_run(cfg, agents, resume=False):
    con = db.connect()
    last = con.execute("select session from segments where session not like 'import-%' and session not like 'offline-%' "
                       "order by id desc limit 1").fetchone()
    session = last[0] if resume and last else datetime.datetime.now().strftime("%Y%m%d-%H%M")
    folder = resolve_folder(cfg, Guard(cfg, sample=False))
    if not folder:  # no OBS needed to import videos or edit clips: start anyway, attach to the live when OBS shows up
        log.warning("Pasta de gravação do OBS não encontrada: rodando sem live (importar vídeos e editar clipes funcionam). "
                    "Assim que o OBS abrir, a gravação passa a ser vigiada sozinha.")
    PID_FILE.write_text(str(os.getpid()))
    try:
        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)  # children inherit: the live keeps priority
    except (psutil.Error, AttributeError):
        pass
    try:
        worker(cfg, session, agents, folder)
    finally:
        PID_FILE.unlink(missing_ok=True)


def cmd_process(cfg, target, agents):
    target = Path(target)
    files = sorted(target.glob("*") if target.is_dir() else [target], key=start_ms_from_name)
    files = [f for f in files if f.suffix.lower() in VIDEO_EXT]
    session = "offline-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    con = db.connect()
    ing = Ingest(con, target if target.is_dir() else target.parent, session, cfg)
    for f in files:
        if not con.execute("select 1 from segments where path=?", (str(f),)).fetchone():
            ing.add(f)
    worker(cfg, session, agents, None, once=True)


def cmd_status(cfg):
    con = db.connect()
    last = con.execute("select session from segments order by id desc limit 1").fetchone()
    if last:
        print_status(con, last[0])
    for r in con.execute("select status, score, out_path, error from candidates order by id desc limit 15"):
        print(f"  {r['status']:9} {r['score']:3}  {Path(r['out_path']).name if r['out_path'] else (r['error'] or '')[:80]}")


def cmd_doctor(cfg):
    ok = True

    def check(name, cond, detail="", blocking=True):
        nonlocal ok
        print(f"  [{'OK' if cond else ('!!' if blocking else '--')}] {name}{' — ' + detail if detail else ''}")
        ok &= bool(cond) or not blocking

    print("LiveForge doctor")
    for tool in ("ffmpeg", "ffprobe", "node"):
        check(tool, shutil.which(tool), shutil.which(tool) or "não encontrado")
    from liveforge.core import video_encoder_args
    check("encoder de hardware AMD (h264_amf)", "h264_amf" in video_encoder_args(), blocking=False)
    hf = ROOT / "hyperframes" / "node_modules"
    check("HyperFrames instalado", hf.exists() and (ROOT / "liveforge" / "render_hf.py").exists(),
          "fallback FFmpeg/ASS será usado" if not hf.exists() else "", blocking=False)
    check("render FFmpeg/ASS (fallback)", (ROOT / "liveforge" / "render_ass.py").exists())
    fonts = list((ROOT / "assets" / "fonts").glob("*.ttf"))
    check("fonte das legendas", fonts, ", ".join(f.name for f in fonts) or "usará Impact do Windows", blocking=False)
    check("efeitos sonoros", len(list((ROOT / "assets" / "sfx").glob("*.wav"))) >= 3)
    model_dir = ROOT / "data" / "models"
    has_model = model_dir.exists() and any(model_dir.iterdir())
    check(f"modelo Whisper '{cfg['analysis']['whisper_model']}'", has_model,
          "" if has_model else "será baixado no primeiro uso", blocking=False)
    free = shutil.disk_usage(ROOT).free / 1e9
    check("espaço livre (LiveForge)", free >= cfg["resources"]["min_free_gb"], f"{free:.0f} GB")
    from liveforge.llm import auth_status, find_claude
    exe = find_claude(cfg["models"].get("claude_path"))
    if cfg["models"].get("backend") == "claude_code":
        check("Claude Code (CLI)", exe, exe or "não encontrado → heurística local", blocking=False)
        if exe:
            logged = auth_status(exe)
            check("Claude Code logado na assinatura", logged,
                  "" if logged else "rode login.cmd uma vez", blocking=False)
    ch = cfg["chat"]
    check("chat da Twitch como sinal", ch.get("enabled") and ch.get("channel"),
          "" if ch.get("enabled") and ch.get("channel") else "conecte o canal na aba Contas do painel", blocking=False)
    print("  (checklist completo, com passo a passo: aba Configuração do painel)")

    g = Guard(cfg, sample=False)
    running = obs_running()
    check("OBS aberto", running, blocking=False)
    folder = None
    if running and cfg["obs"]["enabled"]:
        c = g.obs()
        check("OBS WebSocket (somente leitura)", c, "" if c else "ative em Ferramentas > Configurações do servidor WebSocket")
        if c:
            folder = g.record_directory()
            def param(cat, name):
                try:
                    return c.get_profile_parameter(cat, name).parameter_value
                except Exception:  # noqa: BLE001
                    return None
            adv = param("Output", "Mode") == "Advanced"
            fmt = param("AdvOut" if adv else "SimpleOutput", "RecFormat2")
            split = param("AdvOut", "RecSplitFile") if adv else None
            split_t = param("AdvOut", "RecSplitFileTime")
            check("gravação em MKV", fmt == "mkv", f"atual: {fmt}")
            check("divisão automática da gravação", split == "true", f"RecSplitFile={split}, {split_t} min — recomendado 3 min",
                  blocking=False)
    folder = folder or (cfg["capture"]["directory"] if cfg["capture"]["directory"] != "auto" else None)
    if folder:
        rfree = shutil.disk_usage(folder).free / 1e9
        check(f"pasta de gravação {folder}", Path(folder).is_dir(), f"{rfree:.0f} GB livres (≈16 GB/h nesta qualidade)")
        check("espaço para gravar a live", rfree > 60, "mova a gravação para o disco D:" if rfree <= 60 else "", blocking=False)
        vids = sorted((f for f in Path(folder).iterdir() if f.suffix.lower() in VIDEO_EXT), key=lambda f: f.stat().st_mtime)
        if vids and cfg["shorts"]["webcam_box"]:
            x, y, w, h = cfg["shorts"]["webcam_box"]
            out = ROOT / "work" / "calibrate.jpg"
            out.parent.mkdir(exist_ok=True)
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-sseof", "-3", "-i", str(vids[-1]), "-frames:v", "1", "-vf",
                            f"drawbox={x}:{y}:{w}:{h}:color=red:t=6", str(out)], timeout=60)
            print(f"       webcam_box desenhada em {out} (confira se o retângulo vermelho cobre a webcam)")
    print("\nPRONTO para a live." if ok else "\nHá itens bloqueantes ([!!]).")
    return 0 if ok else 1


def main():
    from liveforge import settings
    setup_logging()
    cfg = settings.effective()  # config.yaml + ajustes feitos no painel
    ap = argparse.ArgumentParser(description="LiveForge")
    ap.add_argument("command", choices=["doctor", "run", "resume", "process", "import", "status"])
    ap.add_argument("target", nargs="?")
    ap.add_argument("--agents", default=",".join(cfg["session"]["agents"]))
    ap.add_argument("--dir", help="pasta de gravação (sobrepõe capture.directory)")
    ap.add_argument("--clips", type=int, default=0, help="import: quantos Shorts (0 = automático)")
    ap.add_argument("--mode", default="fit", choices=["fit", "fill", "template"], help="import: enquadramento")
    ap.add_argument("--game", default="", help="import: jogo/tema do vídeo")
    ap.add_argument("--min-score", type=int, default=70, help="import: nota mínima")
    a = ap.parse_args()
    if a.command == "import":  # vídeo longo já editado -> Shorts prontos, e sai
        if not a.target:
            sys.exit("informe o vídeo: python main.py import CAMINHO.mp4")
        from liveforge import imports
        con = db.connect()
        try:
            iid, msg = imports.create(con, a.target, {"clips": a.clips, "mode": a.mode, "game": a.game, "min_score": a.min_score})
        except ValueError as e:
            sys.exit(f"Não deu para importar: {e}")
        log.info(msg)
        return worker(cfg, f"import-{iid}", ["imports", "shorts"], None, once=True)
    if a.dir:
        cfg["capture"]["directory"] = a.dir
    agents = [x.strip() for x in a.agents.split(",") if x.strip()]
    if a.command == "doctor":
        sys.exit(cmd_doctor(cfg))
    if a.command == "status":
        return cmd_status(cfg)
    if a.command == "process":
        if not a.target:
            sys.exit("informe o arquivo ou pasta")
        return cmd_process(cfg, a.target, agents)
    cmd_run(cfg, agents, resume=a.command == "resume")


if __name__ == "__main__":
    main()
