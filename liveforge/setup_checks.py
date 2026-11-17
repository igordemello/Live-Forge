"""Setup checklist for the panel: detects what is configured outside LiveForge (OBS, Claude, tools, accounts)
and explains how to fix what is missing. Generic: works on any Windows PC, nothing user-specific hardcoded."""
import configparser
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from .core import ROOT, log

APPDATA = Path(os.environ.get("APPDATA", ""))
OBS = APPDATA / "obs-studio"
_cache = {"at": 0, "items": None}


def _ini(path):
    p = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        p.read(path, encoding="utf-8-sig")
    except (configparser.Error, OSError):
        pass
    return p


def _get(ini, section, key, default=None):
    v = ini.get(section, key, fallback=default)
    return v.replace("\\\\", "\\") if isinstance(v, str) else v


def obs_profile():
    """(global/user ini, profile basic.ini) — OBS 31 keeps app settings in user.ini, older versions in global.ini."""
    gl = _ini(OBS / "user.ini") if (OBS / "user.ini").exists() else _ini(OBS / "global.ini")
    prof = _get(gl, "Basic", "ProfileDir")
    return gl, _ini(OBS / "basic" / "profiles" / prof / "basic.ini") if prof else None


def _item(id_, group, title, ok, detail="", how=(), action=None, optional=False):
    status = "ok" if ok else ("optional" if optional else "todo")
    return {"id": id_, "group": group, "title": title, "status": status, "detail": detail,
            "how": list(how) if not ok else [], "action": action if not ok else None}


def webcam_from_obs(guard):
    """[x,y,w,h] of the one visible video-capture source smaller than the canvas in the current OBS scene, or None."""
    scene = guard._obs_call("get_current_program_scene")
    video = guard._obs_call("get_video_settings")
    items = guard._obs_call("get_scene_item_list", scene.scene_name) if scene else None
    if not items or not video:
        return None
    found = []
    for it in items.scene_items:
        if it.get("inputKind") != "dshow_input" or not it.get("sceneItemEnabled"):
            continue
        t = it.get("sceneItemTransform") or {}
        w, h = t.get("width", 0), t.get("height", 0)
        if 32 < w < video.base_width * 0.7:
            x, y = max(0, round(t["positionX"])), max(0, round(t["positionY"]))
            found.append([x, y, min(round(w), video.base_width - x), min(round(h), video.base_height - y)])
    return found[0] if len(found) == 1 else None


MIC_KINDS = {"wasapi_input_capture", "coreaudio_input_capture", "pulse_input_capture"}
SOUND_KINDS = {"wasapi_output_capture", "wasapi_process_output_capture", "dshow_input", "ffmpeg_source", "vlc_source",
               "game_capture", "browser_source", "coreaudio_output_capture", "pulse_output_capture"}


def _has_sound(kind, settings, channels=None):
    """channels = live audio channel count from OBS's volume meters (None when OBS isn't reachable)."""
    if channels:
        return True
    if kind == "game_capture":
        return bool(settings.get("capture_audio"))
    if kind == "browser_source":
        return bool(settings.get("reroute_audio"))  # otherwise its sound already goes through Desktop Audio
    if kind == "dshow_input" and channels is not None:
        # measured silent (e.g. a webcam without its own audio); a capture card with an explicit audio device still
        # counts, even if its scene is hidden right now
        return bool(settings.get("use_custom_audio_device"))
    return kind in SOUND_KINDS


def audio_routing(guard, gl, basic):
    """Which recorded track has ONLY the microphone (voice) and which has everything (mix), as MKV stream indexes."""
    mask = int(_get(basic, "AdvOut", "RecTracks", "1") or 1) if basic is not None else 1
    rec = [t for t in range(1, 7) if mask & (1 << (t - 1))]
    srcs = []  # (name, kind, tracks, muted, settings)
    meters = None
    if guard and guard.snap.get("obs_connected"):  # live truth from OBS
        meters = guard.audio_channels()
        lst = guard._obs_call("get_input_list")
        for i in (lst.inputs if lst else []):
            name, kind = i["inputName"], i.get("unversionedInputKind") or i.get("inputKind")
            tr = guard._obs_call("get_input_audio_tracks", name)
            if tr is None:
                continue  # input without audio
            st = guard._obs_call("get_input_settings", name)
            mu = guard._obs_call("get_input_mute", name)
            srcs.append((name, kind, {int(k) for k, v in tr.input_audio_tracks.items() if v}, bool(mu and mu.input_muted),
                         st.input_settings if st else {}))
    else:  # OBS closed: the saved scene collection
        coll = _ini_json(OBS / "basic" / "scenes" / f"{_get(gl, 'Basic', 'SceneCollectionFile', '')}.json")
        for s in coll.get("sources", []) + [v for k, v in coll.items() if isinstance(v, dict) and k.endswith(tuple("123456"))
                                             and k.startswith(("DesktopAudioDevice", "AuxAudioDevice"))]:
            m = int(s.get("mixers", 0))
            srcs.append((s.get("name"), s.get("id"), {t for t in range(1, 7) if m & (1 << (t - 1))}, bool(s.get("muted")),
                         s.get("settings") or {}))
    mics = [s for s in srcs if s[1] in MIC_KINDS and not s[3] and s[2]]
    others = [s for s in srcs if s[1] not in MIC_KINDS and not s[3] and s[2]
              and _has_sound(s[1], s[4], None if meters is None else meters.get(s[0], 0))]
    voice = next((t for t in rec if any(t in m[2] for m in mics) and not any(t in o[2] for o in others)), None)
    mix = next((t for t in rec if all(t in x[2] for x in mics + others)), rec[0] if rec else None)
    idx = {t: i for i, t in enumerate(rec)}
    return {"rec_tracks": rec, "mics": [m[0] for m in mics], "voice_track": voice, "mix_track": mix,
            "voice_stream": idx.get(voice), "mix_stream": idx.get(mix),
            "others_with_mic": sorted({o[0] for o in others for m in mics if o[2] & m[2] - ({mix} if mix else set())})}


def latest_frame(force=False):
    """A 1920x1080 JPG from the newest recording (for the template editor). Cached 60 s. None if nothing recorded."""
    from . import db
    out = ROOT / "work" / "frame.jpg"
    if not force and out.exists() and time.time() - out.stat().st_mtime < 60:
        return out
    con = db.connect()
    row = con.execute("select path, duration_ms from segments order by start_ms desc limit 1").fetchone()
    con.close()
    tries = [(row[0], row[1] / 2000)] if row and Path(row[0]).exists() else []
    _, basic = obs_profile()
    rec = basic is not None and _get(basic, "AdvOut", "RecFilePath")
    if rec and Path(rec).is_dir():
        vids = sorted((f for f in Path(rec).iterdir() if f.suffix.lower() in (".mkv", ".mp4", ".mov")),
                      key=lambda f: f.stat().st_mtime, reverse=True)
        tries += [(str(f), 1.0) for f in vids[:2]]
    out.parent.mkdir(exist_ok=True)
    for path, ss in tries:
        r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{ss:.2f}", "-i", path, "-frames:v", "1",
                            "-vf", "scale=1920:1080", "-q:v", "3", str(out)], capture_output=True, timeout=30)
        if r.returncode == 0 and out.exists():
            return out
    return None


def gb_per_hour(cfg):
    """Measured from the last ~hour of recordings; 16 GB/h (1080p60, CQP 18) until there are 30+ min of them —
    short tests of a still desktop compress far better than gameplay and would understate it."""
    from . import db
    con = db.connect()
    rows = con.execute("select path, duration_ms from segments order by id desc limit 20").fetchall()
    con.close()
    size = sum(Path(p).stat().st_size for p, _ in rows if Path(p).exists())
    dur = sum(d for p, d in rows if Path(p).exists())
    return (size / 1e9) / (dur / 3.6e6) if dur >= 30 * 60_000 else 16.0


def run_checks(cfg, guard, force=False):
    if not force and _cache["items"] and time.time() - _cache["at"] < 15:
        return _cache["items"]
    items = []
    G = "Programas"
    for exe, name, how in (("ffmpeg", "FFmpeg", "winget install Gyan.FFmpeg"), ("node", "Node.js 22+", "winget install OpenJS.NodeJS.LTS")):
        p = shutil.which(exe)
        items.append(_item(exe, G, name, p, p or "não encontrado",
                           [f"Abra o Prompt de Comando e rode: {how}", "Feche e abra o terminal, depois rode setup.cmd na pasta LiveForge."]))
    hf = ROOT / "hyperframes" / "node_modules" / "hyperframes"
    items.append(_item("hyperframes", G, "HyperFrames (motor de edição)", hf.exists(), "",
                       ["Rode setup.cmd na pasta LiveForge (instala tudo dentro da pasta, nada global)."]))
    chrome = Path.home() / ".cache" / "hyperframes"
    items.append(_item("hf_browser", G, "Navegador do HyperFrames", chrome.exists() and any(chrome.iterdir()), "",
                       ["Rode setup.cmd (baixa o navegador que o HyperFrames usa para renderizar)."]))
    models = ROOT / "data" / "models"
    items.append(_item("whisper", G, "Modelo de transcrição (Whisper)", models.exists() and any(models.iterdir()),
                       "", ["É baixado sozinho na primeira análise (~460 MB). Precisa de internet uma vez."], optional=True))

    G = "Claude (sua assinatura)"
    from .llm import auth_status, find_claude, plan_usage
    exe = find_claude(cfg["models"].get("claude_path"))
    items.append(_item("claude_cli", G, "Claude Code instalado", exe, exe or "",
                       ["Instale o app Claude para desktop (claude.ai/download) e entre com sua conta Pro/Max.",
                        "O LiveForge usa o Claude Code que vem junto com o app — sem chave de API e sem custo extra."]))
    if exe:
        logged = auth_status(exe)
        items.append(_item("claude_login", G, "Claude Code logado na assinatura", logged, "",
                           ["Clique em “Fazer login” (ou dê dois cliques em login.cmd) e autorize no navegador."],
                           action={"id": "claude_login", "label": "Fazer login"}))
    from . import db
    con = db.connect()
    u = plan_usage(con)
    con.close()
    if u:
        p5, lim = u["5h"][0] * 100, cfg["models"]["pause_at_5h_pct"]
        items.append(_item("claude_quota", G, "Cota da assinatura com folga", p5 < lim,
                           f"janela de 5 h em {p5:.0f}% (pausa automática em {lim}%), semanal em {u['7d'][0] * 100:.0f}%",
                           ["Espere a janela de 5 h reiniciar (veja a aba Uso do Claude) — enquanto isso os clipes usam a heurística local."]))

    G = "OBS"
    ws = _ini_json(OBS / "plugin_config" / "obs-websocket" / "config.json")
    gl, basic = obs_profile()
    items.append(_item("obs_installed", G, "OBS Studio instalado e aberto ao menos uma vez", OBS.exists() and basic is not None,
                       "", ["Instale o OBS Studio 30 ou mais novo (obsproject.com), abra uma vez e configure sua cena."]))
    items.append(_item("obs_ws", G, "Servidor WebSocket ativado", ws.get("server_enabled") is True,
                       f"porta {ws.get('server_port', 4455)}",
                       ["No OBS: Ferramentas → Configurações do servidor WebSocket.", "Marque “Ativar servidor WebSocket” e clique em OK.",
                        "Deixe a autenticação ligada: o LiveForge lê a senha sozinho (só leitura, só neste PC)."]))
    snap = guard.metrics() if guard else {}
    if snap.get("obs_running"):
        items.append(_item("obs_conn", G, "LiveForge conectado ao OBS", snap.get("obs_connected"), "",
                           ["Confira o item acima e reinicie o OBS. Se mudou a porta, ajuste obs.port no config.yaml."]))
    if basic is not None:
        adv = _get(basic, "Output", "Mode") == "Advanced"
        items.append(_item("obs_adv", G, "Saída no modo Avançado", adv, "",
                           ["Configurações → Saída → no topo, “Modo de saída”: Avançado."]))
        fmt = _get(basic, "AdvOut", "RecFormat2", "")
        items.append(_item("obs_mkv", G, "Gravação em MKV", fmt == "mkv", f"atual: {fmt or '?'}",
                           ["Configurações → Saída → aba Gravação → Formato de gravação: mkv."]))
        split, stime = _get(basic, "AdvOut", "RecSplitFile") == "true", int(_get(basic, "AdvOut", "RecSplitFileTime", "0") or 0)
        stype = _get(basic, "AdvOut", "RecSplitFileType", "Time")
        items.append(_item("obs_split", G, "Divisão automática da gravação (2–5 min)", split and stype == "Time" and 2 <= stime <= 5,
                           f"{'ligada' if split else 'desligada'}, {stime} min",
                           ["Configurações → Saída → aba Gravação → marque “Divisão automática de arquivo”.",
                            "Escolha “Dividir por tempo” e 3 minutos. É isso que permite cortar clipes durante a live."]))
        rec = _get(basic, "AdvOut", "RecFilePath", "")
        if rec and Path(rec).is_dir():
            free, need = shutil.disk_usage(rec).free / 1e9, gb_per_hour(cfg) * cfg["session"].get("planned_hours", 4)
            items.append(_item("obs_disk", G, f"Espaço para {cfg['session'].get('planned_hours', 4)} h de gravação", free > need * 1.2,
                               f"{rec} — {free:.0f} GB livres, precisa de ~{need:.0f} GB",
                               ["Escolha uma pasta num disco com mais espaço (Configurações → Saída → Gravação → Caminho),",
                                "ou reduza a qualidade (ex.: CQP 22 em vez de 18 ≈ metade do tamanho), ou libere espaço."]))
        else:
            items.append(_item("obs_disk", G, "Pasta de gravação existe", False, rec or "não definida",
                               ["Configurações → Saída → Gravação → Caminho da gravação: escolha uma pasta existente."]))
        auto = _get(gl, "BasicWindow", "RecordWhenStreaming") == "true"
        items.append(_item("obs_autorec", G, "Gravar automaticamente ao transmitir", auto, "",
                           ["Configurações → Geral → seção Saída → marque “Gravar automaticamente quando estiver transmitindo”."]))
        r = audio_routing(guard, gl, basic)
        trk = lambda ts: ", ".join(map(str, ts)) or "nenhuma"  # noqa: E731
        items.append(_item(
            "audio_split", "Áudio", "Microfone numa trilha só dele (voz separada do jogo)", r["voice_track"] is not None,
            f"gravando trilhas {trk(r['rec_tracks'])}; microfone: {', '.join(r['mics']) or 'não encontrado'}"
            + (f"; dividem a trilha da voz: {', '.join(r['others_with_mic'])}" if r["others_with_mic"] else ""),
            ["Por que: com tudo numa trilha só, a transcrição mistura sua fala com os diálogos do jogo e os amigos — "
             "e a detecção de momentos erra junto.",
             "No OBS: menu Editar → Propriedades avançadas de áudio.",
             "Na linha do seu microfone (Mic/Aux): marque as trilhas 1 e 2.",
             "Em TODAS as outras fontes com som (Áudio do desktop, Captura de jogo, placa de captura, webcam com "
             "microfone, alertas, Discord): deixe só a trilha 1 (desmarque a 2).",
             "Configurações → Saída → aba Gravação → “Trilha de áudio”: marque 1 e 2.",
             "Grave uns 10 s de teste e clique em “Verificar de novo”, depois em “Usar trilhas detectadas”.",
             "Os clipes continuam com o som completo (trilha 1); só a legenda e a detecção usam a trilha da voz. "
             "Vozes de amigos no Discord deixam de ser legendadas."]))
        if r["voice_track"] is not None:
            cap = cfg["capture"]
            using = cap.get("transcribe_stream") == r["voice_stream"] and cap.get("audio_stream") == r["mix_stream"]
            last = _last_audio_streams()
            items.append(_item(
                "audio_use", "Áudio", "LiveForge usando a trilha da sua voz", using,
                f"voz = trilha {r['voice_track']}, som completo = trilha {r['mix_track']}"
                + (f" · a última gravação tem {last} trilha(s) de áudio" if last is not None else ""),
                ["Clique em “Usar trilhas detectadas”."], action={"id": "audio_tracks", "label": "Usar trilhas detectadas"}))

    from .core import game_name
    auto = (cfg["session"].get("game") or "auto") == "auto"
    g = game_name(cfg) or (snap.get("obs_game") if auto else None)
    items.append(_item("game", "Áudio", "Jogo da live identificado", bool(g),
                       f"{g} ({'detectado na Captura de jogo do OBS' if auto else 'definido por você'})" if g else
                       "nenhuma Captura de jogo/janela ativa na cena do OBS",
                       ["Na fonte “Captura de jogo” do OBS, escolha a janela do jogo — o LiveForge lê o nome dela sozinho.",
                        "Jogando no console (placa de captura)? Escreva o nome do jogo no campo “Jogo da live” acima."]))
    if snap.get("obs_connected"):
        box, cur = webcam_from_obs(guard), cfg["shorts"].get("webcam_box")
        if box:
            same = cur and all(abs(a - b) <= 8 for a, b in zip(box, cur))
            items.append(_item("webcam", G, "Posição da webcam igual à do OBS", same, f"OBS: {box} · LiveForge: {cur}",
                               ["Clique em “Usar posição do OBS” (ou ajuste na aba Modelos)."],
                               action={"id": "webcam_from_obs", "label": "Usar posição do OBS"}))

    G = "Contas e extras (opcional)"
    ch = cfg.get("chat", {})
    items.append(_item("twitch", G, "Chat da Twitch como sinal", ch.get("enabled") and ch.get("channel"),
                       f"#{ch.get('channel')}" if ch.get("channel") else "", ["Aba Contas → informe o nome do canal e clique em Conectar."],
                       optional=True))
    if snap.get("encode_pct") and snap["encode_pct"] > 50 and not snap.get("recording"):
        items.append(_item("amd_replay", G, "Encoder de vídeo livre", False, f"encoder em {snap['encode_pct']:.0f}% sem o OBS gravar",
                           ["Algum programa está usando o encoder da placa (ex.: AMD Instant Replay ou NVIDIA Instant Replay).",
                            "Se não usa, desligue: AMD Software → Gravar e transmitir → Instant Replay."], optional=True))
    _cache.update(at=time.time(), items=items)
    return items


def _last_audio_streams():
    """Audio track count in the newest ingested recording (proves the OBS change reached the files)."""
    from . import db
    from .core import probe
    con = db.connect()
    row = con.execute("select path from segments order by start_ms desc limit 1").fetchone()
    con.close()
    p = probe(row[0]) if row and Path(row[0]).exists() else None
    return sum(s["codec_type"] == "audio" for s in p["streams"]) if p else None


def _ini_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def action(name, cfg, guard):
    """Run a checklist action; returns a message for the panel."""
    if name == "claude_login":
        os.startfile(ROOT / "login.cmd")  # opens its own console window; the user authorizes in the browser
        _cache["items"] = None
        return "Abri a janela de login do Claude Code: autorize no navegador e volte aqui."
    if name == "audio_tracks":
        gl, basic = obs_profile()
        r = audio_routing(guard, gl, basic)
        if r["voice_stream"] is None:
            return "Ainda não há uma trilha só com o microfone — siga o passo a passo do item de áudio."
        from .settings import save
        save({"capture": {"audio_stream": r["mix_stream"] or 0, "transcribe_stream": r["voice_stream"]}})
        _cache["items"] = None
        return f"Pronto: legendas e detecção pela trilha {r['voice_track']} (sua voz); clipes com a trilha {r['mix_track']}."
    if name == "webcam_from_obs":
        box = webcam_from_obs(guard)
        if not box:
            return "Não achei exatamente uma webcam visível na cena atual do OBS."
        from .settings import save
        save({"shorts": {"webcam_box": box}})
        _cache["items"] = None
        log.info("webcam_box atualizado a partir do OBS: %s", box)
        return f"Posição da webcam salva: {box}"
    raise ValueError("ação desconhecida")
