"""Scribe's terminal recorder: a quiet screen where the transcript surfaces and fades.

Attaches to a recorder that is already running on the port (e.g. `scribe.py serve`),
otherwise starts one in-process.
"""
import json
import os
import select
import sys
import termios
import textwrap
import threading
import time
import tty
import urllib.error
import urllib.request
from collections import deque

from rich.console import Console, Group
from rich.live import Live
from rich.text import Text

import scribe

BARS = " ▁▂▃▄▅▆▇█"
ACCENT = "#f97316"
REVEAL_CPS = 90       # typewriter speed for new lines
FADE = (14, 26, 38)   # seconds until a line dims, dims further, disappears
VOICE_RMS = 0.0015    # quieter than this counts as silence
WIDTH = 72


class Client:
    def __init__(self, port):
        self.base = f"http://127.0.0.1:{port}"

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=3) as r:
            return json.load(r)

    def post(self, path, body=None):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(body or {}).encode(), method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            return json.load(e)

    def listen(self, on_event, stop):
        while not stop.is_set():
            try:
                with urllib.request.urlopen(self.base + "/events", timeout=30) as r:
                    for raw in r:
                        if stop.is_set():
                            return
                        line = raw.decode().strip()
                        if line.startswith("data: "):
                            on_event(json.loads(line[6:]))
            except Exception:
                time.sleep(0.5)


class View:
    def __init__(self, snap):
        self.lock = threading.Lock()
        self.state = snap.get("state", "recording")
        self.title = snap.get("title", "Lecture")
        self.model = snap.get("model", "")
        self.elapsed = snap.get("elapsed", 0.0)
        self.terms = snap.get("terms", [])
        self.vocab = snap.get("vocab", "")
        self.words = sum(len(p["text"].split()) for p in snap.get("paras", []))
        self.marks = len(snap.get("marks", []))
        self.dir = snap.get("dir", "")
        self.lag = 0.0
        self.levels = deque([0.0] * 28, maxlen=28)
        now = time.time()
        self.last_level_at = now
        self.last_voice_at = now
        self.zero_since = None
        self.lines = deque(maxlen=4)  # (text, born)
        self.flash = None  # (text, until)
        self.note = None   # str while typing a note
        self.note_t = 0.0  # when the note was started
        self.confirm_until = 0.0
        self.paused_since = snap.get("paused_since")

    def on_event(self, ev):
        now = time.time()
        with self.lock:
            kind = ev["type"]
            if kind == "level":
                self.elapsed, self.lag = ev["elapsed"], ev["lag"]
                self.levels.append(ev["rms"])
                self.last_level_at = now
                if ev["rms"] > VOICE_RMS:
                    self.last_voice_at = now
                if ev["rms"] == 0.0:
                    self.zero_since = self.zero_since or now
                else:
                    self.zero_since = None
            elif kind == "para":
                self.lines.append((ev["text"], now))
                self.words += len(ev["text"].split())
            elif kind == "mark":
                self.marks += 1
                label = f"★ marked {scribe.fmt_ts(ev['t'])}" + (f"  {ev['note']}" if ev["note"] else "")
                self.flash = (label, now + 2.5)
            elif kind == "context":
                self.terms = ev["terms"]
            elif kind == "status":
                self.state = ev["state"]
                self.dir = ev.get("dir", self.dir)
                self.paused_since = ev.get("since") if self.state == "paused" else None
                if self.state == "recording":
                    self.last_level_at = self.last_voice_at = now

    def mic(self, now):
        if self.state == "paused":
            return "mic off", "grey50"
        if self.state != "recording":
            return "standby", "grey50"
        if now - self.last_level_at > 2:
            return "no input", "red"
        if self.zero_since and now - self.zero_since > 2:
            return "muted", "red"
        if now - self.last_voice_at > 8:
            return "quiet", "yellow"
        return "listening", "green"


def meter(levels):
    out = Text()
    for rms in levels:
        i = min(len(BARS) - 1, int((rms ** 0.5) / 0.22 * (len(BARS) - 1)))
        out.append(BARS[i], style=ACCENT if i >= 6 else "#c2c2c2" if i >= 3 else "grey50")
    return out


def render(v, console):
    now = time.time()
    width = min(WIDTH, console.width - 4)
    pad = " " * max(2, (console.width - width) // 2)
    rows = []

    def line(*parts):
        t = Text(pad)
        for part in parts:
            if isinstance(part, Text):
                t.append_text(part)
            else:
                t.append(*part)
        return t

    with v.lock:
        # header: state + clock on the left, title on the right
        if v.state == "recording":
            dot = ("●", ACCENT) if int(now * 1.6) % 2 == 0 else ("●", "#7a3a12")
            status = Text.assemble(dot, ("  rec", "grey62"))
        elif v.state == "paused":
            status = Text.assemble(("❚❚", "yellow"), ("  paused", "grey62"))
        elif v.state == "finishing":
            status = Text.assemble(("◌", "yellow"), ("  saving", "grey62"))
        else:
            status = Text.assemble(("✓", "green"), ("  saved", "grey62"))
        head = Text.assemble(status, "   ", (scribe.fmt_ts(v.elapsed), "bold"))
        title = Text(v.title, style="grey62")
        title.truncate(max(0, width - len(head) - 4), overflow="ellipsis")
        head.append(" " * (width - len(head) - len(title)))
        head.append_text(title)

        mic_label, mic_style = v.mic(now)
        lag = f"{v.lag:.0f}s" if v.state == "recording" else "–"
        lag_style = "yellow" if v.lag > 30 else "grey50"
        stats = Text.assemble(
            meter(v.levels), "   ", ("● ", mic_style), (mic_label, mic_style),
        )
        numbers = Text.assemble(
            (f"{v.words:,}", "default"), (" words", "grey50"), ("   ·   ", "grey35"),
            (f"{v.marks}", "default"), (" marked", "grey50"), ("   ·   ", "grey35"),
            ("lag ", "grey50"), (lag, lag_style), ("   ·   ", "grey35"),
            (v.model or "whisper", "grey50"),
        )

        # transcript: newest line types itself out, older ones fade and go
        blocks = []
        n = len(v.lines)
        for idx, (text, born) in enumerate(v.lines):
            age = now - born
            newer = n - 1 - idx
            if age > FADE[2] or newer >= 3:
                continue
            if newer == 0 and age < FADE[0]:
                style = "bold #f2f2f2"
            elif newer <= 1 and age < FADE[1]:
                style = "grey62"
            else:
                style = "grey35"
            shown = text[: int(age * REVEAL_CPS)] if newer == 0 else text
            blocks.append((textwrap.wrap(shown, width) or [""], style))
        tx_height = max(4, console.height - 13)
        tx_lines = []
        for wrapped, style in blocks:
            if tx_lines:
                tx_lines.append(Text(""))
            tx_lines += [line((w, style)) for w in wrapped]
        tx_lines = tx_lines[-tx_height:]
        if not tx_lines and v.state == "recording":
            tx_lines = [line(("listening…", "grey35"))]
        elif not tx_lines and v.state == "paused":
            tx_lines = [line(("paused — nothing is being recorded", "grey35"))]

        terms = v.terms or ([t.strip() for t in v.vocab.split(",") if t.strip()] if v.vocab else [])
        context = Text.assemble(("context  ", "grey42"))
        if terms:
            for i, t in enumerate(terms[:8]):
                context.append(("  ·  " if i else ""), style="grey35")
                context.append(t, style="italic grey70")
        else:
            context.append("learns key terms as the lecture goes", style="grey35")
        context.truncate(width, overflow="ellipsis")

        if v.note is not None:
            footer = Text.assemble(("note › ", ACCENT), (v.note, "default"), ("▏", ACCENT),
                                   ("     enter save · esc cancel", "grey42"))
        elif v.flash and now < v.flash[1]:
            footer = Text(v.flash[0], style=ACCENT)
        elif v.state == "finishing":
            footer = Text("finishing the last few seconds…", style="grey50")
        elif v.state == "saved":
            footer = Text(f"saved to sessions/{v.dir}", style="green")
        elif now < v.confirm_until:
            footer = Text("press again to stop and save", style="yellow")
        else:
            if v.state == "paused":
                keys = (("p", "resume"), ("s", "stop"), ("q", "quit"))
                lead = [(f"paused {scribe.fmt_ts(now - (v.paused_since or now))}", "yellow"), ("     ", "")]
            else:
                keys = (("m", "mark"), ("n", "note"), ("p", "pause"), ("s", "stop"), ("q", "quit"))
                lead = []
            footer = Text.assemble(*lead, *[
                part for key, label in keys
                for part in ((key, "bold grey70"), (f" {label}     ", "grey42"))
            ])

    rows += [Text(""), line(head), Text(""), line(stats), line(numbers), Text("")]
    rows += [Text("")] * (tx_height - len(tx_lines)) + tx_lines
    rows += [Text(""), Text(""), line(context), Text(""), line(footer)]
    return Group(*rows)


class Keys:
    """Raw single-key input without echo."""

    def __enter__(self):
        self.fd = sys.stdin.fileno()
        self.saved = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        return self

    def __exit__(self, *a):
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)

    def read(self, timeout):
        if select.select([self.fd], [], [], timeout)[0]:
            return os.read(self.fd, 64).decode(errors="ignore")
        return ""


def ask(console, label, hint=""):
    if hint:
        console.print(f"  [grey35]{'':<7}{hint}[/]")
    return console.input(f"  [grey50]{label:<6}[/] [{ACCENT}]›[/] ").strip()


def run(args):
    console = Console(highlight=False)
    client = Client(args.port)
    owned = False
    try:
        snap = client.get("/state")
    except Exception:
        snap = None

    console.print()
    console.print(f"  [bold]scribe[/]  [grey42]live lecture transcription · on-device[/]")
    console.print()
    if snap and snap["state"] in ("recording", "paused"):
        console.print(f"  [grey50]joining the recording in progress…[/]")
    else:
        title = ask(console, "title")
        vocab = ask(console, "terms", "optional · course jargon, comma separated · it also learns as it goes")
        if snap is None:
            scribe.LOG = lambda *a, **k: None
            with console.status("  [grey50]loading speech model…[/]", spinner="dots"):
                scribe.start_server(args.model, args.port, args.simulate, not args.no_menubar)
            owned = True
        snap = client.post("/start", {"title": title, "vocab": vocab})
        if "error" in snap:
            console.print(f"  [red]{snap['error']}[/]")
            return

    view = View(snap)
    stop = threading.Event()
    threading.Thread(target=client.listen, args=(view.on_event, stop), daemon=True).start()

    def request_stop():
        if view.state in ("recording", "paused"):
            client.post("/stop")

    def toggle_pause():
        res = client.post("/resume" if view.state == "paused" else "/pause")
        if "error" in res:
            with view.lock:
                view.flash = (res["error"], time.time() + 4)

    saved_at = None
    try:
        with Keys() as keys, Live(render(view, console), console=console, screen=True,
                                  auto_refresh=False, redirect_stderr=False) as live:
            while True:
                key = keys.read(0.08)
                with view.lock:
                    typing = view.note is not None
                if typing:
                    for ch in key:
                        with view.lock:
                            if ch in "\r\n":
                                note, view.note = view.note, None
                                body = {"note": note, "t": view.note_t}
                                threading.Thread(target=client.post, args=("/mark", body), daemon=True).start()
                                break
                            if ch == "\x1b":
                                view.note = None
                                break
                            if ch in "\x7f\b":
                                view.note = view.note[:-1]
                            elif ch.isprintable():
                                view.note += ch
                elif key and view.state in ("recording", "paused"):
                    k = key[-1].lower()
                    live_rec = view.state == "recording"
                    if k == "m" and live_rec:
                        threading.Thread(target=client.post, args=("/mark",), daemon=True).start()
                    elif k == "n" and live_rec:
                        view.note, view.note_t = "", view.elapsed
                    elif k == "p":
                        threading.Thread(target=toggle_pause, daemon=True).start()
                    elif k in "sq" and (k == "s" or owned):
                        if time.time() < view.confirm_until:
                            view.confirm_until = 0
                            request_stop()
                        else:
                            view.confirm_until = time.time() + 3
                    elif k == "q":
                        break  # detach; the separate recorder keeps going
                elif key and view.state == "saved":
                    break
                if view.state == "saved":
                    saved_at = saved_at or time.time()
                    if time.time() - saved_at > 2.5:
                        break
                live.update(render(view, console), refresh=True)
    except KeyboardInterrupt:
        if owned and view.state in ("recording", "paused"):
            request_stop()
    finally:
        stop.set()

    if owned and view.state != "saved":
        console.print("  [grey50]saving…[/]")
        while view.state != "saved":
            time.sleep(0.2)

    console.print()
    if view.state == "saved":
        console.print(f"  [green]✓[/] saved   [bold]sessions/{view.dir}[/]")
        console.print(f"    [grey50]{scribe.fmt_ts(view.elapsed)} · {view.words:,} words · {view.marks} marked[/]")
        console.print()
        console.print(f"  [grey50]accurate pass →[/] ./scribe.command final {view.dir}")
    else:
        console.print("  [grey50]detached — the recording is still running.[/]")
    console.print()
