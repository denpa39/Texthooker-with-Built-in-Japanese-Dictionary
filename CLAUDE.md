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
  core), `/lookup`, `/kanji`, `/events` (SSE), `/state`, `/pause`, `/processes` `/hooks` `/attach`
  `/detach` `/hookpick` (game hooking), `/ocr` `/ocr/region` `/ocr/start` `/ocr/stop` (OCR
  fallback), `/anki` (proxy to AnkiConnect :8765 — CORS workaround), `/export` (writes
  exports/*.txt + opens Explorer — WebView2 can't blob-download).
- **ocr.py** — OCR fallback for unhookable games: tkinter drag-a-box region picker (run as a
  SUBPROCESS — must not share a main thread with pywebview; frozen builds re-invoke the exe
  with `--pick-region`), ctypes GDI region screenshot → BMP. Engine = `HybridOcr` when
  manga-ocr is installed: Windows.Media.Ocr (persistent PowerShell worker) locates text —
  per-line + per-word bounding boxes — and manga-ocr reads tight crops of each line, split
  into ≤6:1-aspect chunks at word boundaries (its ViT resizes input to 224x224; whole
  screenshots make it hallucinate, wide thin lines garble). Lines under 55% of the tallest
  are dropped as furigana. Chunk reads are cached by crop pixel hash (LRU 512) — NVL screens
  accumulate text, so an unchanged line costs nothing on the next frame (~0.8s per new line
  vs ~4s cold). No Japanese from Windows = frame skipped — manga-ocr is
  generative, NEVER run it ungated on raw frames. Without manga-ocr installed: plain
  Windows OCR (WinRT types need
  explicit `[Type,Assembly,ContentType=WindowsRuntime]` activation lines — missing one fails
  before READY). Clipboard source drops non-Japanese text (copied paths/hashes). Pixel-hash skips unchanged frames; a line publishes only after two identical
  consecutive reads (typewriter-animation filter). Region persists in `ocr_region.json`
  (gitignored).
- **hook.py** — drives `textractor/x64|x86/TextractorCLI.exe` as a child process (UTF-16-LE
  stdout, one line per sentence). One attached game at a time; user picks the hook channel.
- **setup.py** — downloads everything and builds `dict.sqlite` (gitignored). Tables: `entries`,
  `terms`, `names`, `nameterms`, `kanji`. Each setup step is idempotent (skips if present).
- **deinflect.py + deinflect_data.py** — Yomitan's de-inflection rule table (GPL-3.0!) ported;
  `deinflect_data.py` is generated, don't hand-edit.
- **static/app.js** — tokenizes lines with kuromoji.js in the browser, wraps tokens in hoverable
  spans, fetches `/scan` per hover (cached), renders the popup (kanji cards, Anki export,
  hide-names, romaji→kana for the lookup box), session persistence in localStorage.
- **static/settings.js** — loaded blocking in `<head>` so the saved theme applies pre-paint.
  Theme = 6 CSS custom properties; every other colour derives via `color-mix()` in style.css.

### Ranking (the heart of `/scan`, server.py)

"Longest *plausible* match, anchored on the tokenizer's segmentation." One sort key
(`_sort_key`) decides everything; a longer match only beats the tokenizer's own token when the
longer word is itself common (JMdict-common flag or VN rank ≤ 6600). A kana-written hover
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
