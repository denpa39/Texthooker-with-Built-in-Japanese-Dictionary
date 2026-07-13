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
                                 # Textractor; builds dict.sqlite (idempotent; --force rebuilds)
python test_ranking.py           # lookup-ranking regression tests (needs dict.sqlite)
python test_merge.py             # _merge_reads (py) vs mergeReads (js) parity (needs node)
python deinflect.py              # de-inflector self-test (41 cases)
node --check static/app.js       # JS syntax check (no other lint/build step exists)
build_exe.bat                    # PyInstaller one-file exes (app + setup)
```

UI verification: use the preview tools with `.claude/launch.json` server **"texthooker-verify"
(port 6973)** — port 6972 ("texthooker") may be held by another session. Useful eval helpers:
`addLine("日本語テキスト")` injects a line; click a `.token.word` to pin the popup.

## Architecture

```
game ──embedded TextractorCLI (hook.py)──┐
game ──Textractor → clipboard (ctypes)───┤→ publish_line() → logs/ + SSE /events → browser
Textractor/Agent WS servers :6677/:9001 ─┤                            │ kuromoji tokenizes in-browser
game window ──screen OCR (ocr.py)────────┘   dict.sqlite ←── /scan ──┘ hover popup
```

- **server.py** — everything server-side: clipboard poller, websocket *client* (stdlib RFC 6455,
  connects OUT to Textractor plugin :6677 / Agent :9001), Textractor driver glue, HTTP routes,
  SSE broadcast, dictionary lookup + ranking. Routes: `/scan` (ranked longest-match lookup — the
  core), `/lookup`, `/search` (English→Japanese reverse lookup: FTS5 `gloss_fts` MATCH, ranked
  first-sense word-boundary match > any-sense > mid-gloss, then commonness; fetches ALL matches —
  bm25 preselection cut 刀 from "sword"; returns scan-shaped candidates; graceful error when the
  index is missing), `/kanji`, `/events` (SSE), `/state`, `/pause`, `/processes` `/hooks` `/attach`
  `/detach` `/hookpick` (game hooking), `/ocr` `/ocr/region` `/ocr/start` `/ocr/stop` (OCR
  fallback), `/snap` (base64 PNG of the whole game window for Anki cards — window under the OCR
  region's center, else the hooked pid's biggest visible window, else null; stdlib PNG encoder in
  ocr.py, GDI alpha must be forced to 255 or the PNG is transparent), `/anki` (proxy to
  AnkiConnect :8765 — CORS workaround), `/export` (writes exports/*.txt + opens Explorer —
  WebView2 can't blob-download).
- **ocr.py** — OCR fallback for unhookable games: tkinter drag-a-box region picker (run as a
  SUBPROCESS — must not share a main thread with pywebview; frozen builds re-invoke the exe
  with `--pick-region`), ctypes GDI region screenshot → BMP. Engine = `HybridOcr` when
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
  (last peek/read/published + per-line win/r1/r2/pick) for debugging skipped or
  misread lines against the RUNNING app — check it before theorizing. No Japanese from
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
- **static/app.js** — tokenizes lines with kuromoji.js in the browser, wraps tokens in hoverable
  spans, fetches `/scan` per hover (cached), renders the popup (kanji cards, Anki export —
  attaches the `/snap` game-window screenshot to the card's Sentence field, best-effort,
  hide-names, romaji→kana for the lookup box), session persistence in localStorage. Lookup box
  falls back to English (`/search`) when input is ASCII-but-not-valid-romaji or valid romaji
  that finds no Japanese entry. Ctrl+F find bar: kana-insensitive substring over each line's
  `dataset.raw`, line-level highlight, Enter/Shift+Enter cycle from the newest match.
- **static/settings.js** — loaded blocking in `<head>` so the saved theme applies pre-paint.
  Theme = 6 CSS custom properties; every other colour derives via `color-mix()` in style.css.

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
below the reading-priority tiers so 豆「まめ」still beats the rare uk 忠実「まめ」. Frontend
passes each hovered token's POS/reading/base/surface from kuromoji to drive it. Any change here
must keep `python test_ranking.py` green — those cases encode fixed bug classes (over-extension
into the next particle, names burying real words, kana homographs).

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
