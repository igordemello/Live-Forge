"""Live-first resource guard. One sampler thread owns all OBS (read-only), CPU/RAM/disk and GPU sampling;
everyone else (worker, panel) reads its snapshot. Never changes OBS."""
import json
import os
import shutil
import threading
import time
from collections import deque
from pathlib import Path

import psutil

from .core import ROOT, log

OBS_CFG = Path(os.environ.get("APPDATA", "")) / "obs-studio" / "plugin_config" / "obs-websocket" / "config.json"


def game_from_window(win):
    """OBS capture target 'SONIC PICO PARK [Streaming]:SDL_app:streaming_client.exe' -> 'Sonic Pico Park'."""
    import re
    title = (win or "").split(":")[0].replace("#3A", ":")
    title = re.sub(r"\s*[\[(].*?[\])]\s*", " ", title).strip(" -–|")
    title = re.sub(r"\s+", " ", title)
    return (title.title() if title.isupper() else title) or None


def obs_running() -> bool:
    return any((p.info["name"] or "").lower().startswith("obs64") for p in psutil.process_iter(["name"]))


class Guard:
    def __init__(self, cfg, sample=True):
        self.cfg, self.r = cfg, cfg["resources"]
        self.client = None
        self.retry_at = 0           # next OBS WebSocket connection attempt
        self.obs_lock = threading.Lock()
        self.drops = deque()        # (time, total dropped frames) over the last ~10 s
        self.trouble_until = 0      # no heavy work before this time
        self.reason = None
        self.hot = 0                # consecutive very-high CPU samples (no-OBS mode)
        self.rec_dir = None
        self.game, self.game_at = None, 0  # window targeted by the OBS game/window capture in the live scene
        self.snap = {}
        self.gpu = None
        if sample:
            try:
                from .gpu import GpuSampler
                self.gpu = GpuSampler()
            except Exception as e:  # noqa: BLE001 — GPU metrics are optional
                log.debug("métricas de GPU indisponíveis: %s", e)
            psutil.cpu_percent(None)
            self._sample()
            threading.Thread(target=self._loop, daemon=True, name="guard-sampler").start()

    # --- OBS (read-only) -------------------------------------------------------
    def _obs_call(self, name, *args):
        if not self.cfg["obs"]["enabled"]:
            return None
        with self.obs_lock:
            if self.client is None and time.time() >= self.retry_at:
                self.retry_at = time.time() + 15  # don't hammer a closed port
                try:
                    import obsws_python as obsws
                    self.client = obsws.ReqClient(host=self.cfg["obs"]["host"], port=self.cfg["obs"]["port"],
                                                  password=self._password(), timeout=3)
                except Exception:  # noqa: BLE001 — library raises assorted socket/auth errors
                    return None
            if self.client is None:
                return None
            try:
                return getattr(self.client, name)(*args)
            except Exception as e:  # noqa: BLE001
                if type(e).__name__ != "OBSSDKRequestError":  # a refused request keeps the connection; anything else drops it
                    self.client = None
                return None

    def _password(self):
        pw = os.environ.get("OBS_PASSWORD")
        if not pw and OBS_CFG.exists():  # local file, local socket; never logged
            pw = json.loads(OBS_CFG.read_text(encoding="utf-8")).get("server_password")
        return pw or ""

    def audio_channels(self, seconds=1.5):
        """{input name: audio channel count} from OBS's live volume meters (0 = the source makes no sound).
        Uses its own short-lived event connection; None if OBS can't be reached."""
        try:
            import obsws_python as obsws
            seen = {}

            def on_input_volume_meters(data):
                for i in data.inputs:
                    seen[i["inputName"]] = max(seen.get(i["inputName"], 0), len(i.get("inputLevelsMul") or []))

            cl = obsws.EventClient(host=self.cfg["obs"]["host"], port=self.cfg["obs"]["port"], password=self._password(),
                                   subs=obsws.Subs.INPUTVOLUMEMETERS, timeout=3)
            cl.callback.register(on_input_volume_meters)
            time.sleep(seconds)
            cl.disconnect()
            return seen or None
        except Exception:  # noqa: BLE001 — optional precision; callers fall back to settings-based guesses
            return None

    def obs(self):
        """Connected client or None (used by doctor for read-only profile queries)."""
        self._obs_call("get_version")
        return self.client

    def record_directory(self):
        r = self._obs_call("get_record_directory")
        return r.record_directory if r else None

    # --- sampling ---------------------------------------------------------------
    def _loop(self):
        while True:
            time.sleep(2)
            try:
                self._sample()
            except Exception as e:  # noqa: BLE001 — sampler must never die
                log.debug("falha ao coletar métricas: %s", e)

    def _sample(self):
        now = time.time()
        vm = psutil.virtual_memory()
        s = {"cpu_pct": psutil.cpu_percent(None), "ram_pct": vm.percent, "ram_free_gb": round(vm.available / 1e9, 1),
             "disk_free_gb": round(shutil.disk_usage(ROOT).free / 1e9, 1), "rec_dir": self.rec_dir,
             "rec_disk_free_gb": round(shutil.disk_usage(self.rec_dir).free / 1e9, 1) if self.rec_dir else None,
             "obs_running": obs_running(), "obs_connected": False, "recording": None, "streaming": None,
             "dropped_total": None, "dropped_recent": None, "obs_fps": None,
             "gpu_pct": None, "encode_pct": None, "decode_pct": None, "vram_used_gb": None}
        g = self.gpu.sample() if self.gpu else None
        if g:
            s.update({k: round(v, 1) for k, v in g.items()})
        if s["obs_running"]:
            st, ss, rs = self._obs_call("get_stats"), self._obs_call("get_stream_status"), self._obs_call("get_record_status")
            if st:
                total = st.render_skipped_frames + st.output_skipped_frames + (ss.output_skipped_frames if ss else 0)
                self.drops.append((now, total))
                while self.drops and now - self.drops[0][0] > 10:
                    self.drops.popleft()
                s.update(obs_connected=True, dropped_total=total, dropped_recent=max(0, total - self.drops[0][1]),
                         obs_fps=round(st.active_fps, 1), streaming=ss.output_active if ss else None,
                         recording=rs.output_active if rs else None)
                if now - self.game_at > 15:
                    self.game_at, self.game = now, self._detect_game()
        else:
            s.update(recording=False, streaming=False)
        s["obs_game"] = self.game
        wait = max(0, self.trouble_until - now)
        s.update(guard_wait_s=int(wait), guard_reason=self.reason if wait else None)
        self.snap = s  # atomic swap; readers get a consistent dict

    def _detect_game(self):
        scene = self._obs_call("get_current_program_scene")
        items = self._obs_call("get_scene_item_list", scene.scene_name) if scene else None
        for it in reversed(items.scene_items if items else []):  # top-most visible capture wins
            if it.get("sceneItemEnabled") and it.get("inputKind") in ("game_capture", "window_capture"):
                st = self._obs_call("get_input_settings", it["sourceName"])
                name = game_from_window((st.input_settings if st else {}).get("window"))
                if name:
                    return name
        return None

    def metrics(self) -> dict:
        return dict(self.snap)

    def recording(self):
        """True/False, or None when OBS is open but its state is unknown (WebSocket unreachable)."""
        s = self.snap
        return s.get("recording") if (s.get("obs_connected") or not s.get("obs_running")) else None

    # --- decisions --------------------------------------------------------------
    def trouble(self) -> str | None:
        """Polled during heavy work. Returns a reason to abort, else None."""
        s = self.snap
        if s.get("obs_running") and self.cfg["obs"]["enabled"]:
            if not s.get("obs_connected"):
                return "métricas do OBS indisponíveis" if self.r["defer_on_missing_metrics"] else None
            if (s.get("dropped_recent") or 0) > self.r["max_new_dropped_frames"]:
                return f"OBS perdendo frames (+{s['dropped_recent']} em 10 s)"
            return None
        self.hot = self.hot + 1 if (s.get("cpu_pct") or 0) >= self.r["kill_cpu_pct"] else 0
        return f"CPU {s['cpu_pct']:.0f}% sustentada" if self.hot >= 5 else None

    def ok_to_start(self) -> str | None:
        """Returns a reason to wait, or None when heavy work may start."""
        if time.time() < self.trouble_until:
            return self.reason or "aguardando estabilidade"
        s = self.snap
        if s.get("disk_free_gb", 1e9) < self.r["min_free_gb"]:
            return f"pouco espaço em disco ({s['disk_free_gb']:.0f} GB)"
        if s.get("ram_free_gb", 1e9) < self.r["min_free_ram_gb"]:
            return "pouca RAM livre"
        if (s.get("cpu_pct") or 0) > self.r["max_cpu_pct"]:
            return f"CPU em {s['cpu_pct']:.0f}%"
        # ponytail: encoder load is shown but not gated — AMD's own background service keeps it at 60-70 % here;
        # OBS dropped frames (trouble()) already catch a saturated encoder.
        if (s.get("gpu_pct") or 0) > self.r["max_gpu_pct"]:
            return f"GPU em {s['gpu_pct']:.0f}%"
        reason = self.trouble()
        if reason:
            self.backoff(reason)
        return reason

    def backoff(self, reason):
        log.warning("Proteção da live: %s — trabalho pesado adiado %ss", reason, self.r["resume_stable_seconds"])
        self.reason = reason
        self.trouble_until = time.time() + self.r["resume_stable_seconds"]


def kill_children():
    """Abort our own heavy subprocesses (ffmpeg/node/chrome/claude). Never touches OBS or other apps."""
    for p in psutil.Process().children(recursive=True):
        try:
            p.kill()
        except psutil.Error:
            pass
