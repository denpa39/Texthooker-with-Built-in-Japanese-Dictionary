# Next improvements — handoff

Backlog for "Down the Rabbit Hole" (VN texthooker + offline JMdict dictionary).
Repo: Python stdlib server (`server.py`), setup/DB builder (`setup.py`), de-inflector
(`deinflect.py` + generated `deinflect_data.py`), front-end (`static/app.js`,
`settings.js`, `style.css`, `index.html`). DB = `dict.sqlite` (gitignored, built by
`python setup.py`). Tests: `test_ranking.py`, `python deinflect.py`. Verify UI with the
preview tools on `.claude/launch.json` server "texthooker" (port 6972).

## Remaining ideas

- **OCR per-region preprocessing** — optional upscale/threshold pass for low-contrast
  text (the multi-monitor picker half of "OCR niceties" landed 2026-07-16).

## Done (2026-07-19, fourth pass — reader rework)

**Book mode is a real e-reader now, VN-style advance removed** (user: the
line-at-a-time progress flow "fucked up the whole thing"). /book/open returns the
book's full lines; app.js bookMode replaces the session view with the whole book,
scrollable. Lines render plain and tokenize lazily near the viewport
(IntersectionObserver + a direct tokenizeAround pass — IO needs rendering frames,
backgrounded tabs get none). Reading position = top-of-viewport line, binary-search
detected, debounce-saved to /book/pos, restored on open/reload; the vertical toggle
re-anchors it (vntex-vertical-toggle event). Book text no longer goes through
publish_line — nothing lands in SSE/logs/session; the session restores from
localStorage on Close book; Undo/Clear/Export are no-ops in book mode. Removed:
/book/next, /book/prev, the SSE book flag, advance keys, the floating Next button.

## Done (2026-07-19, third pass)

**Kindle formats + katakana ranking.** Prompted by reading 星の王子さま: the user's
"epub-like file" = .mobi/.azw. book.py now parses the PalmDB container directly —
stdlib PalmDOC-LZ77 decompression, MOBI extra-data trailing-entry trim, cp1252/utf-8
from the header; DRM'd and HUFF/CDIC books answer with a clear Calibre message
instead of garbage. .fb2 (+ fb2 inside a zip) also supported; dispatch is
content-magic first, extension second. RANKING: two katakana rules in /scan —
pure-katakana tokens drop sub-token matches (テグジュペリ showed 大邱「テグ」
"Daegu"), and word-beats-name only holds for established words on katakana hovers
(レオン the Sierra-Leone currency buried the name Leon; カメラ still beats the name
Camera). Both encoded in test_ranking.py.

## Done (2026-07-19, second pass)

**More book formats + vertical text.** book.py grew `parse_book(data, filename)`
dispatch: .txt (Aozora Bunko ruby ｜《》 / ［＃…］ notes / ---- block / 底本 footer
stripped, UTF-8→cp932 fallback), .html through the same extractor, epub by extension
or zip magic; .mobi/.azw/.pdf answer "convert with Calibre". **Vertical (tategaki)
toggle** 縦 in Settings next to B/I: `.vertical` class on `<html>` (settings.js,
persisted with appearance), `#lines` flips to writing-mode: vertical-rl, .line's
sizing switched to logical props so it flips for free; app.js auto-scroll handles
the negative-scrollLeft axis and the book arrows swap (← = next when vertical).
SSE book lines now also trigger a book-state refresh on pages that didn't know a
book was open (phone over LAN gets working controls without a reload).

## Done (2026-07-19)

**Book reader (epub import)**: `book.py` (stdlib zip+OPF spine+html.parser, drops
`<rt>/<rp>` furigana), `/book*` routes in server.py, Book toolbar button + panel +
floating Next ▸ in the UI. Lines feed through publish_line (tagged `book: true` on
SSE so the reader appends verbatim instead of OCR-merge-reconciling); Space/→
advance, ← backscroll (client removes newest line); parsed books + per-book
position persist in `books/` (gitignored). `test_book.py` in CI. Also fixed a
latent keep-alive bug found doing this: POST routes that never read their request
body left it on the socket and every following request on that connection got 501
— do_POST now drains the body before routing.

## Done (2026-07-16, install redesign)

Setup made one-step for new users. **First-run auto-setup**: server.py's
missing-dict path now OFFERS to run setup itself (`_run_first_time_setup` —
console prompt when a console exists; Yes/No MessageBox + setup in its own
console window under pythonw / the frozen exe, which spawns
RabbitHoleSetup.exe). run.bat runs setup.py first when dict.sqlite is missing,
so "double-click run.bat" is the whole Python-path install. **VN frequency by
default**: setup auto-uses jiten_vn.zip when present, else auto-downloads
Innocent Corpus (--no-vn-freq opts out; --innocent now just forces what is
already the default); wordfreq is pip-installed best-effort like pywebview
always was. **Release workflow** (.github/workflows/release.yml): pushing a
v* tag builds both exes with PyInstaller on windows-latest and attaches
DownTheRabbitHole-win64.zip (+ README/LICENSE/START-HERE.txt) to the GitHub
release — README's install section now leads with that no-Python path. All
four _run_first_time_setup branches exercised (stub setup exe, patched
GetConsoleWindow/MessageBoxW); the packaging pwsh step dry-run locally.

## Done (2026-07-16)

Review sweep, all five findings: **LICENSE** (GPL-3.0 — deinflect_data.py is
Yomitan-derived; README/CLAUDE.md note the project licence). **Origin guard**
(`request_allowed` on every request: Host + Origin must be localhost/*.local/
non-global IP, kills drive-by CSRF POSTs against 127.0.0.1 — incl. the /anki
open relay — and DNS rebinding; Tailscale CGNAT stays allowed; matrix in
test_text.py). **Log search** (`/logsearch` + "logs" button in the find bar —
hits grouped by session file in a pinned popup, click loads the line back,
hoverable). **Word audio** (`/audio` JapanesePod101 proxy + ♪ popup button;
the service's fixed 52,288-byte "not available" clip 404s). **LAN QR**
(`/qr` + Settings "Read on your phone" row; qr.py is a ~200-line stdlib QR
encoder — byte mode, v1-5, EC L, mask 0 — verified against the qrcode lib
module-for-module and decoded with jsQR, then frozen as a fixture in
test_qr.py). **Multi-monitor OCR picker** (overlay spans the virtual screen,
overrideredirect + SM_*VIRTUALSCREEN; /ocr/region takes a lock so double-click
can't stack overlays). Nits: /search overflow now drops the obscure tail
(ORDER BY freq), favicon (stdlib-generated ICO), Clear resets the stats
counter, test_text.py covers clean_hook_text / hook._split / _ws_extract_text
/ romajiToKana (node harness like test_merge). CI runs the two new suites.

## Done (2026-07-14)

Emulator hooking (PSP/PS2/Vita/Switch…) via Agent (0xDC00): `setup.py --agent`
downloads the ~120 MB Electron app into `agent/` (gitignored), the Attach panel
grew an "Emulators" section with a Launch Agent button (`/agent` state,
`/agent/start` spawn). The user picks the game script + attaches inside Agent's
own GUI (it's closed-source with no usable CLI — headless driving isn't
possible); text reaches the reader through the existing :9001 websocket client
(now status-tracked in `WS_CONNECTED`) and, before that connects, through
Agent's clipboard copies (publish_line's consecutive-repeat drop dedupes the
double feed). Note: Agent's WS server only listens once attached to a game —
"running but not connected" is normal right after launch.

## Done (2026-07-13, second pass)

Test hardening to close the audit gaps: `/search` regression cases in
test_ranking.py (bm25-cut and sense-tier bug classes), `test_ocr.py` pure-logic
suite (reading order, _clean, _same_line, span tiling, PNG encoder, gates,
coverage — no engines or screenshots needed), GitHub Actions CI running
syntax + test_ocr + test_merge on every push (ranking self-skips without
dict.sqlite). Still open by choice: OCR pipeline fixtures with real frames
(needs captured game screenshots), threshold tuning (needs real-session /ocr
trace data), server.py/app.js split (deferred until they hurt).

## Done (2026-07-13)

English→Japanese reverse lookup (`/search`: FTS5 `gloss_fts` built by setup.py step
[3/7]; lookup box falls back to it for non-romaji ASCII or romaji with no Japanese
hit; ranked first-sense boundary match > any-sense > mid-gloss, then commonness).
Anki cards attach a whole-game-window screenshot (`/snap`: window under the OCR
region else hooked pid, stdlib PNG encoder, ≤1280px). Ctrl+F find bar over session
lines (kana-insensitive, newest-first cycling). Algorithm pass: OCR seam
arbitration + `_same_line` jitter detection get a dictionary-coverage signal
(`_dict_coverage`), ranking gained the boundary rescue for rare-but-real compounds
(生返事), `test_merge.py` locks the py/js merge implementations in parity.

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

- ~~Yomitan rule table is **GPL-3.0** (`deinflect_data.py`) — if the repo is published,
  it must carry a GPL-compatible licence.~~ Resolved 2026-07-16: `LICENSE` = GPL-3.0.
- One test Anki card (食べる) may still be in the user's Anki deck "Down the Rabbit Hole".
- `dict.sqlite` not committed by design; fresh clones run `python setup.py`.
- New setup steps [5/8] kanji and [6/8] examples add tables to an existing
  `dict.sqlite` on the next `python setup.py` run (no `--force` needed).
