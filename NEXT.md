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

## Done (2026-08-17, native popup UTF-8 fix)

Japanese popup text no longer becomes mojibake (`縺…`) on Windows. The parent
writes UTF-8 JSON, but the tkinter child was reading that pipe through the
machine's CP932 `sys.stdin` wrapper; ASCII definitions survived, masking that the
headword/reading transport was corrupt. The child now reads raw stdin bytes and
decodes UTF-8 explicitly, with an fd-0 path for PyInstaller's `--noconsole`
build. `test_ocr.py` locks a `天使【てんし】` byte round-trip.

## Done (2026-08-17, popup name-overmatch fix)

The screen popup now compensates for having no kuromoji token boundary. A rare
name may no longer consume a trailing kana particle and bury an established
word (`夢か……` now opens `夢【ゆめ】 dream`, not the name `夢か【ゆめか】 Yumeka`).
When a real word wins, same-spelling personal-name entries are also suppressed
in the compact native popup; pure-name and katakana-name hits remain available.
Repeated glosses across adjacent JMdict senses are collapsed for the compact
display. The exact screenshot regression is locked in `test_ocr.py` and
`test_ranking.py`.

## Done (2026-08-17, switchable OCR reader + screen popup)

Screen OCR now has two live-switchable presentation modes backed by the same
MeikiOCR instance and saved region. **Reader window** preserves the traditional
stable-line publishing flow. **Screen popup** retains per-character OCR boxes,
maps the global mouse position to the hovered character, runs the remaining text
through the existing ranked `/scan` dictionary logic, and displays compact
definitions beside the cursor while the Caps Lock toggle is on. The lightweight
tkinter popup runs in an isolated no-focus subprocess and opts out of Windows
screen capture so it cannot recursively OCR itself. Its palette follows the
app's six core theme colours. No new runtime dependency.

## Done (2026-08-17, Meikipop-style game OCR)

**MeikiOCR is now the default OCR engine.** Normal source setup installs it and
release/local PyInstaller builds bundle it into the app; `python setup.py --ocr`
installs or verifies only that backend. Meikipop's game-trained two-stage ONNX
pipeline performs whole-region text-line detection followed by batched character
recognition, with the same 0.5 detection,
0.1 recognition, and punctuation confidence settings. Character boxes drive
top-to-bottom / vertical right-to-left ordering and median-size furigana removal.
Complete-frame reads are pixel-cached so the stability confirmation does not run
ONNX twice. MeikiOCR is the sole engine: installation or model-start failures
surface in the OCR panel with a repair command instead of switching engines.
The retired Windows/manga hybrid, its crop/seam voting, PowerShell worker, and
dictionary-coverage arbiter were deleted. Pure result-shaping tests cover
furigana filtering, confidence traces, and vertical reading order.

## Done (2026-07-20, page numbers + jump)

**Real page numbers and jump-to-page.** Footer + Book-panel now show `p. N / M`
from a page map: bookPages[] = the start line of each page, built by buildPageMap
running the exact fillForward pagination in an offscreen measurer that mirrors
the real pane's geometry + typography (width/height/padding/font/line-height/
writing-mode copied from #lines, paged .line box reset via a styleKid so the base
.line chip CSS doesn't leak in). Cached by a layout fingerprint (book, vertical,
paneW, paneH, fontSize, lineHeight, furi, tokenizer-ready); rebuilt via
refreshBookLayout on resize, vertical toggle, and font/line-height/furigana
changes (settings.js dispatches vntex-appearance-change). Map build yields every
25 pages + a 20s hard ceiling so a huge novel never freezes; shows "paginating…"
meanwhile. Jump UI in the Book panel: a range slider (live preview on input,
saves on change) + a number box, both 1-based, hidden until a multi-page book is
open. Verified measurer↔render consistency: page turns land exactly on map
boundaries across furigana on/off, a 52px font bump (60→240 pages), and vertical
mode. fillForward extracted from renderBookPage so map and render share one
pagination path.

## Done (2026-07-20, book typography)

**Paged mode now LOOKS like a book** (user: "how is this close to books and
kindle" — the pagination worked but pages rendered as the session's chip UI:
fit-content hover-highlighted pills with gaps). `#lines.paged` CSS: centred
46em column, lines become continuous justified paragraphs with 1em text-indent,
margins/padding/border-radius/hover-fill zeroed; vertical mode uses full-height
right-to-left columns. Direction-aware page-turn slide animation
(.turn-fwd/.turn-back, reversed in vertical, prefers-reduced-motion respected).
Touch swipe turns pages (left = next horizontally, reversed vertical).

## Done (2026-07-19, fifth pass — Kindle-style pages)

**Book mode paginates now** ("just copy kindle atp"). One page in the DOM at a
time: renderBookPage appends lines until the pane overflows (overflowing line →
next page; a single line taller than the pane gets its own clipped page),
bookTurn(±1) flips — backward fill computes where the previous page must start.
Inputs: click far side of the page (Kindle tap zones, near side goes back),
arrows / Space / Shift+Space / PageUp+Down / ArrowUp+Down, wheel (250ms
throttle); all flip direction in vertical mode. pos = first line of the page,
saved to /book/pos on every turn. Resize and the vertical toggle re-fill from
the same pos. Glass footer pill: title · percent. Lazy-tokenization machinery
(IntersectionObserver + tokenizeAround) deleted — a page is ~10-30 lines,
tokenized synchronously at build. Ctrl+F now only sees the current page (logs
button still searches everything) — acceptable trade.

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

Historical OCR work introduced drag-select capture, typewriter stability, and
persisted regions. Its Windows.Media.Ocr + manga-ocr hybrid and all associated
crop/seam/dictionary arbitration were retired on 2026-08-17 after MeikiOCR became
the sole engine. Clipboard input still drops non-Japanese paths and hashes.

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
