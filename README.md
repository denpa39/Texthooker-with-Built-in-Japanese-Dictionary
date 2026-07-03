# Down the Rabbit Hole

*Copy a line of Japanese, fall in, and read your way through.* A visual-novel
**texthooker** with a **built-in offline Japanese→English dictionary**.
It watches your Windows clipboard for Japanese text (e.g. extracted from a game by
[Textractor](https://github.com/Artikash/Textractor)), shows it in a clean reader,
and gives you a **hover dictionary** — point at any word to see its meaning, reading,
and part of speech, Yomitan-style.



---

## Quick start

```sh
# (recommended) better word-frequency ranking during the build
python -m pip install wordfreq

# 1. One-time setup — downloads the kuromoji tokenizer, JMdict, and the
#    JMnedict name dictionary, then builds the database.
python setup.py            # (or double-click setup.bat)
#    python setup.py --no-names   # skip names to keep the DB small (~74 MB vs ~250 MB)

# 2. Run it
python server.py           # (or double-click run.bat)
```

The app opens in **its own window** (no browser needed — falls back to your browser
if `pywebview` isn't installed). Copy some Japanese text and it appears instantly.
Hover a word for its definition.

### Using it with a game (built-in hooking — no external Textractor)

1. Start your visual novel.
2. Click **Attach** in the toolbar and pick the game from the list.
3. Advance the game a line or two — its text channels appear in the panel.
   Click the one showing the actual dialogue. Done: every line streams in.

Setup downloads Textractor's hook engine automatically and the app drives it
internally (`textractor/`, GPL-3.0). The old way still works too: run Textractor
yourself with its **Copy to Clipboard** extension — the app watches the clipboard
as a fallback.

**Tip:** click a word to pin its popup open; press **Esc** to close. Hover gives a quick peek.

### Visual-novel frequency ranking (optional, recommended for VN readers)

By default, ranking uses a general-corpus frequency (`wordfreq`). For VN-accurate
ranking, import a **visual-novel frequency list** from
[jiten.moe](https://jiten.moe/other):

1. Go to **https://jiten.moe/other**, pick **Visual Novel**, and download the
   **Yomitan** frequency dictionary (a `.zip`). Save it into this folder.
2. Rebuild with it:
   ```sh
   python setup.py --skip-kuromoji --force --freq jiten_vn.zip
   ```

Now lookups prefer the reading that's common in visual novels, and a longer match is
only chosen over a shorter common word when it's itself a common VN word — so 一日中
and という still win, but a rare homograph like 底荷「そこに」"ballast" never buries
そこ "there". Frequency data: jiten.moe, **CC BY-SA 4.0**. (Definitions remain
JMdict/JMnedict — the same sources jiten itself uses.)

**No-manual-step alternative:** `python setup.py --skip-kuromoji --force --innocent`
auto-downloads the **Innocent Corpus** VN/novel frequency list instead. It's convenient
but coarser — it omits some very common function words and under-counts する — so the
hand-downloaded jiten.moe VN list above is recommended. The frequency loader
auto-detects rank-style vs. count-style lists, so any Yomitan frequency `.zip` works
with `--freq`.

---

## Features

- **Auto clipboard capture** over Server-Sent Events — text appears the moment it's copied,
  with the classic Textractor artifacts (whole line doubled, every character doubled)
  cleaned automatically.
- **Offline dictionary** built from JMdict (the standard free J–E dictionary). Works with no internet after setup.
- **Smart longest-match scanning** — catches multi-word expressions and compounds the
  tokenizer splits (一日中, という), with a full de-inflector that also shows the
  inflection trail (読んでいた → 読む · -te form › -いる › past).
- **Yomitan-grade de-inflection** — the rule table is ported from
  [Yomitan](https://github.com/yomidevs/yomitan)'s Japanese transforms (850+ rules with
  grammatical condition chaining), plus auxiliary chains it leaves to multi-word lookup
  (〜てみる, 〜ていく, 〜てくれる, 〜にくい/やすい…). 食べさせられていた, わからん,
  高くなさそう, お読みになります — all reach their dictionary form.
- **Kanji info cards (KANJIDIC2)** — in a pinned popup, click any kanji in the headword
  for its meanings, on/kun readings, stroke count, school grade, JLPT level and
  frequency rank.
- **Hide names** — a popup toggle that filters JMnedict name clusters out of lookups
  (they reappear with one click, and a pure-name token still shows its names).
- **Manual lookup box** — type or paste any word in the toolbar and press Enter; no
  hooking needed. Romaji works too (tabemasu → たべます).
- **VN frequency chip** (№1,638) in the popup so you can tell at a glance whether a word
  is worth mining; green = common in visual novels.
- **Anki export** — the ★ button on any entry adds a card (word, reading, meanings,
  source sentence) via [AnkiConnect](https://ankiweb.net/shared/info/2055492159).
  Deck name is configurable (Settings → Study), the note type is created automatically,
  a one-time "Anki ✓ / Anki –" toolbar indicator shows whether Anki is reachable, and
  a duplicate card answers with "dup" instead of a generic error.
- **Session persistence & export** — lines survive a page reload, and **Export** saves
  the session to `exports\` and reveals it in Explorer. A live counter shows characters
  read and reading speed (chars/hr).
  The server also appends every line to a per-day file in `logs/` (named after the
  hooked game), so nothing is lost to a browser-storage wipe.
- **WebSocket input** — reads text straight from Textractor's websocket plugin (:6677)
  or Agent (:9001) with no clipboard round-trip; `--ws` configures or disables it.
- **LAN mode** — `python server.py --lan` prints a LAN URL so you can read along on a
  phone or tablet while the game runs on the PC (the layout adapts to small screens).
- **Tokenizer-anchored ranking** so the intended word comes first. It trusts kuromoji's
  own analysis of each token — part-of-speech (は = particle, not 羽 "feather"), reading
  (本【ほん】"book", not 本【もと】"origin"), dictionary form (居る "to be", not 射る
  "to shoot") — and only lets a *longer* match win when that longer match is itself a
  common word. So そこ stays "there" (not the rare 底荷 "ballast"), 村 stays "village"
  (not a surname), while real compounds like 一日中 and という are still caught.
- **Name dictionary (JMnedict)** — recognizes character and place names (田中 → "Tanaka"),
  ranked below a real word of the same length.
- **Polished popup** — click a word to pin it open, a copy button, and a Jisho.org link;
  hover for a quick peek.
- **Furigana toggle**, **text alignment** (left / center / right / justify), adjustable
  font size with **bold/italic** toggles, **themes** (six Wonderland palettes plus
  custom colours & fonts), pause/resume capture, undo, and clear.
- A flat, editorial reading UI — every colour (including the toolbar fill) is derived
  from the active theme, so the whole interface re-themes consistently.
- Everything runs locally on `127.0.0.1`; nothing is sent anywhere, and no account or payment is needed.

---

## Toolbar

| Control | What it does |
|---|---|
| status dot | Connection state at a glance — green = ready, orange = paused, red = disconnected (hover for the label) |
| lookup box | Type a word + Enter (romaji OK) — dictionary popup without hooking |
| **Attach** | Hook a running game directly (embedded Textractor) — pick the process, then the text channel |
| **Pause** / **Resume** | Stop/continue capture (clipboard + websocket) |
| **Furigana** | Show readings above kanji |
| ▤ alignment icons | Text alignment — left, center, right, or justify |
| **Settings** | Opens the settings panel (themes, colours, font, text size, bold/italic, spacing, Anki deck) |
| A slider | Reader font size (also in Settings → Text size) |
| **Export** | Download the session as a .txt file |
| **Undo** | Remove the most recent line |
| **Clear** | Clear all lines (click twice to confirm) |
| chars counter | Characters read this session · reading speed (chars/hr) |

### Settings panel

Click **Settings** to open it:

- **Theme** — six Wonderland palettes (Alice, Caterpillar, Cheshire, Mad Hatter, Queen
  of Hearts, White Rabbit). Picking one recolours everything — toolbar, popup, reader,
  highlights — since every surface derives its colour from the same six theme variables.
- **Colours** — override any individual swatch (background, text, accent, reading,
  tag, furigana) on top of the current theme.
- **Font** — pick a font stack (sans, Gothic, Mincho serif, rounded, or monospace); use
  the toolbar's **A** slider for size.
- **Spacing** — line height and furigana size sliders.
- **B** / *I* — toggle bold or italic for the reader text.
- **Reset to default** — clears every override back to the default theme.

All of the above is saved to your browser's local storage and restored automatically
next time you open the page — no flash of the wrong theme on load.

---

## Setup options

```sh
python setup.py --common     # smaller "common words only" dictionary
python setup.py --force      # redownload + rebuild everything
python setup.py --skip-kuromoji   # only rebuild the dictionary DB
```

## Server options

```sh
python server.py --port 7000     # use a different port
python server.py --lan           # phone/tablet mode: listen on the network, print the LAN URL
python server.py --ws ws://127.0.0.1:6677   # websocket text sources (default: Textractor :6677 + Agent :9001; '' disables)
python server.py --browser       # open in the web browser instead of the app window
python server.py --no-browser    # serve only; open nothing
python setup.py --textractor     # (re)download only the embedded Textractor
python setup.py --no-textractor  # skip Textractor during setup
```

## Packaging (one-exe for non-Python users)

`build_exe.bat` builds `DownTheRabbitHole.exe` + `RabbitHoleSetup.exe` with
PyInstaller. Ship both in one folder: the user runs the setup exe once (it
downloads the tokenizer, dictionaries and Textractor next to itself), then the
app exe. The data stays outside the exe, so dictionary rebuilds don't mean
re-downloading the app.

---

## How it works

```
 game ──Textractor──> Windows clipboard
                           │  (ctypes polling)
                      server.py ──SSE──> browser
                           │                 │ kuromoji tokenize
                       dict.sqlite <─lookup──┘ hover popup
```

- `setup.py` downloads kuromoji.js + its dictionary into `static/kuromoji/`, and
  builds `dict.sqlite` (entries + a term index) from JMdict.
- `server.py` polls the clipboard, streams new text to the page, and serves
  longest-match lookups from the SQLite dictionary via `/scan` (de-inflecting and
  ranking candidates so the intended word comes first).
- `static/app.js` tokenizes each line in the browser, wraps words in hoverable
  spans, and fetches definitions on demand.

## Notes

- Clipboard monitoring is Windows-only (uses the Win32 clipboard API). The UI and
  dictionary still work on other platforms — paste text manually.
- The dictionary database and downloaded tokenizer are not committed; run
  `setup.py` to generate them.
- **Anki export** needs Anki running with the AnkiConnect add-on installed (code
  `2055492159`). The first export creates a "Down the Rabbit Hole" deck and note type.

## Data & licences

- Definitions: **JMdict / JMnedict** (EDRDG licence).
- De-inflection rules: ported from **Yomitan**'s Japanese transforms — **GPL-3.0**
  (`deinflect_data.py`; if you redistribute this project, GPL-3.0 terms apply to it).
- Game hooking: **Textractor** — GPL-3.0 (downloaded to `textractor/`, driven as a
  separate process).
- VN frequency: **jiten.moe** — CC BY-SA 4.0.
- Kanji info: **KANJIDIC2** (EDRDG licence).
