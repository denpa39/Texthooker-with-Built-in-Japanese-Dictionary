# VN Texthooker

A visual-novel **text hooker** with a **built-in offline Japanese→English dictionary**.
It watches your Windows clipboard for Japanese text (e.g. extracted from a game by
[Textractor](https://github.com/Artikash/Textractor)), shows it in a clean reader,
and gives you a **hover dictionary** — point at any word to see its meaning, reading,
and part of speech, Yomitan-style.

Pure Python standard library + a browser UI. No `pip install` required.

---

## Quick start

```sh
# 1. One-time setup — downloads the kuromoji tokenizer + JMdict and builds the DB
python setup.py            # (or double-click setup.bat)

# 2. Run it
python server.py           # (or double-click run.bat)
```

Your browser opens at `http://127.0.0.1:6969`. Copy some Japanese text and it
appears instantly. Hover a word for its definition.

### Using it with a game

1. Run a text extractor such as **Textractor** and hook your visual novel.
2. In Textractor, enable the **Copy to Clipboard** extension (it ships with it).
3. Start this app — every line the game produces shows up automatically.

### Dictionary ranking

Hover lookups are ranked so the word you actually mean comes first: by
part-of-speech (は = topic particle, not 羽 "feather"), reading (本【ほん】"book",
not 本【もと】"origin"), and word frequency (居る "to be", not 射る "to shoot").
Frequency data comes from the `wordfreq` package at build time — if you rebuild
the dictionary, `pip install wordfreq` first for best ranking (otherwise it falls
back to JMdict's common-word flag).

---

## Features

- **Auto clipboard capture** over Server-Sent Events — text appears the moment it's copied.
- **Offline dictionary** built from JMdict (the standard free J–E dictionary). Works with no internet after setup.
- **Hover lookups** with automatic de-inflection (粘った → 粘る) via the kuromoji morphological tokenizer.
- **Furigana toggle**, adjustable font size, pause/resume capture, and clear.
- Everything runs locally on `127.0.0.1`; nothing is sent anywhere, and no account or payment is needed.

---

## Toolbar

| Control | What it does |
|---|---|
| ⏸ Pause / ▶ Resume | Stop/continue reading the clipboard |
| あ Furigana | Show readings above kanji |
| A slider | Reader font size |
| 🗑 Clear | Remove all lines |
| ● live / paused | Connection + capture status |

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
python server.py --no-browser    # don't auto-open the browser
```

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
  `/lookup?term=…` from the SQLite dictionary.
- `static/app.js` tokenizes each line in the browser, wraps words in hoverable
  spans, and fetches definitions on demand.

## Notes

- Clipboard monitoring is Windows-only (uses the Win32 clipboard API). The UI and
  dictionary still work on other platforms — paste text manually.
- The dictionary database and downloaded tokenizer are not committed; run
  `setup.py` to generate them.
