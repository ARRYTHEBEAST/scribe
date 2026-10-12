"""Scribe's menu bar status: a dot and a clock while a lecture records.

Started automatically by the recorder; quits on its own when the recorder goes away.
"""
import argparse
import subprocess
import threading
import time

import rumps
from AppKit import (
    NSApplication, NSApplicationActivationPolicyAccessory, NSAttributedString, NSColor, NSFont,
    NSFontAttributeName, NSForegroundColorAttributeName, NSMutableAttributedString,
)

import scribe
from tui import Client, View

ORANGE = NSColor.colorWithSRGBRed_green_blue_alpha_(0.976, 0.451, 0.086, 1)  # tui ACCENT
YELLOW = NSColor.systemYellowColor()
RED = NSColor.systemRedColor()
GREEN = NSColor.systemGreenColor()
DIM = NSColor.secondaryLabelColor()
TEXT = NSColor.labelColor()


def styled(*parts):
    """[(text, color)] -> attributed string with tabular digits so the clock doesn't jitter."""
    font = NSFont.monospacedDigitSystemFontOfSize_weight_(13, 0.0)
    out = NSMutableAttributedString.alloc().init()
    for text, color in parts:
        attrs = {NSFontAttributeName: font, NSForegroundColorAttributeName: color}
        out.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(text, attrs))
    return out


class MenuBar(rumps.App):
    def __init__(self, port):
        super().__init__("Scribe", title="scribe", quit_button=None)
        self.client = Client(port)
        self.view = None
        self.listening = threading.Event()
        self.failures = 0
        self.started = time.time()
        self.last_poll = 0.0

        self.heading = rumps.MenuItem("Scribe")
        self.stats = rumps.MenuItem("")
        self.context = rumps.MenuItem("")
        self.pause_item = rumps.MenuItem("Pause", callback=self.on_pause)
        self.mark_item = rumps.MenuItem("Mark moment", callback=self.on_mark)
        self.stop_item = rumps.MenuItem("Stop and save…", callback=self.on_stop)
        self.menu = [
            self.heading, self.stats, self.context, None,
            self.pause_item, self.mark_item, self.stop_item, None,
            rumps.MenuItem("Open lectures folder", callback=self.on_open),
            rumps.MenuItem("Hide from menu bar", callback=lambda _: rumps.quit_application()),
        ]
        rumps.Timer(self.tick, 0.5).start()

    # -- actions -------------------------------------------------------------
    def _post(self, path):
        threading.Thread(target=self.client.post, args=(path,), daemon=True).start()

    def on_pause(self, _):
        if self.view:
            self._post("/resume" if self.view.state == "paused" else "/pause")

    def on_mark(self, _):
        self._post("/mark")

    def on_stop(self, _):
        if rumps.alert("Stop and save this lecture?", ok="Stop and save", cancel="Keep recording") == 1:
            self._post("/stop")

    def on_open(self, _):
        target = scribe.SESSIONS / self.view.dir if self.view and self.view.dir else scribe.SESSIONS
        subprocess.run(["open", str(target if target.exists() else scribe.ROOT)])

    # -- state ---------------------------------------------------------------
    def poll(self):
        """Resync from /state every couple of seconds; quit once the recorder is gone."""
        try:
            snap = self.client.get("/state")
        except Exception:
            self.failures += 1
            if self.failures >= 3 or (self.view is None and time.time() - self.started > 20):
                rumps.quit_application()
            return
        self.failures = 0
        if self.view is None:
            self.view = View(snap)
            threading.Thread(target=self.client.listen, args=(self.view.on_event, self.listening),
                             daemon=True).start()
        else:
            with self.view.lock:
                self.view.state = snap["state"]
                if snap["state"] != "idle":
                    self.view.title = snap["title"]
                    self.view.dir = snap["dir"]
                    self.view.paused_since = snap.get("paused_since")
                    self.view.marks = len(snap.get("marks", []))

    def tick(self, _):
        now = time.time()
        if now - self.last_poll > 2:
            self.last_poll = now
            self.poll()
        v = self.view
        if v is None:
            return
        with v.lock:
            state, elapsed, title = v.state, v.elapsed, v.title
            mic_label, _ = v.mic(now)
            words, marks, lag, terms = v.words, v.marks, v.lag, v.terms
            paused_for = now - (v.paused_since or now)

        clock = scribe.fmt_ts(elapsed)
        if state == "recording":
            dot = RED if mic_label in ("muted", "no input") else ORANGE
            label = styled(("● ", dot), (clock, TEXT))
        elif state == "paused":
            label = styled(("❚❚ ", YELLOW), (clock, DIM))
        elif state == "finishing":
            label = styled(("◌ ", YELLOW), ("saving", DIM))
        elif state == "saved":
            label = styled(("✓ ", GREEN), ("saved", DIM))
        else:
            label = styled(("scribe", DIM))
        self._nsapp.nsstatusitem.button().setAttributedTitle_(label)

        active = state in ("recording", "paused")
        self.heading.title = title if state != "idle" else "Scribe — not recording"
        if state == "paused":
            self.stats.title = f"paused {scribe.fmt_ts(paused_for)}  ·  mic off"
        elif active:
            behind = f"  ·  {lag:.0f}s behind" if lag > 20 else ""
            self.stats.title = f"{mic_label}  ·  {words:,} words  ·  {marks} marked{behind}"
        else:
            self.stats.title = f"{words:,} words  ·  {marks} marked" if state != "idle" else ""
        shown = "  ·  ".join(terms[:4])
        self.context.title = f"context  {shown}" if shown else ""
        self.pause_item.title = "Resume" if state == "paused" else "Pause"
        self.pause_item.set_callback(self.on_pause if active else None)
        self.mark_item.set_callback(self.on_mark if state == "recording" else None)
        self.stop_item.set_callback(self.on_stop if active else None)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8765)
    args = p.parse_args()
    # menu bar only: no Dock icon, no app switcher entry
    NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    MenuBar(args.port).run()


if __name__ == "__main__":
    main()
