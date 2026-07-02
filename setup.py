"""
One-time setup for Down the Rabbit Hole (the texthooker).

  1. Downloads the kuromoji.js tokenizer + its dictionary into static/kuromoji/.
  2. Downloads JMdict (the standard free Japanese->English dictionary) and builds
     a fast SQLite lookup database (dict.sqlite).

Pure standard library. Just run:

    python setup.py            # full JMdict (recommended)
    python setup.py --common   # smaller "common words only" edition
    python setup.py --skip-kuromoji   # only rebuild the dictionary DB

Re-running skips files that already exist (use --force to redownload).
"""

import argparse
import io
import json
import os
import sqlite3
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KUROMOJI_DIR = os.path.join(BASE_DIR, "static", "kuromoji")
KUROMOJI_DICT_DIR = os.path.join(KUROMOJI_DIR, "dict")
DB_PATH = os.path.join(BASE_DIR, "dict.sqlite")

KUROMOJI_VERSION = "0.1.2"
KUROMOJI_CDN = f"https://cdn.jsdelivr.net/npm/kuromoji@{KUROMOJI_VERSION}"
# Saved without the .gz suffix (content stays gzipped; kuromoji's loader always
# gunzips). Download managers like IDM hijack *.gz URLs from the app's webview
# and spam download dialogs — a plain .dat name sails past them.
KUROMOJI_DICT_FILES = [
    "base.dat.gz", "cc.dat.gz", "check.dat.gz", "tid.dat.gz", "tid_map.dat.gz",
    "tid_pos.dat.gz", "unk.dat.gz", "unk_char.dat.gz", "unk_compat.dat.gz",
    "unk_invoke.dat.gz", "unk_map.dat.gz", "unk_pos.dat.gz",
]

JMDICT_RELEASES_API = "https://api.github.com/repos/scriptin/jmdict-simplified/releases/latest"
# Innocent Corpus: frequency from a large corpus of visual novels + light novels.
# Auto-downloaded as the VN-flavored default when no --freq is given.
INNOCENT_URL = ("https://raw.githubusercontent.com/MarvNC/yomitan-dictionaries/"
                "master/japanese/freq/innocent_corpus/innocent_corpus.zip")
# Kanjium pitch-accent data (term \t reading \t accent), CC BY-SA 4.0.
KANJIUM_URL = ("https://raw.githubusercontent.com/mifunetoshiro/kanjium/"
               "master/data/source_files/raw/accents.txt")
# Textractor (GPL-3.0) — its CLI is embedded so the app can hook games directly.
TEXTRACTOR_API = "https://api.github.com/repos/Artikash/Textractor/releases/latest"
TEXTRACTOR_DIR = os.path.join(BASE_DIR, "textractor")
UA = {"User-Agent": "texthooker-setup/1.0"}


def download(url, dest, headers=None):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    req = urllib.request.Request(url, headers=headers or UA)
    with urllib.request.urlopen(req) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        read = 0
        chunk = 1 << 16
        with open(dest, "wb") as f:
            while True:
                data = resp.read(chunk)
                if not data:
                    break
                f.write(data)
                read += len(data)
                if total:
                    pct = read * 100 // total
                    sys.stdout.write(f"\r  {os.path.basename(dest)}: {pct}% "
                                     f"({read >> 20}/{total >> 20} MB)")
                else:
                    sys.stdout.write(f"\r  {os.path.basename(dest)}: {read >> 20} MB")
                sys.stdout.flush()
    sys.stdout.write("\n")


def fetch_bytes(url, headers=None):
    req = urllib.request.Request(url, headers=headers or UA)
    with urllib.request.urlopen(req) as resp:
        return resp.read()


# --------------------------------------------------------------------------- #
def setup_kuromoji(force=False):
    print("[1/6] kuromoji tokenizer")
    js_path = os.path.join(KUROMOJI_DIR, "kuromoji.js")
    if force or not os.path.isfile(js_path):
        download(f"{KUROMOJI_CDN}/build/kuromoji.js", js_path)
    else:
        print("  kuromoji.js already present")

    # Point the loader at the .gz-less filenames we save below.
    with open(js_path, encoding="utf-8") as f:
        js = f.read()
    if ".dat.gz" in js:
        with open(js_path, "w", encoding="utf-8") as f:
            f.write(js.replace(".dat.gz", ".dat"))

    os.makedirs(KUROMOJI_DICT_DIR, exist_ok=True)
    for name in KUROMOJI_DICT_FILES:
        dest = os.path.join(KUROMOJI_DICT_DIR, name[:-3])
        if not force and os.path.isfile(dest):
            continue
        download(f"{KUROMOJI_CDN}/dict/{name}", dest)
    print("  kuromoji dictionary ready\n")


# --------------------------------------------------------------------------- #
def find_jmdict_asset(common):
    print("  locating latest JMdict release on GitHub...")
    info = json.loads(fetch_bytes(JMDICT_RELEASES_API))
    assets = info.get("assets", [])
    # Names look like jmdict-eng-3.6.1.json.tgz / jmdict-eng-common-3.6.1.json.zip
    def matches(name):
        if not name.endswith((".tgz", ".zip")):
            return False
        if common:
            return name.startswith("jmdict-eng-common")
        return name.startswith("jmdict-eng-") and "common" not in name
    cands = [a for a in assets if matches(a["name"])]
    # Prefer .tgz (smaller) over .zip.
    cands.sort(key=lambda a: 0 if a["name"].endswith(".tgz") else 1)
    if not cands:
        raise RuntimeError("Could not find a JMdict English asset in the latest release.")
    return cands[0]["name"], cands[0]["browser_download_url"]


def extract_json(archive_path):
    if archive_path.endswith(".tgz") or archive_path.endswith(".tar.gz"):
        with tarfile.open(archive_path, "r:gz") as tar:
            member = next(m for m in tar.getmembers() if m.name.endswith(".json"))
            return tar.extractfile(member).read()
    elif archive_path.endswith(".zip"):
        with zipfile.ZipFile(archive_path) as zf:
            name = next(n for n in zf.namelist() if n.endswith(".json"))
            return zf.read(name)
    raise RuntimeError("Unknown archive format: " + archive_path)


_WF = None


def _wf_dict():
    """Load wordfreq's Japanese frequency table (no MeCab needed). {} if absent."""
    global _WF
    if _WF is None:
        try:
            from wordfreq import get_frequency_dict
            _WF = get_frequency_dict("ja")
        except Exception:
            _WF = {}
    return _WF


def _entry_freq(kanji, kana, common, wf):
    """Ranking score — prefer the kanji spelling, fall back to kana. Scored by
    spelling so 居る (frequent) beats 射る (rare), even though both read いる."""
    forms = kanji if kanji else kana
    score = max((wf.get(t, 0.0) for t in forms), default=0.0)
    f = int(round(score * 1e8))
    if f == 0 and common:
        f = 1  # keep common-but-unscored words above rare homographs
    return f


def _display_freq(kanji, kana, common, wf):
    """Overall commonness (max over ALL spellings, so words usually written in kana
    (する) aren't judged rare by their rare kanji (為る)). Stored as `df`; currently
    unused by the ranking — kept for possible future use."""
    score = max((wf.get(t, 0.0) for t in (kanji + kana)), default=0.0)
    d = int(round(score * 1e8))
    if d == 0 and common:
        d = 1
    return d


def _freq_value(v):
    """Pull the numeric value out of a Yomitan term_meta_bank freq value (handles
    plain ints, strings, and nested {value}/{frequency}/{reading} shapes)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        digits = "".join(ch for ch in v if ch.isdigit())
        return int(digits) if digits else None
    if isinstance(v, dict):
        for key in ("frequency", "value", "displayValue"):
            if key in v:
                r = _freq_value(v[key])
                if r is not None:
                    return r
    return None


def _load_vn_freq(zip_path):
    """Parse a Yomitan frequency dictionary (.zip) into {term: rank}, lower = more
    common. Auto-detects whether the source stores ranks (lower = common, like the
    jiten.moe lists) or raw occurrence counts (higher = common, like Innocent
    Corpus), then normalizes both to a clean 1..N rank."""
    lo, hi = {}, {}          # smallest / largest value seen per term
    gmax = 0
    with zipfile.ZipFile(zip_path) as zf:
        banks = [n for n in zf.namelist()
                 if "term_meta_bank" in n and n.endswith(".json")]
        for n in banks:
            for entry in json.loads(zf.read(n)):
                if len(entry) < 3 or entry[1] != "freq":
                    continue
                term, val = entry[0], _freq_value(entry[2])
                if val is None:
                    continue
                lo[term] = min(val, lo.get(term, val))
                hi[term] = max(val, hi.get(term, val))
                gmax = max(gmax, val)
    if not lo:
        print("  VN frequency: no entries found — skipping")
        return {}

    n = len(lo)
    count_based = gmax > 3 * n          # counts are far larger than the term count
    if count_based:
        commonness = {t: hi[t] for t in hi}        # higher count = more common
    else:
        commonness = {t: -lo[t] for t in lo}       # lower rank = more common
    ranked = sorted(commonness, key=commonness.get, reverse=True)
    freq = {t: i + 1 for i, t in enumerate(ranked)}
    print(f"  VN frequency: {n:,} terms "
          f"({'occurrence counts' if count_based else 'ranks'} → normalized rank)")
    return freq


# Curated high-frequency grammatical set phrases that JMdict does *not* ship as
# headwords, so the tokenizer would otherwise leave them split into their pieces.
# Each is marked common so the longest-match scan trusts it (like について / という).
# IDs live in a private 90,000,000+ range so they never collide with JMdict/JMnedict.
_EXPRESSIONS = [
    # (kanji[], kana[], gloss[])
    (["に違いない"],   ["にちがいない"],     ["there is no doubt that", "surely", "must be"]),
    ([],               ["にちがいありません"], ["(polite) there is no doubt that", "surely", "must be"]),
    (["ものではない"], ["ものではない"],     ["one should not ...", "it is not the sort of thing one ..."]),
    (["様がない"],     ["ようがない"],       ["there is no way to ...", "cannot possibly ..."]),
    (["に他ならない"], ["にほかならない"],   ["nothing but ...", "none other than ..."]),
    (["を始め"],       ["をはじめ"],         ["starting with ...", "including", "among others"]),
    (["よりほかない"], ["よりほかない"],     ["have no choice but to ...", "cannot help but ..."]),
    (["より仕方ない"], ["よりしかたない"],   ["cannot be helped", "there is no choice but to ..."]),
]
_EXPR_ID_BASE = 90_000_001


def add_expression_supplement(cur):
    """Insert the curated set-phrase supplement (idempotent — safe to re-run)."""
    cur.execute("DELETE FROM entries WHERE id >= ?", (_EXPR_ID_BASE,))
    cur.execute("DELETE FROM terms   WHERE id >= ?", (_EXPR_ID_BASE,))
    for i, (kanji, kana, gloss) in enumerate(_EXPRESSIONS):
        eid = _EXPR_ID_BASE + i
        rec = {"id": str(eid), "k": kanji, "r": kana, "c": True, "f": 800,
               "s": [{"pos": ["exp"], "gloss": gloss, "misc": []}]}
        cur.execute("INSERT INTO entries VALUES (?,?,?)",
                    (eid, 800, json.dumps(rec, ensure_ascii=False)))
        for t in kanji + kana:
            cur.execute("INSERT INTO terms VALUES (?,?)", (t, eid))


def build_db(jmdict_json_bytes, vn_freq=None):
    print("  parsing JMdict (this takes a moment)...")
    data = json.loads(jmdict_json_bytes)
    words = data["words"]
    print(f"  {len(words):,} dictionary entries")
    wf = _wf_dict()
    print("  frequency data: " + (f"{len(wf):,} words (wordfreq)" if wf
                                   else "wordfreq not installed — ranking by common flag"))

    if os.path.exists(DB_PATH):
        try:
            os.remove(DB_PATH)
        except PermissionError:
            sys.exit("\n  ERROR: dict.sqlite is in use by another process.\n"
                     "  Stop the running app first (close 'python server.py' / run.bat),\n"
                     "  then re-run this command.")
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("PRAGMA journal_mode = OFF")
    cur.execute("PRAGMA synchronous = OFF")
    cur.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY, freq INTEGER, json TEXT)")
    cur.execute("CREATE TABLE terms (term TEXT, id INTEGER)")

    entries = []
    terms = []
    for w in words:
        kanji = [k["text"] for k in w.get("kanji", [])]
        kana = [k["text"] for k in w.get("kana", [])]
        # Per-element JMdict priority: which specific spellings/readings are common,
        # not just the entry as a whole (口 is common as くち, rare as こう).
        kanji_common = [k["text"] for k in w.get("kanji", []) if k.get("common")]
        kana_common = [k["text"] for k in w.get("kana", []) if k.get("common")]
        common = bool(kanji_common or kana_common)
        freq = _entry_freq(kanji, kana, common, wf)
        dfreq = _display_freq(kanji, kana, common, wf)
        senses = []
        for s in w.get("sense", []):
            gloss = [g["text"] for g in s.get("gloss", []) if g.get("lang", "eng") == "eng"]
            if not gloss:
                continue
            senses.append({
                "pos": s.get("partOfSpeech", []),
                "gloss": gloss,
                "misc": s.get("misc", []),
                "info": s.get("info", []),
            })
        if not senses:
            continue
        eid = int(w["id"])
        rec = {"id": w["id"], "k": kanji, "r": kana, "c": common, "f": freq,
               "df": dfreq, "s": senses}
        if kana_common:
            rec["rc"] = kana_common     # readings JMdict marks common
        if kanji_common:
            rec["kc"] = kanji_common    # spellings JMdict marks common
        if vn_freq is not None:
            kr = [vn_freq[t] for t in kanji if t in vn_freq] or \
                 [vn_freq[t] for t in kana if t in vn_freq]
            rec["vr"] = min(kr) if kr else None     # VN rank, kanji-preferred (drives ranking)
        entries.append((eid, freq, json.dumps(rec, ensure_ascii=False)))
        seen = set()
        for t in kanji + kana:
            if t and t not in seen:
                seen.add(t)
                terms.append((t, eid))

        if len(entries) >= 5000:
            cur.executemany("INSERT INTO entries VALUES (?,?,?)", entries)
            cur.executemany("INSERT INTO terms VALUES (?,?)", terms)
            entries.clear()
            terms.clear()

    if entries:
        cur.executemany("INSERT INTO entries VALUES (?,?,?)", entries)
    if terms:
        cur.executemany("INSERT INTO terms VALUES (?,?)", terms)

    add_expression_supplement(cur)   # curated set phrases JMdict lacks as headwords

    print("  building index...")
    cur.execute("CREATE INDEX idx_terms ON terms(term)")
    con.commit()
    cur.execute("VACUUM")
    con.commit()
    con.close()
    size_mb = os.path.getsize(DB_PATH) >> 20
    print(f"  dict.sqlite built ({size_mb} MB)\n")


def setup_dictionary(common, force=False, freq_zip=None, innocent=False):
    print("[2/6] JMdict dictionary + frequency")
    if not force and os.path.isfile(DB_PATH):
        print("  dict.sqlite already present (use --force to rebuild)\n")
        return
    vn_freq = None
    with tempfile.TemporaryDirectory() as tmp:
        if freq_zip:
            print(f"  loading VN frequency from {os.path.basename(freq_zip)}")
            vn_freq = _load_vn_freq(freq_zip)
        elif innocent:
            try:
                ic = os.path.join(tmp, "innocent_corpus.zip")
                print("  downloading VN/novel frequency (Innocent Corpus)...")
                download(INNOCENT_URL, ic)
                vn_freq = _load_vn_freq(ic)
            except Exception as e:
                print(f"  VN frequency unavailable ({e}); using general word frequency")
        name, url = find_jmdict_asset(common)
        print(f"  downloading {name}")
        archive = os.path.join(tmp, name)
        download(url, archive)
        js = extract_json(archive)
    build_db(js, vn_freq=vn_freq)


def find_jmnedict_asset():
    info = json.loads(fetch_bytes(JMDICT_RELEASES_API))
    cands = [a for a in info.get("assets", [])
             if a["name"].startswith("jmnedict-all") and a["name"].endswith((".tgz", ".zip"))]
    cands.sort(key=lambda a: 0 if a["name"].endswith(".tgz") else 1)
    if not cands:
        raise RuntimeError("Could not find a JMnedict asset in the latest release.")
    return cands[0]["name"], cands[0]["browser_download_url"]


def build_names(js_bytes):
    print("  parsing JMnedict (this takes a moment)...")
    words = json.loads(js_bytes)["words"]
    print(f"  {len(words):,} names")
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("PRAGMA journal_mode = OFF")
    cur.execute("PRAGMA synchronous = OFF")
    cur.execute("DROP TABLE IF EXISTS names")
    cur.execute("DROP TABLE IF EXISTS nameterms")
    cur.execute("CREATE TABLE names (id INTEGER PRIMARY KEY, json TEXT)")
    cur.execute("CREATE TABLE nameterms (term TEXT, id INTEGER)")

    rows, terms = [], []
    for w in words:
        kanji = [k["text"] for k in w.get("kanji", [])]
        kana = [k["text"] for k in w.get("kana", [])]
        senses = []
        for t in w.get("translation", []):
            gl = [x["text"] for x in t.get("translation", []) if x.get("lang", "eng") == "eng"]
            if gl:
                senses.append({"pos": t.get("type", []), "gloss": gl})
        if not senses:
            continue
        eid = int(w["id"])
        rec = {"id": w["id"], "k": kanji, "r": kana, "c": False, "f": 0, "s": senses}
        rows.append((eid, json.dumps(rec, ensure_ascii=False)))
        seen = set()
        for t in kanji + kana:
            if t and t not in seen:
                seen.add(t)
                terms.append((t, eid))
        if len(rows) >= 5000:
            cur.executemany("INSERT OR IGNORE INTO names VALUES (?,?)", rows)
            cur.executemany("INSERT INTO nameterms VALUES (?,?)", terms)
            rows.clear()
            terms.clear()
    if rows:
        cur.executemany("INSERT OR IGNORE INTO names VALUES (?,?)", rows)
    if terms:
        cur.executemany("INSERT INTO nameterms VALUES (?,?)", terms)

    print("  building names index...")
    cur.execute("CREATE INDEX idx_nameterms ON nameterms(term)")
    con.commit()
    con.close()
    print("  names ready\n")


def setup_pitch(force=False):
    """Kanjium pitch-accent table: pitch(term, reading, accent). Optional —
    lookups work without it; the popup just shows no accent numbers."""
    print("[4/6] pitch accent (Kanjium)")
    if not os.path.isfile(DB_PATH):
        print("  build the dictionary first.\n")
        return
    con = sqlite3.connect(DB_PATH)
    if not force:
        has = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pitch'").fetchone()
        if has:
            con.close()
            print("  pitch accents already present (use --force to rebuild)\n")
            return
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "accents.txt")
            download(KANJIUM_URL, path)
            rows = []
            with open(path, encoding="utf-8") as f:
                for line in f:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) != 3 or not parts[2]:
                        continue
                    term, reading, accent = parts
                    rows.append((term, reading or term, accent))
        cur = con.cursor()
        cur.execute("PRAGMA journal_mode = OFF")
        cur.execute("PRAGMA synchronous = OFF")
        cur.execute("DROP TABLE IF EXISTS pitch")
        cur.execute("CREATE TABLE pitch (term TEXT, reading TEXT, accent TEXT)")
        cur.executemany("INSERT INTO pitch VALUES (?,?,?)", rows)
        cur.execute("CREATE INDEX idx_pitch ON pitch(term)")
        con.commit()
        print(f"  {len(rows):,} pitch entries ready\n")
    except Exception as e:
        print(f"  pitch accent unavailable ({e}) — skipping (lookups still work)\n")
    finally:
        con.close()


def setup_textractor(force=False):
    """Download the Textractor zip and extract it into textractor/ so the app can
    spawn TextractorCLI.exe itself (the in-app 'Attach' button)."""
    print("[5/6] Textractor (embedded game hooking)")
    if not force and (os.path.isfile(os.path.join(TEXTRACTOR_DIR, "x64", "TextractorCLI.exe"))
                      or os.path.isfile(os.path.join(TEXTRACTOR_DIR, "x86", "TextractorCLI.exe"))):
        print("  Textractor already present (use --force to redownload)\n")
        return
    try:
        info = json.loads(fetch_bytes(TEXTRACTOR_API))
        asset = next(a for a in info.get("assets", [])
                     if a["name"].endswith(".zip"))
        with tempfile.TemporaryDirectory() as tmp:
            zpath = os.path.join(tmp, asset["name"])
            print(f"  downloading {asset['name']}")
            download(asset["browser_download_url"], zpath)
            print("  extracting...")
            with zipfile.ZipFile(zpath) as zf:
                for m in zf.namelist():
                    parts = m.replace("\\", "/").split("/")
                    # strip the top-level "Textractor/" folder if the zip has one
                    if parts and parts[0].lower().startswith("textractor"):
                        parts = parts[1:]
                    if not parts or not parts[-1]:
                        continue
                    dest = os.path.join(TEXTRACTOR_DIR, *parts)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with zf.open(m) as src, open(dest, "wb") as out:
                        out.write(src.read())
        print("  Textractor ready — use the Attach button in the app\n")
    except Exception as e:
        print(f"  Textractor unavailable ({e}) — the Attach button will explain; "
              f"clipboard capture still works\n")


def setup_pywebview():
    """Best-effort install of pywebview for the standalone app window. The app
    falls back to the browser without it, so a failure here is not fatal."""
    print("[6/6] app window (pywebview)")
    try:
        import webview  # noqa: F401
        print("  pywebview already installed\n")
        return
    except ImportError:
        pass
    try:
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pywebview"],
                              stdout=subprocess.DEVNULL)
        print("  pywebview installed — the app opens in its own window\n")
    except Exception as e:
        print(f"  could not install pywebview ({e}) — the app will open in the "
              f"browser instead\n")


def setup_names(force=False):
    print("[3/6] JMnedict names")
    if not os.path.isfile(DB_PATH):
        print("  build the dictionary first.\n")
        return
    if not force:
        con = sqlite3.connect(DB_PATH)
        has = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='names'").fetchone()
        con.close()
        if has:
            print("  names already present (use --force to rebuild)\n")
            return
    name, url = find_jmnedict_asset()
    print(f"  downloading {name}")
    with tempfile.TemporaryDirectory() as tmp:
        archive = os.path.join(tmp, name)
        download(url, archive)
        js = extract_json(archive)
    build_names(js)


# --------------------------------------------------------------------------- #
def main():
    try:
        sys.stdout.reconfigure(errors="replace")   # never crash printing to a non-UTF-8 console
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Down the Rabbit Hole - setup / dictionary builder")
    ap.add_argument("--common", action="store_true",
                    help="use the smaller common-words-only JMdict edition")
    ap.add_argument("--force", action="store_true", help="redownload / rebuild everything")
    ap.add_argument("--skip-kuromoji", action="store_true", help="only build the dictionary DB")
    ap.add_argument("--no-names", action="store_true", help="skip the JMnedict names dictionary")
    ap.add_argument("--freq", metavar="ZIP",
                    help="path to a Yomitan frequency dictionary .zip (e.g. the Visual "
                         "Novel list from jiten.moe) to drive lookup ranking")
    ap.add_argument("--innocent", action="store_true",
                    help="auto-download the Innocent Corpus VN/novel frequency list "
                         "(no manual step, but coarser than the jiten.moe VN list)")
    ap.add_argument("--no-textractor", action="store_true",
                    help="skip downloading Textractor (in-app game hooking)")
    ap.add_argument("--textractor", action="store_true",
                    help="only download Textractor (skip everything else)")
    args = ap.parse_args()

    print("Down the Rabbit Hole - setup\n" + "=" * 28)
    if args.textractor:
        setup_textractor(force=args.force)
        return
    if not args.skip_kuromoji:
        setup_kuromoji(force=args.force)
    setup_dictionary(common=args.common, force=args.force, freq_zip=args.freq,
                     innocent=args.innocent)
    if not args.no_names:
        setup_names(force=args.force)
    setup_pitch(force=args.force)
    if not args.no_textractor:
        setup_textractor(force=args.force)
    setup_pywebview()
    print("Done!  Start the app with:  python server.py")


if __name__ == "__main__":
    main()
