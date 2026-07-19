# Down the Rabbit Hole

*Copy a line of Japanese, fall in, and read your way through.* A visual-novel
**texthooker** with a **built-in offline Japanese→English dictionary**.
It watches your Windows clipboard for Japanese text (e.g. extracted from a game by
[Textractor](https://github.com/Artikash/Textractor)), shows it in a clean reader,
and gives you a **hover dictionary** — point at any word to see its meaning, reading,
and part of speech, Yomitan-style.



---

## Install

### Easiest — no Python needed (Windows)

1. Download **DownTheRabbitHole-win64.zip** from the
   [latest release](../../releases/latest) and unzip it anywhere.
2. Run **RabbitHoleSetup.exe** once — it downloads the dictionary and tools
   (~250 MB, one time) right next to the exe.
3. Run **DownTheRabbitHole.exe** and start reading.

Forgot step 2? The app notices and offers to run setup for you.

### With Python (3.9+)

Download the source (green **Code** button → Download ZIP, or `git clone`), then
**double-click `run.bat`** — the first run sets everything up automatically and
opens the app. That's the whole install.

The same, by hand:

```sh
python setup.py            # one-time: tokenizer, dictionaries, hook engine,
                           # VN word frequency — every piece skipped if present
python server.py           # run (or double-click run.bat)
```

Setup takes care of the extras itself: it pip-installs `wordfreq` (build-time
ranking data) and `pywebview` (the app window) when they're missing, and picks a
visual-novel frequency list automatically — `jiten_vn.zip` if you've downloaded
one (see below), else the Innocent Corpus. `python setup.py --no-names` keeps the
database small (~74 MB vs ~250 MB).

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

### Emulated games (PSP / PS2 / Vita / Switch…)

Textractor can't hook inside an emulator — [Agent](https://github.com/0xDC00/agent)
(frida-based) can, with per-game scripts for PPSSPP, PCSX2, Vita3K,
yuzu/Ryujinx, Citra, RPCS3 and more from its
[community scripts repo](https://github.com/0xDC00/scripts).

1. `python setup.py --agent` — one-time, downloads Agent (~120 MB) into `agent/`.
2. Open **Attach** → **Emulators** → **Launch Agent**.
3. In Agent's window: update scripts (dropdown), pick the script matching your
   game, drag the crosshair onto the emulator window, **Attach**.

Hooked text flows into the reader automatically (Agent's websocket on :9001 is
one of the app's default text sources, and Agent's clipboard copies are picked
up even before that connects). No script for your game yet? Request one on the
[scripts repo](https://github.com/0xDC00/scripts/issues), or fall back to OCR below.

### OCR mode (for games that won't hook)

Some engines defeat every hook. Plan B, built in:

1. Open **Attach** → **OCR fallback** → **Select text area**, and drag a box over
   the game's *text box only* (skip menus/UI so they don't get read).
2. **Start OCR.** The app watches that area and reads new text automatically —
   partial lines from typewriter animations are filtered out, and the area is
   remembered across restarts.

Recognition uses Windows' built-in Japanese OCR (needs the Japanese language
pack: Settings → Time & Language → Language). If [manga-ocr](https://github.com/kha-white/manga-ocr)
is installed (`pip install manga-ocr`, ~400 MB with torch; with an NVIDIA GPU,
`pip install --force-reinstall torch --index-url https://download.pytorch.org/whl/cu128`
afterwards makes reads ~2-3x faster), it takes over the
actual reading — far more accurate on game fonts. Windows OCR stays on to find
*where* the text is: manga-ocr is a generative model that *hallucinates*
plausible Japanese when fed whole screenshots (background art, multiple lines),
so it only ever sees tight crops of the text lines Windows OCR located, and a
frame with no Japanese found is skipped entirely.

**Tip:** click a word to pin its popup open; press **Esc** to close. Hover gives a quick peek.

### Visual-novel frequency ranking (automatic, upgradeable)

Fresh installs get VN-flavoured ranking out of the box: setup auto-downloads the
**Innocent Corpus** VN/novel frequency list. It's decent but coarse — it omits
some very common function words and under-counts する. For the best ranking,
upgrade to the **jiten.moe visual-novel list**:

1. Go to **https://jiten.moe/other**, pick **Visual Novel**, and download the
   **Yomitan** frequency dictionary (a `.zip`). Save it into this folder as
   `jiten_vn.zip` — setup picks it up automatically from now on.
2. Rebuild with it:
   ```sh
   python setup.py --skip-kuromoji --force
   ```

Now lookups prefer the reading that's common in visual novels, and a longer match is
only chosen over a shorter common word when it's itself a common VN word — so 一日中
and という still win, but a rare homograph like 底荷「そこに」"ballast" never buries
そこ "there". Frequency data: jiten.moe, **CC BY-SA 4.0**. (Definitions remain
JMdict/JMnedict — the same sources jiten itself uses.)

The frequency loader auto-detects rank-style vs. count-style lists, so any Yomitan
frequency `.zip` works via `--freq path.zip`; `--no-vn-freq` skips VN frequency
entirely (general word frequency only).

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
  hooking needed. Romaji works too (tabemasu → たべます), and so does **English**:
  "cherry blossom" searches the dictionary definitions and finds 桜 (full-text index
  over every JMdict gloss, built by setup.py).
- **Find in lines (Ctrl+F)** — search everything you've read this session
  (kana-insensitive, さくら finds サクラ); Enter / Shift+Enter cycle matches. The bar's
  **logs** button searches every *past* session too (the server's `logs/` files) and
  clicking a result loads that line back into the reader, hoverable again.
- **Word audio** — the ♪ button in the popup plays the word's pronunciation
  (JapanesePod101 recordings, the same source Yomitan uses; needs internet, words
  without a recording show ✗).
- **VN frequency chip** (№1,638) in the popup so you can tell at a glance whether a word
  is worth mining; green = common in visual novels.
- **Anki export** — the ★ button on any entry adds a card (word, reading, meanings,
  source sentence) via [AnkiConnect](https://ankiweb.net/shared/info/2055492159).
  When a game is attached or OCR is set up, a **screenshot of the whole game window**
  rides along on the card — the full scene, not just the text box.
  Deck name is configurable (Settings → Study), the note type is created automatically,
  a one-time "Anki ✓ / Anki –" toolbar indicator shows whether Anki is reachable, and
  a duplicate card answers with "dup" instead of a generic error.
- **Session persistence & export** — lines survive a page reload, and **Export** saves
  the session to `exports\` and reveals it in Explorer. A live counter shows characters
  read and reading speed (chars/hr).
  The server also appends every line to a per-day file in `logs/` (named after the
  hooked game), so nothing is lost to a browser-storage wipe.
- **Book reader** — the **Book** toolbar button imports an e-book (`.epub`, Kindle
  `.mobi`/`.azw`/`.azw3`, `.fb2`, `.txt` including Aozora Bunko markup with automatic
  cp932 detection, or `.html`) and serves it one line at a time, like a visual novel:
  **Space**/**→** shows the next line (a floating **Next ▸** button does the same on
  phones), **←** steps back. Ruby furigana in the file is stripped so the reader's own
  hover dictionary and furigana work on clean text. Imported books and your position
  in each are remembered in `books/` across sessions. (DRM-protected Kindle books and
  `.pdf` can't be read directly — convert to epub with
  [Calibre](https://calibre-ebook.com/) first.)
- **Vertical text (tategaki)** — the 縦 toggle in Settings flips the reader to
  right-to-left vertical columns, like a printed novel; furigana, the hover
  dictionary and the book reader all follow (in vertical mode **←** advances the
  book, matching the reading direction). Remembered like every other setting.
- **WebSocket input** — reads text straight from Textractor's websocket plugin (:6677)
  or Agent (:9001) with no clipboard round-trip; `--ws` configures or disables it.
- **LAN mode** — `python server.py --lan` prints a LAN URL so you can read along on a
  phone or tablet while the game runs on the PC (the layout adapts to small screens).
  Settings shows a **QR code** of that address — scan it instead of typing the IP.
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
| lookup box | Type a word + Enter (romaji or English OK) — dictionary popup without hooking |
| **Attach** | Hook a running game directly (embedded Textractor) — pick the process, then the text channel |
| **Book** | Import an e-book (`.epub` / `.mobi` / `.azw` / `.fb2` / `.txt` / `.html`) and read it line by line — Space/→ next, ← back; position remembered per book |
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
- **B** / *I* / 縦 — toggle bold, italic, or vertical (tategaki) reader text.
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
python setup.py --agent          # download Agent (~120 MB) for emulator hooking
```

## Packaging (one-exe for non-Python users)

`build_exe.bat` builds `DownTheRabbitHole.exe` + `RabbitHoleSetup.exe` with
PyInstaller. Ship both in one folder: the user runs the setup exe once (it
downloads the tokenizer, dictionaries and Textractor next to itself), then the
app exe. The data stays outside the exe, so dictionary rebuilds don't mean
re-downloading the app.

Pushing a tag (`git tag v1.0 && git push --tags`) builds the same pair on CI
(`.github/workflows/release.yml`) and attaches **DownTheRabbitHole-win64.zip**
to the GitHub release — that's what the "no Python needed" install path uses.

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

- **This project: GPL-3.0** (see `LICENSE`) — required by the Yomitan-derived
  de-inflection table below.
- Definitions: **JMdict / JMnedict** (EDRDG licence).
- De-inflection rules: ported from **Yomitan**'s Japanese transforms — **GPL-3.0**
  (`deinflect_data.py`; if you redistribute this project, GPL-3.0 terms apply to it).
- Game hooking: **Textractor** — GPL-3.0 (downloaded to `textractor/`, driven as a
  separate process).
- Emulator hooking: **Agent** by 0xDC00 — free closed-source binary (downloaded to
  `agent/`, launched as a separate app; its community game scripts are MIT).
- VN frequency: **jiten.moe** — CC BY-SA 4.0.
- Kanji info: **KANJIDIC2** (EDRDG licence).
