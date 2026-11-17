"""SQLite state: segments queue, words, candidates queue, markers, LLM usage."""
import sqlite3
import time

from .core import ROOT

DB_PATH = ROOT / "data" / "liveforge.db"
MAX_ATTEMPTS = 3

SCHEMA = """
create table if not exists segments(
  id integer primary key, session text, path text unique, start_ms integer, duration_ms integer,
  status text default 'pending', attempts integer default 0, next_at real default 0, error text);
create table if not exists words(
  segment_id integer, start_ms integer, end_ms integer, text text, prob real);
create index if not exists words_t on words(start_ms);
create table if not exists loud(segment_id integer, at_ms integer, db real);
create table if not exists motion(segment_id integer, at_ms integer, value real);
create index if not exists motion_t on motion(at_ms);
create table if not exists chat(at_ms integer, laugh integer, clipword integer);
create index if not exists chat_t on chat(at_ms);
create table if not exists candidates(
  id integer primary key, session text, key text unique, start_ms integer, end_ms integer, score integer,
  data text, status text default 'pending', attempts integer default 0, next_at real default 0,
  error text, out_path text, created real, priority integer default 0);
create table if not exists episodes(
  id integer primary key, session text, key text unique, start_ms integer, end_ms integer,
  data text, status text default 'pending', attempts integer default 0, next_at real default 0,
  error text, out_path text, created real);
create table if not exists markers(
  id integer primary key, session text, at_ms integer, kind text, used integer default 0);
create table if not exists llm_calls(
  id integer primary key, session text, ts real, model text, input_tokens integer, output_tokens integer,
  cache_read integer, cost_usd real, purpose text);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)  # autocommit
    con.row_factory = sqlite3.Row
    con.execute("pragma journal_mode=wal")
    con.executescript(SCHEMA)
    for table, col in (("candidates", "priority integer default 0"),  # databases created by older versions
                       ("llm_calls", "rl_5h real"), ("llm_calls", "rl_5h_reset real"), ("llm_calls", "rl_7d real"),
                       ("llm_calls", "rl_7d_reset real"), ("llm_calls", "rl_status text")):
        try:
            con.execute(f"alter table {table} add column {col}")
        except sqlite3.OperationalError:
            pass
    return con


def recover(con):
    # ponytail: single worker process, so "running" after a restart means it died mid-job; no leases/heartbeats needed.
    for t in ("segments", "candidates", "episodes"):
        con.execute(f"update {t} set status='retry' where status='running'")


def claim(con, table, order="id"):
    """Take the next runnable job (pending/retry/deferred whose time has come) and mark it running."""
    row = con.execute(
        f"select * from {table} where status in ('pending','retry','deferred') and next_at<=? order by {order} limit 1",
        (time.time(),)).fetchone()
    if row:
        con.execute(f"update {table} set status='running', attempts=attempts+1 where id=?", (row["id"],))
    return row


def finish(con, table, job_id, status="done", error=None, delay=0, **extra):
    """status: done | failed | retry (backoff) | deferred (resources/media not ready, no attempt consumed)."""
    if status == "retry":
        attempts = con.execute(f"select attempts from {table} where id=?", (job_id,)).fetchone()[0]
        if attempts >= MAX_ATTEMPTS:
            status = "failed"
        delay = delay or 60 * 2 ** attempts
    if status == "deferred":
        con.execute(f"update {table} set attempts=max(attempts-1,0) where id=?", (job_id,))
    sets = ", ".join(f"{k}=?" for k in extra)
    con.execute(f"update {table} set status=?, error=?, next_at=?{', ' + sets if sets else ''} where id=?",
                (status, error, time.time() + delay, *extra.values(), job_id))


def add_marker(session, kind, at_ms=None):
    con = connect()  # own connection: called from the hotkey thread
    con.execute("insert into markers(session, at_ms, kind) values (?,?,?)",
                (session, at_ms or int(time.time() * 1000), kind))
    con.close()
