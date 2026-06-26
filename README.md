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
# (recommended) better word-frequency ranking during the build
python -m pip install wordfreq

# 1. One-time setup — downloads the kuromoji tokenizer, JMdict, and the
#    JMnedict name dictionary, then builds the database.
python setup.py            # (or double-click setup.bat)
#    python setup.py --no-names   # skip names to keep the DB small (~74 MB vs ~250 MB)

# 2. Run it
python server.py           # (or double-click run.bat)
```

Your browser opens at `http://127.0.0.1:6969`. Copy some Japanese text and it
appears instantly. Hover a word for its definition.

### Using it with a game

1. Run a text extractor such as **Textractor** and hook your visual novel.
2. In Textractor, enable the **Copy to Clipboard** extension (it ships with it).
3. Start this app — every line the game produces shows up automatically.

**Tip:** click a word to pin its popup open; press **Esc** to close. Hover gives a quick peek.

### Visual-novel frequency ranking (optional, recommended for VN readers)

By default, word rarity/ranking uses a general-corpus frequency (`wordfreq`). For
VN-accurate rarity tiers, import a **visual-novel frequency list** from
[jiten.moe](https://jiten.moe/other):

1. Go to **https://jiten.moe/other**, pick **Visual Novel**, and download the
   **Yomitan** frequency dictionary (a `.zip`). Save it into this folder.
2. Rebuild with it:
   ```sh
   python setup.py --skip-kuromoji --force --freq jiten_vn.zip
   ```

Now rarity tiers are by **VN rank** (top 1,500 words = Common, … rarest = Legendary),
the popup shows each word's VN frequency rank, and lookups prefer the reading that's
common in visual novels. Frequency data: jiten.moe, **CC BY-SA 4.0**. (Definitions
remain JMdict/JMnedict — the same sources jiten itself uses.)

**No-manual-step alternative:** `python setup.py --skip-kuromoji --force --innocent`
auto-downloads the **Innocent Corpus** VN/novel frequency list instead. It's convenient
but coarser — it omits some very common function words and under-counts する — so the
hand-downloaded jiten.moe VN list above is recommended for the cleanest tiers. The
frequency loader auto-detects rank-style vs. count-style lists, so any Yomitan
frequency `.zip` works with `--freq`.

---

## Features

- **Auto clipboard capture** over Server-Sent Events — text appears the moment it's copied.
- **Offline dictionary** built from JMdict (the standard free J–E dictionary). Works with no internet after setup.
- **Smart longest-match scanning** — catches multi-word expressions and compounds the
  tokenizer splits (一日中, という), with a full de-inflector that also shows the
  inflection trail (読んでいた → 読む · progressive › past).
- **Smart ranking** so the intended word comes first: part-of-speech (は = particle,
  not 羽 "feather"), reading (本【ほん】"book", not 本【もと】"origin"), and word
  frequency (居る "to be", not 射る "to shoot").
- **Name dictionary (JMnedict)** — recognizes character and place names (田中 → "Tanaka").
- **Word rarity tiers** — every word gets a Fortnite-style rarity badge by how common it
  is: Common → Uncommon → Rare → Epic → Legendary → Mythic (the rarer the word, the
  flashier the badge).
- **Polished popup** — click a word to pin it open, a copy button, and a Jisho.org link;
  hover for a quick peek.
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
