#!/usr/bin/env python3
"""Scribe — record lectures and transcribe them live, entirely on this machine.

    python scribe.py                       # terminal recorder (default)
    python scribe.py serve                 # browser recorder at http://localhost:8765
    python scribe.py final <session|file>  # high-accuracy re-transcription of a full recording

Every session saves the full audio (audio.flac) alongside the live transcript, so the
live text is a convenience and the recording is the source of truth.
"""
import argparse
import datetime as dt
import json
import queue
import re
import subprocess
import sys
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

import numpy as np
import soundfile as sf

SR = 16000
ROOT = Path(__file__).resolve().parent
SESSIONS = ROOT / "sessions"
INDEX_HTML = ROOT / "static" / "index.html"
LOG = print  # the terminal UI silences this so it can own the screen

# Filler that dominates spoken lectures but says nothing about the topic.
STOPWORDS = set("""
about above actually after again against alright also although always another anyone anything
around back basically because been before being below between both bring call called came cannot
case come comes coming could didn doesn doing done down during each either else enough even ever
every example find first from front gets getting give given gives going gonna good got gotta great
guys have having here hmm idea into just keep kind know known last least less like little look
looking lots made make makes making many maybe mean means might more most much must need never next
nice none nothing okay once only onto other others over part people pick point pretty probably
question quite rather really right said same saying says second see seen should show side since
some someone something sort start still such sure take takes talk talking tell than thank thanks
that their them then there these they thing things think this those though three through time
today together told took toward true trying turn under understand until upon used using very want
wanted wants well went were what whatever when where whether which while whole will with within
without work would write yeah year years your yours
""".split())


def extract_terms(texts, limit=10):
    """Recurring topic words and phrases from the transcript so far."""
    unigrams, bigrams = Counter(), Counter()
    for text in texts:
        words = [w.strip("'-") for w in re.findall(r"[a-z][a-z'-]*", text.lower())]
        words = [w.split("'")[0] for w in words]
        content = [w if len(w) > 3 and w not in STOPWORDS else None for w in words]
        unigrams.update(w for w in content if w)
        bigrams.update(f"{a} {b}" for a, b in zip(content, content[1:]) if a and b)
    terms, covered = [], set()
    for t, c in bigrams.most_common():
        if c < 3 or len(terms) >= limit // 2:
            break
        if not covered & set(t.split()):  # "definite matrices" adds nothing to "positive definite"
            terms.append(t)
            covered.update(t.split())
    for w, c in unigrams.most_common():
        if len(terms) >= limit or c < 3:
            break
        if w not in covered:
            terms.append(w)
    return terms


def fmt_ts(sec):
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def load_model(name):
    from faster_whisper import WhisperModel

    print(f"Loading Whisper model '{name}' (first run downloads it)...", flush=True)
    return WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=4)


def make_prompt(title, vocab, context="", terms=()):
    """Whisper follows prompts that read like prior transcript, not bare keyword lists."""
    parts = []
    if title:
        parts.append(f"Lecture: {title}.")
    if vocab:
        parts.append(f"Today we will cover {vocab.rstrip('.')}.")
    terms = [t for t in terms if t not in vocab.lower()]
    if terms:
        parts.append(f"Key terms so far: {', '.join(terms)}.")
    if context:
        parts.append(context)
    return " ".join(parts) or None


def fmt_dur(sec):
    return f"{round(sec / 60)} min" if sec >= 90 else f"{int(sec)} s"


def write_transcript(path, meta, paragraphs, marks):
    """paragraphs: list of (start_sec, text). Pauses from meta are shown inline."""
    lines = [f"# {meta['title']}", ""]
    lines.append(
        f"*Recorded {meta['started'][:16].replace('T', ' ')} · "
        f"{fmt_ts(meta.get('duration', 0))} · model {meta['model']}*"
    )
    if meta.get("vocab"):
        lines.append(f"\n*Key terms: {meta['vocab']}*")
    if marks:
        lines += ["", "## Marked moments", ""]
        lines += [f"- [{fmt_ts(m['t'])}] {m['note'] or '(marked)'}" for m in marks]
    lines += ["", "## Transcript", ""]
    items = [(start, f"**[{fmt_ts(start)}]** {text}") for start, text in paragraphs]
    items += [(p["t"] - 0.01, f"*— paused {fmt_dur(p['seconds'])} —*") for p in meta.get("pauses", [])]
    for _, line in sorted(items, key=lambda x: x[0]):
        lines += [line, ""]
    path.write_text("\n".join(lines))


def find_cut(buf, final):
    """Pick where to end the next transcription chunk: a quiet spot 6–28 s in."""
    n = len(buf)
    if final:
        return n if n else None
    if n < 10 * SR:
        return None
    frame = SR // 10
    usable = min(n, 28 * SR) // frame * frame
    rms = np.sqrt(np.mean(buf[:usable].reshape(-1, frame) ** 2, axis=1))
    rms = np.convolve(rms, np.ones(3) / 3, mode="same")
    lo = 60  # don't cut before 6 s
    i = lo + int(np.argmin(rms[lo:]))
    if rms[i] < 0.5 * np.median(rms) or n >= 25 * SR:
        return i * frame + frame // 2
    return None


class Hub:
    """Fan-out of server-sent events to every open browser tab."""

    def __init__(self):
        self.clients = set()
        self.lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=2000)
        with self.lock:
            self.clients.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.clients.discard(q)

    def publish(self, event):
        with self.lock:
            for q in list(self.clients):
                try:
                    q.put_nowait(event)
                except queue.Full:
                    pass


class Session:
    def __init__(self, model, model_name, title, vocab, hub, simulate=None):
        self.model = model
        self.hub = hub
        started = dt.datetime.now()
        slug = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-")[:50] or "lecture"
        self.dir = SESSIONS / f"{started:%Y-%m-%d_%H%M}_{slug}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.meta = {
            "title": title or "Lecture",
            "vocab": vocab,
            "started": started.isoformat(timespec="seconds"),
            "model": model_name,
        }
        self.segments = []
        self.paragraphs = []
        self.marks = []
        self.terms = []  # learned from the transcript as the lecture goes
        self.pauses = []  # {"t": recording time, "seconds": how long}
        self.paused_since = None
        self.samples_written = 0
        self.samples_done = 0
        self.state = "recording"

        self.audio = sf.SoundFile(self.dir / "audio.flac", "w", SR, 1, format="FLAC")
        self.live_md = open(self.dir / "transcript.md", "w")
        self.live_md.write(f"# {self.meta['title']} (live, in progress)\n\n")
        self.q_raw = queue.Queue()
        self.q_tx = queue.Queue()
        self.writer = threading.Thread(target=self._write_loop, daemon=True)
        self.transcriber = threading.Thread(target=self._transcribe_loop, daemon=True)
        self.writer.start()
        self.transcriber.start()

        if simulate:
            self.stream = None
            self._stop_sim = threading.Event()
            threading.Thread(target=self._simulate, args=(simulate,), daemon=True).start()
        else:
            import sounddevice as sd

            self.stream = sd.InputStream(
                samplerate=SR, channels=1, dtype="float32", blocksize=SR // 10,
                callback=lambda data, *_: self.q_raw.put(data[:, 0].copy()),
            )
            self.stream.start()

    @property
    def elapsed(self):
        return self.samples_written / SR

    def _simulate(self, path):
        from faster_whisper import decode_audio

        audio = decode_audio(path, sampling_rate=SR)
        block = SR // 10
        for i in range(0, len(audio), block):
            while self.state == "paused" and not self._stop_sim.is_set():
                time.sleep(0.05)
            if self._stop_sim.is_set():
                break
            self.q_raw.put(audio[i:i + block].copy())
            time.sleep(0.1 / 4)  # 4x real time

    def _write_loop(self):
        last_level = 0
        flushed = time.time()
        while (block := self.q_raw.get()) is not None:
            if isinstance(block, str):  # "flush": pass through to the transcriber in order
                self.q_tx.put(block)
                continue
            self.audio.write(block)
            self.samples_written += len(block)
            self.q_tx.put(block)
            now = time.time()
            if now - flushed > 5:
                self.audio.flush()
                flushed = now
            if now - last_level > 0.1:
                self.hub.publish({
                    "type": "level",
                    "rms": float(np.sqrt(np.mean(block ** 2))),
                    "elapsed": self.elapsed,
                    "lag": (self.samples_written - self.samples_done) / SR,
                })
                last_level = now
        self.q_tx.put(None)

    def _transcribe_loop(self):
        buf = np.zeros(0, np.float32)
        offset = 0
        done = False
        while True:
            blocks = []
            try:
                blocks.append(self.q_tx.get(timeout=0.5))
                while True:
                    blocks.append(self.q_tx.get_nowait())
            except queue.Empty:
                pass
            done = done or any(b is None for b in blocks)
            flush = any(isinstance(b, str) for b in blocks)  # paused: don't hold half a sentence
            blocks = [b for b in blocks if isinstance(b, np.ndarray)]
            if blocks:
                buf = np.concatenate([buf, *blocks])
            while (cut := find_cut(buf, final=done or flush)) is not None:
                self._transcribe(buf[:cut], offset / SR)
                buf, offset = buf[cut:], offset + cut
                self.samples_done = offset
            if done:
                return

    def _transcribe(self, chunk, start):
        if len(chunk) < SR // 2:
            return
        context = " ".join(p[1] for p in self.paragraphs[-2:])[-300:]
        prompt = make_prompt(self.meta["title"], self.meta["vocab"], context, self.terms)
        try:
            segs, _ = self.model.transcribe(
                chunk, beam_size=1, vad_filter=True, initial_prompt=prompt,
                condition_on_previous_text=False,
            )
            segs = list(segs)
        except Exception as e:  # never let a bad chunk kill the recording
            LOG(f"transcription error at {fmt_ts(start)}: {e}", file=sys.stderr)
            return
        texts = []
        for s in segs:
            text = s.text.strip()
            if text:
                seg = {"start": round(start + s.start, 2), "end": round(start + s.end, 2), "text": text}
                self.segments.append(seg)
                texts.append(text)
        if not texts:
            return
        para_start = start + segs[0].start
        para = " ".join(texts)
        self.paragraphs.append((para_start, para))
        self.live_md.write(f"**[{fmt_ts(para_start)}]** {para}\n\n")
        self.live_md.flush()
        self.hub.publish({"type": "para", "start": para_start, "text": para})
        terms = extract_terms(p[1] for p in self.paragraphs)
        if terms != self.terms:
            self.terms = terms
            self.hub.publish({"type": "context", "terms": terms})

    def mark(self, note, t=None):
        m = {"t": round(self.elapsed if t is None else min(float(t), self.elapsed), 1), "note": note.strip()}
        self.marks.append(m)
        self.hub.publish({"type": "mark", **m})
        return m

    def pause(self):
        """Release the mic; nothing is recorded until resume()."""
        if self.stream:
            self.stream.stop()
        self.state = "paused"
        self.paused_since = time.time()
        self.q_raw.put("flush")
        self.pauses.append({"t": round(self.elapsed, 1), "seconds": 0})
        self.hub.publish({"type": "status", "state": "paused", "since": self.paused_since})

    def resume(self):
        if self.stream:
            self.stream.start()
        self._end_pause()
        self.state = "recording"
        self.hub.publish({"type": "status", "state": "recording"})

    def _end_pause(self):
        if self.paused_since:
            self.pauses[-1]["seconds"] = round(time.time() - self.paused_since)
            self.paused_since = None

    def stop(self):
        """Stop capture; finish transcribing the tail in the background."""
        self._end_pause()
        self.state = "finishing"
        self.hub.publish({"type": "status", "state": "finishing"})
        if self.stream:
            if self.stream.active:
                self.stream.stop()
            self.stream.close()
        else:
            self._stop_sim.set()
        self.q_raw.put(None)
        threading.Thread(target=self._finish, daemon=True).start()

    def _finish(self):
        self.writer.join()
        self.transcriber.join()
        self.audio.close()
        self.live_md.close()
        self.meta["duration"] = round(self.elapsed, 1)
        self.meta["marks"] = self.marks
        self.meta["auto_terms"] = self.terms
        self.meta["pauses"] = self.pauses
        (self.dir / "session.json").write_text(json.dumps(self.meta, indent=2))
        (self.dir / "transcript.json").write_text(json.dumps(self.segments, indent=2))
        write_transcript(self.dir / "transcript.md", self.meta, self.paragraphs, self.marks)
        self.state = "saved"
        self.hub.publish({"type": "status", "state": "saved", "dir": self.dir.name})
        LOG(f"Saved {self.dir}", flush=True)

    def snapshot(self):
        return {
            "state": self.state, "title": self.meta["title"], "dir": self.dir.name,
            "elapsed": self.elapsed, "model": self.meta["model"],
            "vocab": self.meta["vocab"], "terms": self.terms, "paused_since": self.paused_since,
            "paras": [{"start": s, "text": t} for s, t in self.paragraphs],
            "marks": self.marks,
        }


def list_sessions():
    out = []
    if SESSIONS.exists():
        for d in sorted(SESSIONS.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            meta = {}
            if (d / "session.json").exists():
                meta = json.loads((d / "session.json").read_text())
            out.append({
                "dir": d.name, "title": meta.get("title", d.name),
                "duration": meta.get("duration"),
                "final": (d / "transcript_final.md").exists(),
            })
    return out


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            if self.path == "/":
                body = INDEX_HTML.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/state":
                s = app["session"]
                self._json(s.snapshot() if s else {"state": "idle"})
            elif self.path == "/sessions":
                self._json(list_sessions())
            elif self.path.startswith("/files/"):
                target = (SESSIONS / unquote(self.path[len("/files/"):])).resolve()
                if not target.is_relative_to(SESSIONS.resolve()) or not target.is_file():
                    return self._json({"error": "not found"}, 404)
                ctype = {".md": "text/plain; charset=utf-8", ".flac": "audio/flac",
                         ".json": "application/json"}.get(target.suffix, "application/octet-stream")
                body = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                q = app["hub"].subscribe()
                try:
                    while True:
                        try:
                            ev = q.get(timeout=15)
                            self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    app["hub"].unsubscribe(q)
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            s = app["session"]
            with app["lock"]:
                if self.path == "/start":
                    if s and s.state != "saved":
                        return self._json({"error": "already recording"}, 409)
                    body = self._body()
                    try:
                        app["session"] = Session(
                            app["model"], app["model_name"], body.get("title", "").strip(),
                            body.get("vocab", "").strip(), app["hub"], app["simulate"],
                        )
                    except Exception as e:
                        return self._json({"error": f"could not start microphone: {e}"}, 500)
                    app["hub"].publish({"type": "status", "state": "recording"})
                    return self._json(app["session"].snapshot())
                if self.path == "/stop":
                    if not s or s.state not in ("recording", "paused"):
                        return self._json({"error": "not recording"}, 409)
                    s.stop()
                    return self._json({"ok": True})
                if self.path == "/pause":
                    if not s or s.state != "recording":
                        return self._json({"error": "not recording"}, 409)
                    s.pause()
                    return self._json({"ok": True})
                if self.path == "/resume":
                    if not s or s.state != "paused":
                        return self._json({"error": "not paused"}, 409)
                    try:
                        s.resume()
                    except Exception as e:
                        return self._json({"error": f"could not restart microphone: {e}"}, 500)
                    return self._json({"ok": True})
                if self.path == "/mark":
                    if not s or s.state != "recording":
                        return self._json({"error": "not recording"}, 409)
                    body = self._body()
                    return self._json(s.mark(body.get("note", ""), body.get("t")))
            self._json({"error": "not found"}, 404)

    return Handler


def launch_menubar(port):
    """Status item in the macOS menu bar; it quits by itself when this process exits."""
    try:
        subprocess.Popen(
            [sys.executable, str(ROOT / "menubar.py"), "--port", str(port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except OSError:
        pass


def start_server(model_name, port, simulate=None, menubar=True):
    """Load the model and serve the recorder API on a background thread."""
    app = {
        "model": load_model(model_name), "model_name": model_name, "hub": Hub(),
        "session": None, "lock": threading.Lock(), "simulate": simulate,
    }
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if menubar:
        launch_menubar(port)
    return app


def cmd_serve(args):
    app = start_server(args.model, args.port, args.simulate, not args.no_menubar)
    url = f"http://localhost:{args.port}"
    print(f"Scribe is running at {url}  (Ctrl+C to quit)", flush=True)
    if not args.no_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        s = app["session"]
        if s and s.state in ("recording", "paused"):
            print("\nStopping the recording and saving...", flush=True)
            s.stop()
            while s.state != "saved":
                time.sleep(0.2)


def cmd_final(args):
    """Re-transcribe a full recording with a bigger model and full context."""
    from faster_whisper import decode_audio

    target = Path(args.target)
    if not target.exists() and (SESSIONS / args.target).exists():
        target = SESSIONS / args.target
    if target.is_dir():
        out_dir, audio_path = target, target / "audio.flac"
        meta_path = target / "session.json"
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    else:  # an audio file recorded elsewhere (phone memo, .m4a, .mp3, ...)
        audio_path = target
        out_dir = SESSIONS / f"{dt.datetime.now():%Y-%m-%d_%H%M}_{target.stem}"
        out_dir.mkdir(parents=True, exist_ok=True)
        meta = {"started": dt.datetime.now().isoformat(timespec="seconds")}
    meta.setdefault("title", target.stem)
    meta.setdefault("started", "")
    vocab = args.vocab or meta.get("vocab", "")
    terms = meta.get("auto_terms", [])
    meta["vocab"] = vocab
    meta["model"] = args.model

    model = load_model(args.model)
    audio = decode_audio(str(audio_path), sampling_rate=SR)
    total = len(audio) / SR
    meta["duration"] = total
    print(f"Transcribing {fmt_ts(total)} of audio...", flush=True)
    segs, _ = model.transcribe(
        audio, beam_size=5, vad_filter=True, initial_prompt=make_prompt(meta["title"], vocab, terms=terms),
        condition_on_previous_text=True,
    )
    segments, paragraphs = [], []
    para_start, para, last_end = None, [], 0.0
    t0 = time.time()
    for s in segs:
        text = s.text.strip()
        if not text:
            continue
        segments.append({"start": round(s.start, 2), "end": round(s.end, 2), "text": text})
        # new paragraph after a pause or once a paragraph spans ~a minute
        if para and (s.start - last_end > 2.0 or s.start - para_start > 60):
            paragraphs.append((para_start, " ".join(para)))
            para = []
        if not para:
            para_start = s.start
        para.append(text)
        last_end = s.end
        rate = s.end / max(time.time() - t0, 1e-6)
        print(f"\r  {fmt_ts(s.end)} / {fmt_ts(total)}  ({rate:.1f}x real time)", end="", flush=True)
    if para:
        paragraphs.append((para_start, " ".join(para)))
    print()
    (out_dir / "transcript_final.json").write_text(json.dumps(segments, indent=2))
    write_transcript(out_dir / "transcript_final.md", meta, paragraphs, meta.get("marks", []))
    print(f"Wrote {out_dir / 'transcript_final.md'}")


def cmd_tui(args):
    import tui

    tui.run(args)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")
    t = sub.add_parser("tui", help="terminal recorder (default)")
    t.add_argument("--model", default="small.en", help="live model (tiny.en, base.en, small.en, ...)")
    t.add_argument("--port", type=int, default=8765)
    t.add_argument("--simulate", metavar="AUDIO", help=argparse.SUPPRESS)
    t.add_argument("--no-menubar", action="store_true", help="don't show the menu bar status")
    t.set_defaults(func=cmd_tui)
    s = sub.add_parser("serve", help="run the live recorder")
    s.add_argument("--model", default="small.en", help="live model (tiny.en, base.en, small.en, ...)")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-browser", action="store_true")
    s.add_argument("--simulate", metavar="AUDIO", help="feed an audio file instead of the mic (testing)")
    s.add_argument("--no-menubar", action="store_true", help="don't show the menu bar status")
    s.set_defaults(func=cmd_serve)
    f = sub.add_parser("final", help="high-accuracy pass over a saved recording or any audio file")
    f.add_argument("target", help="session folder name/path, or an audio file")
    f.add_argument("--model", default="medium.en", help="e.g. small.en (faster), medium.en, large-v3 (slowest)")
    f.add_argument("--vocab", default="", help="course terms to bias spelling, e.g. 'eigenvalue, Hamiltonian'")
    f.set_defaults(func=cmd_final)
    argv = sys.argv[1:]
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help")):
        argv = ["tui", *argv]
    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
