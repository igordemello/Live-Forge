"""Twitch chat as an anonymous, read-only signal. Only timestamps and laugh/clip flags are stored:
no usernames, no message text — so viewers' data stays out and chat can never instruct the model.
The thread follows cfg["chat"] live, so connecting/disconnecting in the panel applies immediately."""
import random
import re
import socket
import ssl
import threading
import time

from . import db
from .core import log

HOST = "irc.chat.twitch.tv"
LAUGH = re.compile(r"k{3,}|(ha){2,}|(rs){2,}|kek|lul|lmao|xd\b|😂|🤣", re.I)
CLIP = re.compile(r"\b(clip|clipa|corta|corte)\b", re.I)
state = {"connected": False, "channel": None, "messages": 0}


def _open(channel, caps=False, timeout=10):
    raw = socket.create_connection((HOST, 6697), timeout=timeout)
    s = ssl.create_default_context().wrap_socket(raw, server_hostname=HOST)
    cap = "CAP REQ :twitch.tv/commands twitch.tv/tags\r\n" if caps else ""
    s.sendall(f"{cap}NICK justinfan{random.randint(10000, 99999)}\r\nJOIN #{channel}\r\n".encode())
    return s


def test_channel(channel, wait=6) -> bool:
    """True if the channel exists: Twitch sends ROOMSTATE only for real channels."""
    try:
        s = _open(channel, caps=True)
        end, buf = time.time() + wait, b""
        while time.time() < end:
            s.settimeout(max(0.1, end - time.time()))
            data = s.recv(65536)
            if not data:
                break
            buf += data
            if b" ROOMSTATE #" in buf:
                return True
    except OSError:
        pass
    return False


def parse(line):
    """'... PRIVMSG #ch :text' -> (laugh, clipword) or None."""
    if " PRIVMSG " not in line:
        return None
    text = line.split(" PRIVMSG ", 1)[1].partition(" :")[2]
    return int(bool(LAUGH.search(text))), int(bool(CLIP.search(text)))


def start(cfg):
    threading.Thread(target=_loop, args=(cfg,), daemon=True, name="chat").start()


def _wanted(cfg):
    c = cfg.get("chat") or {}
    return (c.get("channel") or "").strip().lower().lstrip("#") if c.get("enabled") else ""


def _loop(cfg):
    backoff = 5
    while True:
        channel = _wanted(cfg)
        if not channel:
            state.update(connected=False, channel=None)
            time.sleep(3)
            continue
        try:
            s, con, buf = _open(channel), db.connect(), b""
            s.settimeout(5)  # wake up regularly to notice panel changes
            state.update(connected=True, channel=channel)
            backoff = 5
            log.info("Chat da Twitch conectado (#%s, somente leitura)", channel)
            while _wanted(cfg) == channel:
                try:
                    data = s.recv(65536)
                except socket.timeout:
                    continue
                if not data:
                    raise ConnectionError("chat fechou a conexão")
                *lines, buf = (buf + data).split(b"\r\n")
                rows = []
                for b in lines:
                    line = b.decode("utf-8", "replace")
                    if line.startswith("PING"):
                        s.sendall(b"PONG" + b[4:] + b"\r\n")
                    elif (p := parse(line)) is not None:
                        rows.append((int(time.time() * 1000), *p))
                if rows:
                    con.executemany("insert into chat values (?,?,?)", rows)
                    state["messages"] += len(rows)
            s.close()
            log.info("Chat da Twitch desconectado (#%s)", channel)
        except Exception as e:  # noqa: BLE001 — network flakiness over 24 h: reconnect with backoff
            state["connected"] = False
            log.warning("Chat desconectado (%s); nova tentativa em %ss", e.__class__.__name__, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)


if __name__ == "__main__":
    assert parse(":a!a@a.tmi.twitch.tv PRIVMSG #x :KKKKKK que isso") == (1, 0)
    assert parse(":a!a@a.tmi.twitch.tv PRIVMSG #x :clipa isso") == (0, 1)
    assert parse(":tmi.twitch.tv 001 justinfan1 :Welcome") is None
    print("ok")
