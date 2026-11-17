"""HyperFrames render adapter: edit plan (dict) -> 1080x1920 MP4.

HyperFrames renders the picture only (video elements muted); source audio + sfx are
mixed with ffmpeg afterwards (adelay + volume + amix normalize=0 + alimiter).
"""
import json
import os
import shutil
import subprocess
import sys

HF_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hyperframes")
HF_CLI = os.path.join(HF_DIR, "node_modules", "hyperframes", "bin", "hyperframes.mjs")
WORKERS = "3"  # keep CPU free for the live stream
FLAGS = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0) if sys.platform == "win32" else 0


def _run(cmd, cwd, timeout, env=None):
    try:
        r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, creationflags=FLAGS)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"timeout after {timeout}s: {' '.join(cmd[:3])}")
    if r.returncode:
        raise RuntimeError(f"{os.path.basename(cmd[0])} failed ({r.returncode}):\n{(r.stderr or r.stdout)[-2000:]}")


def render(plan: dict, out_path: str, work_dir: str) -> None:
    node, ffmpeg = shutil.which("node"), shutil.which("ffmpeg")
    if not node or not ffmpeg:
        raise RuntimeError("node and ffmpeg must be on PATH")
    if not os.path.isfile(HF_CLI):
        raise RuntimeError(f"HyperFrames not installed; run 'npm ci' in {HF_DIR}")
    work_dir, out_path = os.path.abspath(work_dir), os.path.abspath(out_path)
    comp = os.path.join(work_dir, "hf_comp")
    silent = os.path.join(work_dir, "hf_video.mp4")
    os.makedirs(comp, exist_ok=True)
    plan_path = os.path.join(work_dir, "hf_plan.json")
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(plan, f)

    _run([node, "build.mjs", plan_path, comp], HF_DIR, 60)
    env = dict(os.environ, HYPERFRAMES_NO_TELEMETRY="1", DO_NOT_TRACK="1", HYPERFRAMES_NO_UPDATE_CHECK="1")
    _run([node, HF_CLI, "render", comp, "-o", silent, "-w", str(plan.get("workers", WORKERS)),
          "--fps", str(plan["canvas"]["fps"]), "--crf", "20", "--quiet"], HF_DIR, 1800, env)

    # audio: source + sfx, mixed in ffmpeg
    dur = plan["duration_ms"] / 1000
    sfx = [e for e in plan.get("effects", []) if e["type"] == "sfx"]
    cmd = [ffmpeg, "-y", "-v", "error", "-i", silent, "-i", plan["source"]]
    fc = ["[1:a]aresample=48000,aformat=channel_layouts=stereo[a0]"]
    for i, e in enumerate(sfx, 1):
        cmd += ["-i", e["asset"]]
        ms = int(e["start_ms"])
        fc.append(f"[{i + 1}:a]aresample=48000,aformat=channel_layouts=stereo,"
                  f"volume={e.get('gain_db', 0)}dB,adelay={ms}|{ms}[a{i}]")
    ins = "".join(f"[a{i}]" for i in range(len(sfx) + 1))
    fc.append(f"{ins}amix=inputs={len(sfx) + 1}:normalize=0:duration=first,alimiter=limit=0.95[aout]")
    cmd += ["-filter_complex", ";".join(fc), "-map", "0:v:0", "-map", "[aout]", "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-t", f"{dur:.3f}", "-movflags", "+faststart", out_path]
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    _run(cmd, work_dir, 300)


if __name__ == "__main__":
    # python -m liveforge.render_hf plan.json out.mp4
    p = json.load(open(sys.argv[1], encoding="utf-8"))
    render(p, sys.argv[2], os.path.join(os.path.dirname(os.path.abspath(sys.argv[2])), "hf_work"))
