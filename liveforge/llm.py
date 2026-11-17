"""Editorial model calls through the Claude Code CLI (`claude -p`) on the user's Claude subscription — no paid API.

Each call: no tools, no MCP, no session saved, custom system prompt, JSON-schema-validated output.
Transcripts are untrusted data; with zero tools the model can only answer JSON.
"""
import glob
import json
import os
import shutil
import subprocess
import time

from .core import LOW_PRIORITY, ROOT, log


def find_claude(explicit=None):
    """Explicit path > `claude` on PATH > newest copy bundled with the Claude desktop app."""
    if explicit and os.path.isfile(explicit):
        return explicit
    found = shutil.which("claude")
    if found:
        return found
    bundled = glob.glob(os.path.join(os.environ.get("LOCALAPPDATA", ""), "Claude-Data", "*", "claude-code", "*", "*", "claude.exe"))
    return max(bundled, key=os.path.getmtime) if bundled else None


def clean_env():
    """Never inherit a host session's Claude/Anthropic variables (e.g. an API key would bill the API)."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("CLAUDE", "ANTHROPIC"))}
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")  # optional: from `claude setup-token`, kept in .env
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return env


def auth_status(exe) -> bool:
    try:
        r = subprocess.run([exe, "auth", "status"], capture_output=True, text=True, env=clean_env(), timeout=30)
        return json.loads(r.stdout).get("loggedIn", False) or bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def plan_usage(con):
    """Latest subscription usage reported by Claude Code: {'5h': (fraction, resets_at), '7d': (...), 'status', 'ts'}."""
    r = con.execute("select ts, rl_5h, rl_5h_reset, rl_7d, rl_7d_reset, rl_status from llm_calls "
                    "where rl_5h is not null order by ts desc limit 1").fetchone()
    if not r:
        return None
    now = time.time()
    return {"ts": r[0], "status": r[5],  # a window that already reset counts as 0
            "5h": (r[1] if (r[2] or 0) > now else 0.0, r[2]), "7d": (r[3] if (r[4] or 0) > now else 0.0, r[4])}


def usage_summary(con, cfg, session):
    """Everything the panel shows about Claude usage: plan windows, our share, totals, projection."""
    now, m = time.time(), cfg["models"]
    plan = plan_usage(con)
    cols = "count(*), coalesce(sum(input_tokens),0), coalesce(sum(output_tokens),0), coalesce(sum(cache_read),0), coalesce(sum(cost_usd),0)"
    tot = lambda where, args: dict(zip(("calls", "tokens_in", "tokens_out", "cache_read", "cost_equiv_usd"),  # noqa: E731
                                       con.execute(f"select {cols} from llm_calls where {where}", args).fetchone()))
    hours = [tot("ts >= ? and ts < ?", (now - (h + 1) * 3600, now - h * 3600)) | {"hours_ago": h} for h in range(23, -1, -1)]
    # our footprint: how much the 5 h / 7 d windows move per call (median of consecutive deltas inside one window)
    seq = con.execute("select rl_5h, rl_5h_reset, rl_7d, rl_7d_reset from llm_calls where rl_5h is not null and ts > ? "
                      "order by ts", (now - 7 * 86400,)).fetchall()

    def per_call(i, j):
        d = sorted(b[i] - a[i] for a, b in zip(seq, seq[1:]) if a[j] == b[j] and b[i] >= a[i])
        return d[len(d) // 2] if len(d) >= 3 else None

    p5, p7 = per_call(0, 1), per_call(2, 3)
    last_hour = hours[-1]["calls"]
    rate = last_hour or tot("session=?", (session,))["calls"] / max(1, (now - (con.execute(
        "select min(ts) from llm_calls where session=?", (session,)).fetchone()[0] or now)) / 3600)
    cur5 = plan["5h"][0] * 100 if plan else None
    return {
        "plan": plan and {"five_hour_pct": round(plan["5h"][0] * 100, 1), "five_hour_resets_at": plan["5h"][1],
                          "seven_day_pct": round(plan["7d"][0] * 100, 1), "seven_day_resets_at": plan["7d"][1],
                          "status": plan["status"], "updated_at": plan["ts"]},
        "limits": {"pause_at_5h_pct": m["pause_at_5h_pct"], "pause_at_7d_pct": m["pause_at_7d_pct"],
                   "max_calls_hour": m["max_calls_hour"], "max_calls_session": m["max_calls_session"]},
        "session": tot("session=?", (session,)), "today": tot("ts >= ?", (now - 86400,)), "last_hour_calls": last_hour,
        "hourly": hours, "calls_per_hour": round(rate, 1),
        "per_call_pct": {"five_hour": p5 and round(p5 * 100, 3), "seven_day": p7 and round(p7 * 100, 3)},
        "projection": {
            "five_hour_pct_per_hour": p5 and round(p5 * 100 * rate, 1),
            "hours_until_pause": (round((m["pause_at_5h_pct"] - cur5) / (p5 * 100 * rate), 1)
                                  if p5 and rate and cur5 is not None and cur5 < m["pause_at_5h_pct"] else None),
            "seven_day_pct_for_24h_live": p7 and round(p7 * 100 * rate * 24, 1),
        },
        "paused": bool(plan and (plan["5h"][0] * 100 >= m["pause_at_5h_pct"] or plan["7d"][0] * 100 >= m["pause_at_7d_pct"])),
    }


class LLM:
    def __init__(self, cfg, con, session):
        self.cfg = cfg  # shared dict: panel changes apply live
        self.con, self.session = con, session
        self.exe = find_claude(self.m.get("claude_path"))
        self.enabled = bool(self.m.get("backend") == "claude_code" and self.exe and self.m["max_calls_session"] > 0)

    @property
    def m(self):
        return self.cfg["models"]

    def budget_ratio(self) -> float:
        """Fraction of the allowance used (>=1 means stop): our own call caps AND the plan's 5 h / weekly windows,
        so the live never eats the subscription the user also needs."""
        if not self.enabled:
            return 1.0
        q = "select count(*) from llm_calls where session=? and ts>=?"
        s = self.con.execute(q, (self.session, 0)).fetchone()[0]
        h = self.con.execute(q, (self.session, time.time() - 3600)).fetchone()[0]
        ratios = [s / self.m["max_calls_session"], h / max(self.m["max_calls_hour"], 1e-9)]
        u = plan_usage(self.con)
        if u:
            ratios += [u["5h"][0] * 100 / self.m["pause_at_5h_pct"], u["7d"][0] * 100 / self.m["pause_at_7d_pct"]]
            if u["status"] == "rejected" and u["5h"][0] > 0:
                ratios.append(1.0)
        return max(ratios)

    def ask_json(self, model, system, user, schema, max_tokens=2000, purpose="") -> dict | None:
        if self.budget_ratio() >= 1:
            log.warning("limite de uso do Claude atingido (chamadas ou cota da assinatura); usando heurística local")
            return None
        cmd = [self.exe, "-p", "--model", model, "--output-format", "stream-json", "--verbose",
               "--json-schema", json.dumps(schema), "--tools", "", "--system-prompt", system, "--effort", "low",
               "--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands", "--setting-sources", "project"]
        cwd = ROOT / "data"  # neutral folder: no project settings, hooks or CLAUDE.md
        try:
            r = subprocess.run(cmd, input=user, capture_output=True, text=True, encoding="utf-8", errors="replace",
                               env=clean_env(), cwd=cwd, timeout=180, creationflags=LOW_PRIORITY)
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("Claude Code indisponível: %s", e.__class__.__name__)
            return None
        d, rl = {}, {}
        for line in r.stdout.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "result":
                d = ev
            elif ev.get("type") == "rate_limit_event":
                rl = ev.get("rate_limit_info") or {}
        if not d:
            log.warning("Claude Code sem resposta (código %s)", r.returncode)
            return None
        u, win = d.get("usage") or {}, rl.get("unifiedWindows") or {}
        w5, w7 = win.get("five_hour") or {}, win.get("seven_day") or {}
        self.con.execute(
            "insert into llm_calls(session, ts, model, input_tokens, output_tokens, cache_read, cost_usd, purpose,"
            " rl_5h, rl_5h_reset, rl_7d, rl_7d_reset, rl_status) values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.session, time.time(), model, u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0),
             u.get("output_tokens", 0), u.get("cache_read_input_tokens", 0), d.get("total_cost_usd", 0), purpose,
             w5.get("utilization"), w5.get("resetsAt"), w7.get("utilization"), w7.get("resetsAt"), rl.get("status")))
        if rl.get("status") in ("allowed_warning", "rejected"):
            log.warning("Assinatura Claude: aviso de limite (%s); janela 5 h em %.0f%%", rl["status"], (w5.get("utilization") or 0) * 100)
        if d.get("is_error") or not isinstance(d.get("structured_output"), dict):
            msg = str(d.get("result") or d.get("subtype"))[:200]
            log.warning("Claude Code não respondeu o JSON: %s", msg)
            if "authenticat" in msg.lower() or "login" in msg.lower():
                log.error(r"Claude Code sem login. Rode .\scripts\login.ps1 — até lá, heurística local.")
                self.enabled = False
            return None
        return d["structured_output"]
