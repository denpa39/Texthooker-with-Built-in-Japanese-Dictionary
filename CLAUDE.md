# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Keep this file updated

**After any change that adds/removes a feature, route, DB table, CLI flag, or file, update this
CLAUDE.md in the same commit.** It is the substitute for re-reading the project every session.

## What this is

"Down the Rabbit Hole" — a Windows visual-novel texthooker with a built-in offline JMdict
dictionary. Pure Python **stdlib only** on the server (no pip dependencies except optional
`pywebview` for the app window and optional `wordfreq` at build time). Vanilla JS frontend,
no framework, no bundler.

## Commands

```sh
python server.py                 # run (opens pywebview window; falls back to browser)
python server.py --no-browser --port 6973   # headless, for testing
python setup.py                  # one-time: downloads kuromoji, JMdict, JMnedict, KANJIDIC2,
                                 # Textractor; builds dict.sqlite (idempotent; --force rebuilds).
                                 # VN frequency is AUTOMATIC: jiten_vn.zip if present, else
                                 # Innocent Corpus download (--no-vn-freq opts out); wordfreq +
                                 # pywebview pip-installed best-effort. server.py OFFERS to run
                                 # this itself when dict.sqlite/kuromoji are missing
                                 # (_run_first_time_setup: console prompt, or Yes/No MessageBox
                                 # + new-console setup when run from pythonw / the frozen exe)
python setup.py --agent          # opt-in (~120 MB): Agent (0xDC00) into agent/ — emulator hooking
python test_ranking.py           # ranking + /search regression tests (needs dict.sqlite)
python test_merge.py             # _merge_reads (py) vs mergeReads (js) parity (needs node)
python test_ocr.py               # OCR pure-logic units (reading order, spans, PNG, gates)
python test_text.py              # text plumbing: clean_hook_text, hook _split, ws extract,
                                 # origin guard, romajiToKana via node (needs node)
python test_qr.py                # QR encoder: format BCH, RS syndromes, structure, fixture
python test_book.py              # e-book parsers: epub spine/ruby, Aozora txt, html, dispatch
python deinflect.py              # de-inflector self-test (41 cases)
node --check static/app.js       # JS syntax check (no other lint/build step exists)
build_exe.bat                    # PyInstaller one-file exes (app + setup)
```

CI (`.github/workflows/tests.yml`) runs syntax + test_ocr + test_merge + test_text +
test_qr + test_book on every push; test_ranking self-skips there (no dict.sqlite) — run it locally
before pushing ranking changes. Pushing a `v*` tag triggers
`.github/workflows/release.yml`: PyInstaller on windows-latest builds both exes and
attaches DownTheRabbitHole-win64.zip (exes + README + LICENSE + START-HERE.txt) to the
GitHub release — README's no-Python install path points at it. run.bat also runs
setup.py first when dict.sqlite is missing.

UI verification: use the preview tools with `.claude/launch.json` server **"texthooker-verify"
(port 6973)** — port 6972 ("texthooker") may be held by another session. Useful eval helpers:
`addLine("日本語テキスト")` injects a line; click a `.token.word` to pin the popup.

## Architecture

```
game ──embedded TextractorCLI (hook.py)──┐
game ──Textractor → clipboard (ctypes)───┤→ publish_line() → logs/ + SSE /events → browser
Textractor/Agent WS servers :6677/:9001 ─┤                            │ kuromoji tokenizes in-browser
game window ──screen OCR (ocr.py)────────┤   dict.sqlite ←── /scan ──┘ hover popup
.epub ── book.py, /book/next per keypress┘
emulator (PPSSPP/PCSX2/Vita3K/yuzu…) ── Agent GUI (agent/, launched via /agent/start) ──:9001↑
```

- **server.py** — everything server-side: clipboard poller, websocket *client* (stdlib RFC 6455,
  connects OUT to Textractor plugin :6677 / Agent :9001), Textractor driver glue, HTTP routes,
  SSE broadcast, dictionary lookup + ranking. Routes: `/scan` (ranked longest-match lookup — the
  core), `/lookup`, `/search` (English→Japanese reverse lookup: FTS5 `gloss_fts` MATCH, ranked
  first-sense word-boundary match > any-sense > mid-gloss, then commonness; leading "to " is
  stripped from query and gloss alike so "to eat" ranks like "eat"; fetches ALL matches —
  bm25 preselection cut 刀 from "sword"; returns scan-shaped candidates; graceful error when the
  index is missing), `/kanji`, `/events` (SSE), `/state`, `/pause`, `/processes` `/hooks` `/attach`
  `/detach` `/hookpick` (game hooking), `/ocr` `/ocr/region` `/ocr/start` `/ocr/stop` (OCR
  fallback), `/agent` `/agent/start` (emulator hooking: Agent by 0xDC00, downloaded by
  `setup.py --agent` into agent/ [gitignored], spawned as a GUI child — the user picks the
  game script + attaches in Agent's own window; text arrives via the existing :9001 WS
  client, whose live status is `WS_CONNECTED`; Agent also copies lines to the clipboard,
  publish_line's consecutive-repeat drop dedupes the double feed; Agent's WS server only
  starts listening once attached, so "running but not connected" is the normal state
  right after launch), `/snap` (base64 PNG of the whole game window for Anki cards — window under the OCR
  region's center, else the hooked pid's biggest visible window, else null; stdlib PNG encoder in
  ocr.py, GDI alpha must be forced to 255 or the PNG is transparent), `/anki` (proxy to
  AnkiConnect :8765 — CORS workaround), `/export` (writes exports/*.txt + opens Explorer —
  WebView2 can't blob-download), `/logsearch` (kana-insensitive substring over logs/*.txt,
  newest file first, cap 200 — Ctrl+F only sees the reader's 300 kept lines; the find bar's
  "logs" button shows hits in a pinned popup, click loads the line back into the reader),
  `/audio` (JapanesePod101 word-audio proxy for the popup's ♪ — the service answers EVERY
  query with something, a missing word returns a fixed 52,288-byte "not available" clip
  that must 404, size gate in fetch_audio), `/book` `/book/import` `/book/open`
  `/book/pos` `/book/close` (e-book reader: import parses via book.py and persists
  {title, lines} to books/<title>.json [gitignored] + per-book position in
  books/progress.json; open returns the book's FULL lines array — the client shows
  it PAGED, Kindle-style [bookMode in app.js: one page in the DOM at a time,
  fillForward fills the pane line-by-line until it overflows, bookTurn flips
  whole pages via edge clicks / arrows / Space / PageUp+Down / wheel / touch
  swipe, zones+arrows+swipe flip in vertical mode; pos = the page's first line,
  saved to /book/pos per turn; footer pill + Book-panel jump slider/number show
  PAGE numbers from a page map (buildPageMap: same fillForward run in an
  offscreen measurer sized/styled to the real pane, walking the whole book,
  cached by a layout fingerprint = book/vertical/paneW/paneH/fontSize/
  lineHeight/furi/tokenizer, rebuilt on resize + vertical + font/line-height/
  furigana change via refreshBookLayout, yields to the event loop so a long
  novel never freezes); resize + vertical toggle re-page from the same pos;
  session restores from localStorage on close]. `#lines.paged` in style.css turns the chip UI into BOOK typography —
  centred 46em column, continuous justified prose, 1em paragraph indents, no
  per-line padding/gaps/hover fill — and plays a direction-aware slide on turn
  (.turn-fwd/.turn-back, reversed under .vertical). Nothing goes through
  publish_line — book text never touches SSE, logs/ or the session),
  `/qr` (PNG QR of LAN_URL else SERVER_URL —
  qr.py: stdlib byte-mode QR, versions 1-5, EC L, mask 0; format-bit PLACEMENT is the trap,
  it's the transpose of the data orientation — test_qr.py's fixture was frozen after
  matching the `qrcode` reference lib module-for-module and decoding with jsQR),
  `/favicon.ico` (static/favicon.ico, stdlib-generated ICO wrapping a PNG). **Every request
  passes `request_allowed`** (server.py): CSRF/DNS-rebinding guard — Host, and Origin when
  a browser sends one, must be localhost / *.local / a non-global IP (`ipaddress.is_global`
  covers loopback+RFC1918+link-local+CGNAT, so Tailscale phones work); Origin "null" and
  public hosts get 403. Closes drive-by localhost POSTs incl. the /anki relay. Matrix in
  test_text.py. `/state` also carries `lan_url` (populates the Settings QR row when --lan).
- **ocr.py** — OCR fallback for unhookable games: tkinter drag-a-box region picker (run as a
  SUBPROCESS — must not share a main thread with pywebview; frozen builds re-invoke the exe
  with `--pick-region`; the overlay spans the whole VIRTUAL screen via overrideredirect +
  SM_*VIRTUALSCREEN — "-fullscreen" only covered the primary monitor; canvas coords =
  screen − (vx,vy), and /ocr/region takes a non-blocking lock so a double-click can't stack
  two overlays), ctypes GDI region screenshot → BMP. Engine = `HybridOcr` when
  manga-ocr is installed: Windows.Media.Ocr (persistent PowerShell worker) locates text —
  per-line + per-word bounding boxes — and manga-ocr reads each line as chunks stacked
  vertically into a near-square canvas (its ViT resizes input to 224x224 — whole
  screenshots hallucinate, wide thin lines garble, isolated small chunks lose sentence
  context and swap plausible wrong chars, 海水→海２; max 6 rows/canvas, more starves
  resolution). Spans tile the line CONTIGUOUSLY (gap midpoints) so a word box Windows
  missed can't leave a glyph out of every crop; every interior cut is then nudged to the
  least-inky pixel column nearby (a cut through a glyph doubles it: 空→空空 — deterministic,
  so the confirm gate can't catch it), and the outer span edges extend past the line bbox
  (Windows routinely misses the trailing 。box). If the first manga read matches the Windows
  text EXACTLY, that's the answer — two independent engines agreeing beats any amount of
  manga re-reading (1 model call, ~0.8s, the common case on clean fonts). Otherwise
  multi-chunk lines are read TWICE with seams in different places; if the reads disagree
  (decoder drops a glyph at a row seam, まもなく→もなく) the Windows text arbitrates by
  similarity — Windows garbles shapes but rarely misses that a char exists. Near-tie
  similarity (within ~0.05, rounded to 1 decimal) falls to DICTIONARY COVERAGE
  (`_dict_coverage`: greedy longest-match segmentation into ≥2-char dict.sqlite words,
  de-inflection included) — a seam-dropped もなく stops segmenting where まもなく doesn't;
  it's a semantic signal independent of both engines, so it works exactly where Windows
  garbled the disputed spot. The same scorer sharpens `_same_line`: a read just below the
  similarity threshold whose coverage GAP vs the other is ≥0.3 (gap, not absolute — garble
  still hits stray words, 行つた covers via つた) is a garbled re-read, not a new line.
  Coverage degrades to None without dict.sqlite — OCR must never require it. When the picked read differs from Windows by 1-2
  single-char substitutions (both manga reads misread the same glyph: 空→望), a tight
  ~3-glyph crop around the spot gets a context-free re-read as the third opinion; the
  Windows char wins only when that local read confirms it (Windows alone never
  overrides — it garbles ブ→プ). When the read barely resembles the Windows text at all
  (<0.4 similarity — dark flash frames made manga read 「うぐっ！？」 as ．．．), the line
  retries with transformed canvases (2x upscale / inverted / both) and keeps the best;
  if all whiff, the line is SKIPPED — publishing the Windows garble read worse than a missing line. Lines under 55% of the tallest are dropped as
  furigana. _has_japanese needs a THIRD of letters Japanese, not one char — a single
  glyph misread as kanji (ⅱ冊) used to publish fullwidth transcriptions of other
  windows when the region was uncovered. Reading order is row-clustered, not (y, x)-sorted:
  Windows splits one visual line into fragments with px-level y jitter (でかい……そして
  split at the ellipsis came back scrambled), so fragments cluster into rows by
  vertical-center proximity, rows top-down then left-to-right, and same-row fragments
  within 2×height merge back into ONE line (single canvas, full sentence context).
  Canvases are
  cached by pixel hash (LRU 256) — NVL screens accumulate text, so an unchanged line
  costs nothing (~0.7s per new line, ~2s cold frame). Frame-change detection is
  TEXT-level, not pixel-level: when the pixel hash changes, a cheap `peek` (Windows OCR
  only, ~0.15s) answers "did the TEXT change?" — blinking click-cursors and animated
  backgrounds churn pixels forever (the old pixel-settle loop stalled 2s on every
  blink), text doesn't. A new peek must REPEAT within the last 2 distinct peeks before
  manga-ocr runs — not exact-consecutive: a blinking cursor makes Windows alternate two
  readings (だから/たから) and consecutive matching starved such lines forever, while
  mid-transition garbage peeks differently every poll and still never qualifies. An
  unconfirmed peek forces re-peeking even when pixels freeze (post-fade lines died
  pending). Engines expose peek(): WindowsOcr = its read; bare MangaOcr returns None →
  loop confirms on the expensive read instead. /ocr state carries a live trace
  (last peek/read/published + per-line win/r1/r2/pick/rescued) for debugging skipped or
  misread lines against the RUNNING app — check it before theorizing. FIELD DEBUG DATA
  persists on this PC (never in git — logs/ is gitignored): every OCR decision appends
  to `logs/ocr-debug-YYYY-MM-DD.jsonl` (events: read [with per-line traces + saved-frame
  name], publish, jitter_drop [text + what it deduped against], merge, gate_drop), and
  frames the pipeline found suspicious — a whole-line whiff, a rescue, a seam
  disagreement — are copied to `logs/ocr-frames/*.bmp` (post-upscale, the exact model
  input; capped at 40, oldest pruned). PURPOSE: when the user reports a misread or a
  missing line after a real session, READ THESE FILES FIRST — the jsonl says what was
  decided and why, the matching frame reproduces it offline, and together they are
  ready-made regression fixtures and the ground truth for tuning the eyeballed
  thresholds (_same_line 0.12 band / 0.3 coverage gap, rescue 0.4/0.35). No Japanese from
  Windows = frame skipped — manga-ocr is
  generative, NEVER run it ungated on raw frames. Without manga-ocr installed: plain
  Windows OCR (WinRT types need
  explicit `[Type,Assembly,ContentType=WindowsRuntime]` activation lines — missing one fails
  before READY). Clipboard source drops non-Japanese text (copied paths/hashes). Re-reads of the same on-screen line RECONCILE
  via `_merge_reads` (ocr.py, mirrored as `mergeReads` in app.js — `test_merge.py` locks
  the two in parity, run it whenever either changes): containment picks the
  fuller read, a head/tail overlap ≥ half the shorter read splices reads that each missed
  a different end; the merged superstring republishes and the reader swaps it in place of
  the partial (no stacked near-duplicates). manga-ocr dot-runs normalize to …… in _clean.
  Pixel-hash skips unchanged frames; a line publishes only after two identical
  consecutive reads (typewriter-animation filter). Region persists in `ocr_region.json`
  (gitignored).
- **hook.py** — drives `textractor/x64|x86/TextractorCLI.exe` as a child process (UTF-16-LE
  stdout, one line per sentence). One attached game at a time; user picks the hook channel.
- **setup.py** — downloads everything and builds `dict.sqlite` (gitignored). Tables: `entries`,
  `terms`, `names`, `nameterms`, `kanji`, `gloss_fts` (FTS5 over glosses, rowid = entry id;
  built FROM the entries table so re-running setup upgrades an existing db without
  redownloading). Each setup step is idempotent (skips if present).
- **deinflect.py + deinflect_data.py** — Yomitan's de-inflection rule table (GPL-3.0!) ported;
  `deinflect_data.py` is generated, don't hand-edit.
- **qr.py** — minimal stdlib QR encoder for `/qr` (see the route note above). Self-contained;
  `python qr.py` prints a test matrix as ASCII.
- **book.py** — e-book → text lines for the book reader (see `/book*` routes above).
  `parse_book(data, filename)` dispatches on content magic first, then extension:
  PK zip = epub (container.xml → OPF spine order → html.parser), falling back to a
  .fb2 inside the archive (fb2.zip); BOOKMOBI/TEXtREAd magic = **Kindle .mobi/.azw/
  .azw3/.prc** — PalmDB records, stdlib PalmDOC-LZ77 decompression (`_palmdoc_decompress`),
  per-record trailing-entry trim via the MOBI extra-data flags, cp1252/utf-8 from the
  MOBI header; DRM'd and HUFF/CDIC-compressed books get a clear "convert with Calibre"
  ValueError (never garbage); .fb2 = FictionBook XML (`<p>/<v>/<subtitle>` per body);
  .html = one document through the same extractor; .txt = line-per-line with Aozora
  Bunko markup handling (｜base《ruby》 ruby, ［＃…］ notes, the ---- 記号 block, the
  底本： footer) and UTF-8→cp932 encoding fallback; .pdf/.kfx stay "convert with
  Calibre". One block element (`<p>`, `<h1>`…, `<br>`) / text line = one
  reader line; `<rt>`/`<rp>` and Aozora 《》 (ruby furigana) never leak — the reader
  adds its own furigana from kuromoji, baked-in readings would double up.
  `test_book.py` covers it.
- **static/app.js** — tokenizes lines with kuromoji.js in the browser, wraps tokens in hoverable
  spans, fetches `/scan` per hover (cached), renders the popup (kanji cards, ♪ word audio via
  `/audio`, Anki export — attaches the `/snap` game-window screenshot to the card's Sentence
  field, best-effort, hide-names, romaji→kana for the lookup box), session persistence in
  localStorage. Lookup box
  falls back to English (`/search`) when input is ASCII-but-not-valid-romaji or valid romaji
  that finds no Japanese entry. Ctrl+F find bar: kana-insensitive substring over each line's
  `dataset.raw`, line-level highlight, Enter/Shift+Enter cycle from the newest match; its
  "logs" button searches every past session via `/logsearch`. Clear resets the stats counter
  (chars measure THIS session). UI text sticks to basic-plane chars (♪ not 🔊) — WebView2
  renders emoji as tofu.
- **static/settings.js** — loaded blocking in `<head>` so the saved theme applies pre-paint.
  Theme = 6 CSS custom properties; every other colour derives via `color-mix()` in style.css.
  Also owns the **vertical text (tategaki) toggle** (縦 next to B/I): a `.vertical` class
  on `<html>`, persisted with the rest of the appearance settings. style.css flips
  `#lines` to `writing-mode: vertical-rl` — `.line` uses LOGICAL props
  (`inline-size`/`max-inline-size`/`margin-inline`) so its sizing flips for free; app.js
  handles the flipped auto-scroll axis (Chrome's scrollLeft is NEGATIVE in vertical-rl,
  newest line leftmost); toggling dispatches `vntex-vertical-toggle` and app.js
  re-anchors the scroll (book position, or the newest session line).
  The Settings panel's "Read on your phone" QR row (index.html #lanRow) is populated by
  app.js from `/state`'s lan_url — hidden unless the server runs with --lan.
- **LICENSE** — GPL-3.0 for the whole project (forced by the Yomitan-ported
  deinflect_data.py; Textractor is GPL too). Don't add GPL-incompatible code/data.

### Ranking (the heart of `/scan`, server.py)

"Longest *plausible* match, anchored on the tokenizer's segmentation." One sort key
(`_sort_key`) decides everything; a longer match only beats the tokenizer's own token when the
longer word is itself common (JMdict-common flag or VN rank ≤ 6600) — OR via the BOUNDARY
RESCUE: a rare longer match keeps its length when its extension past the token contains
kanji AND the match ends at a natural boundary (`_BOUNDARY_AFTER`: particles/copula/punct
or line end) — 生返事 #16,624 hover 生 in 生返事だった. Kana extensions never rescue: the
over-match trap class swallows particles (底荷「そこに」+ は passes the boundary test but
not the kanji test). A kana-written hover
prefers the usually-kana homograph (JMdict `uk` on the first sense — the "uk boost"), ranked
below the reading-priority tiers so 豆「まめ」still beats the rare uk 忠実「まめ」. KATAKANA
tokens get two special rules (western names were a weakness): a pure-katakana token drops
all sub-token matches outright (katakana isn't agglutinative — hover テグジュペリ used to
show 大邱「テグ」"Daegu"; honest no-match beats a confident wrong one), and the
word-beats-name tier only holds for katakana when the word is `_established` — a rare
loanword ties with the name and later tiers decide (レオン the Sierra-Leone currency no
longer buries Leon, while カメラ still beats the name Camera). Frontend
passes each hovered token's POS/reading/base/surface from kuromoji to drive it. Any change here
must keep `python test_ranking.py` green — those cases encode fixed bug classes (over-extension
into the next particle, names burying real words, kana homographs, katakana prefix garbage).

## Hard-won gotchas (don't rediscover these)

- **Zombie servers**: `allow_reuse_address = False` + `os._exit(0)` on window close / Ctrl+C are
  deliberate. Windows lets a second bind on a busy port silently route requests to the OLD
  process — this caused hours of "new feature 404s" confusion. Don't remove either.
- **WebView2 (pywebview) quirks**: no blob downloads (hence server-side `/export`), caches
  static files (hence `Cache-Control: no-cache` by default), and misses emoji-range
  glyphs (⬇ rendered as tofu — stick to basic chars like ↓ in UI text).
- **Cache policy is two-tier**: `/static/kuromoji/` and `/static/fonts/` get
  `max-age=31536000, immutable` (26 MB of never-changing assets — re-fetching them every
  launch was the main startup cost); everything else stays `no-cache`. Keep new immutable
  assets under those two dirs. Client `lookupCache` is FIFO-capped (500); server `scan`
  lru_cache is 1024.
- Kuromoji dict files are stored gunzip-named `.dat` (still gzipped) — IDM download managers
  hijack `*.gz` URLs from the webview.
- Frozen (PyInstaller) builds resolve `BASE_DIR` from `sys.executable`; data (static/,
  dict.sqlite, textractor/) lives NEXT TO the exe, not inside it.
- Console printing: wrap in `sys.stdout.reconfigure(errors="replace")` — user consoles are cp932.
- `_serve_events` uses manual chunked transfer encoding; heartbeat comment every 15s.
- **POST bodies must be drained**: the server is HTTP/1.1 keep-alive, so a POST route
  that never reads its request body leaves it on the socket and the NEXT request on
  that connection parses the leftover bytes as a method line → 501 for everything
  after. `do_POST` reads the whole body into `self._post_body` before routing — new
  routes must use `self._post_body` / `_read_json_body()`, never `self.rfile.read`.

## Features removed on user request — do NOT re-add without asking

Pitch accent (whole feature incl. Kanjium table), known-words tracking (marking/dimming/
import-export), Tanaka example sentences, per-line coverage %, furigana-unknown-only mode,
pitch-accent contour graph. History in NEXT.md.

## Conventions

- User communicates casually; commit after each feature batch and **push** (they ask for it
  every time). Commit messages: normal English, explain the "why".
- Stats/UI text in English (user rejected 字/字時).
- All UI colours must derive from the 6 theme vars (`--bg --text --accent --accent-2 --pos
  --danger`) — never hardcode a colour in style.css except the status dot and the
  theme-agnostic lighting effects (`--shadow`, `--glass-spec`).
- UI style is "liquid glass": chrome surfaces (toolbar/popup/panels/hint) are translucent
  `--glass-*` + backdrop-filter over an ambient body gradient; a no-backdrop-filter
  fallback at the end of style.css keeps them solid. Reader lines stay non-glass.
- README.md and NEXT.md are kept in sync with feature changes; NEXT.md lists backlog +
  removed-features memory.
- Licences matter: deinflect_data.py is GPL-3.0 (from Yomitan), Textractor GPL-3.0,
  JMdict/JMnedict/KANJIDIC2 EDRDG, VN frequency CC BY-SA. Note new data sources in README.
