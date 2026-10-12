# Scribe

Records your lectures and transcribes them live. Everything runs on this Mac, so no audio leaves the machine.

## Start

Double-click `scribe.command`, or run `./scribe.command` in a terminal. Enter a title and,
optionally, a few key terms, and recording starts. The first time, macOS asks for microphone
access for your terminal app. Click Allow.

```
  ●  rec   00:42:17                                  Linear Algebra Wk6
  ▂▃▅▆▅▃▂▁▂▃▅▇▆▅▃▂▃▄   ● listening
  6,214 words   ·   3 marked   ·   lag 4s   ·   small.en

  Second, we can check the leading principal minors. If every leading
  principal minor is positive, the matrix is positive definite.

  context  positive definite  ·  quadratic form  ·  eigenvalues

  m mark     n note     p pause     s stop     q quit
```

- The transcript types itself out, then fades. Everything is still saved.
- **context** shows the key terms Scribe has picked up from the lecture so far. It feeds them back
  to the speech model, so it gets the topic's vocabulary right as the lecture goes on.
- **m** marks the moment. **n** marks it with a note (the time is taken when you press n).
- **p** pauses: the mic turns off and nothing is recorded. Press **p** again to resume.
  Pauses show up in the transcript as *— paused 10 min —*.
- **s** twice stops and saves. This also works while paused.
- The mic indicator reads *listening*, *quiet* (no speech for 8s), *muted*, or *no input*.

### Menu bar

While Scribe runs, the macOS menu bar shows its status, so you can keep the terminal out of sight:

| Menu bar | Meaning |
|---|---|
| `● 00:42:17` (orange) | recording |
| `● 00:42:17` (red) | recording, but the mic is muted or sending nothing. Check it. |
| `❚❚ 00:42:17` | paused |
| `◌ saving` → `✓ saved` | finishing up |

Click it to see the title, mic state, word count and context. You can also pause, resume, mark a
moment, or stop from there; stopping asks you to confirm first. It disappears when Scribe exits.
Use `scribe --no-menubar` to turn it off.

Prefer a browser window? `./scribe.command serve` opens the web version. The terminal app attaches
to a recording already running in it.

## After the lecture: the accurate transcript

```bash
.venv/bin/python scribe.py final 2026-10-12_1100_Linear-Algebra-Wk5
```

This runs `medium.en` over the whole recording, which gives better accuracy and punctuation than the live pass.
It writes `transcript_final.md`. On an M1 it runs at about 1.5x real time, so a 1-hour lecture takes about 40 minutes.
Use `--model small.en` for a faster, rougher pass.

You can also run `final` on any audio file, such as a phone voice memo: `scribe.py final ~/Downloads/lecture.m4a`.

## What each session contains

| File | What it is |
|---|---|
| `audio.flac` | The full recording, written to disk as you record. This is the source of truth. |
| `transcript.md` | Live transcript, with timestamped paragraphs and your marked moments |
| `transcript.json` | Live segments with start and end times |
| `transcript_final.md/.json` | The accurate re-transcription, once you've run `final` |
| `session.json` | Title, key terms, duration, marks |

## Tips
- Sit near the speaker, or put the laptop or an external mic close to them. Mic placement matters more than model choice.
- If the status bar says the transcript is falling behind, restart with `./scribe.command --model base.en`.
  The audio is always saved either way.
- Keep the lid open and the Mac plugged in for long lectures.
