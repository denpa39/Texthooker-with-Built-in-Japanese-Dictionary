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
as the default engine and REMOVED: it hallucinates full Japanese sentences from
no-text frames (generative model, never returns empty). Don't re-enable without
gating it behind a cheap text-presence check (e.g. Windows OCR first, manga-ocr
re-read only when Windows found text in the region).

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
