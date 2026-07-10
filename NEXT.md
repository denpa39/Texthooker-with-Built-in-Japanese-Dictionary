# Next improvements — handoff

Backlog for "Down the Rabbit Hole" (VN texthooker + offline JMdict dictionary).
Repo: Python stdlib server (`server.py`), setup/DB builder (`setup.py`), de-inflector
(`deinflect.py` + generated `deinflect_data.py`), front-end (`static/app.js`,
`settings.js`, `style.css`, `index.html`). DB = `dict.sqlite` (gitignored, built by
`python setup.py`). Tests: `test_ranking.py`, `python deinflect.py`. Verify UI with the
preview tools on `.claude/launch.json` server "texthooker" (port 6972).

## Remaining ideas

- **Word audio** — 🔊 button, Yomitan-style JapanesePod101 URL. Needs internet; optional.
- **OCR niceties** — multi-monitor region picker (current overlay covers the primary
  monitor), optional per-region preprocessing (upscale/threshold) for low-contrast text.

## Done (2026-07-06)

OCR fallback mode (`ocr.py`): drag-select screen region, GDI capture, Windows
OCR, typewriter-animation stability gate, region persisted. manga-ocr was tried
as the default engine and removed for hallucinating full Japanese sentences
from no-text frames (generative model, never returns empty) — then re-enabled
(2026-07-10) as `HybridOcr`: Windows OCR locates text (line/word bounding
boxes, also the presence gate — no Japanese means frame skipped), manga-ocr
reads each line in one call: ≤6:1-aspect word-boundary chunks stacked vertically
into a near-square canvas (multi-row manga-bubble shape = its training data, and
the decoder gets whole-sentence context — isolated chunk reads swapped in
plausible wrong chars, 海水→海２). Whole-region frames make manga-ocr hallucinate
even when real text is on screen (ViT squishes everything to 224x224) — the
first gate-only attempt still hallucinated on a full-screen NVL game; the whole
frame in one canvas starves resolution and misreads too, so one canvas per line
is the sweet spot. Spans tile the line contiguously (a Windows-missed word box
must not leave a glyph uncovered) and every multi-chunk line is read twice with
shifted seams — the decoder sometimes drops a glyph at a row seam (まもなく→
もなく), the two reads then disagree and the Windows text picks the winner by
similarity (ties: closest length to the Windows char count — seam doubles like
空空 read long). Interior cuts nudge to the least-inky pixel column nearby (a
cut through a glyph halves it into both canvas rows and the decoder reads it
twice — deterministically, so neither the dual-read vote nor the publish
confirmation reliably catches it) and the outer span edges extend past the
line bbox (Windows routinely misses the trailing 。box, an edge hole that
contiguous tiling can't cover). Change detection is text-level: a cheap Windows
peek on every pixel change tells cursor blinks (pixels churn, text same → skip)
from real text (must hold two consecutive polls before manga-ocr runs) — this
replaced the pixel-settle loop that stalled 2s on every blink and the
read-twice publish confirmation. Lines sort by (y, x); Windows line order isn't guaranteed and
once flipped, publishing a reordered duplicate. Text publishes only after two
consecutive loop passes agree — one-off garbage from mid-transition frames dies
unconfirmed. Lines under 55% of the tallest are dropped as furigana.
Clipboard source now also drops non-Japanese text (copied paths/hashes used to
become reader lines).

## Done (2026-07-02 sweep, trimmed 07-03 per user feedback)

Kanji info cards (KANJIDIC2, `/kanji`), hide-names popup toggle, Anki polish
(configurable deck + toolbar indicator + dup feedback), manual lookup box
(romaji accepted), websocket input (Textractor :6677 / Agent :9001, `--ws`),
server-side session log (`logs/`), LAN mode (`--lan` + responsive pass),
PyInstaller packaging (`build_exe.bat`, app exe `--noconsole`), run.bat launches
console-less via pythonw (startup errors -> message box), window close
hard-exits the process (`os._exit`), export via server to
`exports/` (WebView2 can't blob-download), stats counter in English.

Built then REMOVED on user request (don't re-add without asking): Tanaka example
sentences, the ENTIRE pitch-accent feature (chip + Kanjium table), the ENTIRE
known-words feature (marking, dimming, import/export), per-line coverage %,
furigana-on-unknown-only (back to plain on/off).

## Known loose ends

- Yomitan rule table is **GPL-3.0** (`deinflect_data.py`) — if the repo is published,
  it must carry a GPL-compatible licence. Noted in README "Data & licences".
- One test Anki card (食べる) may still be in the user's Anki deck "Down the Rabbit Hole".
- `dict.sqlite` not committed by design; fresh clones run `python setup.py`.
- New setup steps [5/8] kanji and [6/8] examples add tables to an existing
  `dict.sqlite` on the next `python setup.py` run (no `--force` needed).
