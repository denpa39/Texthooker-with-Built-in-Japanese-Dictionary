"""
One-time setup for the texthooker.

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
KUROMOJI_DICT_FILES = [
    "base.dat.gz", "cc.dat.gz", "check.dat.gz", "tid.dat.gz", "tid_map.dat.gz",
    "tid_pos.dat.gz", "unk.dat.gz", "unk_char.dat.gz", "unk_compat.dat.gz",
    "unk_invoke.dat.gz", "unk_map.dat.gz", "unk_pos.dat.gz",
]

JMDICT_RELEASES_API = "https://api.github.com/repos/scriptin/jmdict-simplified/releases/latest"
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
    print("[1/2] kuromoji tokenizer")
    js_path = os.path.join(KUROMOJI_DIR, "kuromoji.js")
    if force or not os.path.isfile(js_path):
        download(f"{KUROMOJI_CDN}/build/kuromoji.js", js_path)
    else:
        print("  kuromoji.js already present")

    os.makedirs(KUROMOJI_DICT_DIR, exist_ok=True)
    for name in KUROMOJI_DICT_FILES:
        dest = os.path.join(KUROMOJI_DICT_DIR, name)
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
    """Frequency score for an entry — prefer the kanji spelling, fall back to kana.
    Scored by spelling so 居る (frequent) beats 射る (rare), even though both read いる."""
    forms = kanji if kanji else kana
    score = max((wf.get(t, 0.0) for t in forms), default=0.0)
    f = int(round(score * 1e8))
    if f == 0 and common:
        f = 1  # keep common-but-unscored words above rare homographs
    return f


def build_db(jmdict_json_bytes):
    print("  parsing JMdict (this takes a moment)...")
    data = json.loads(jmdict_json_bytes)
    words = data["words"]
    print(f"  {len(words):,} dictionary entries")
    wf = _wf_dict()
    print("  frequency data: " + (f"{len(wf):,} words (wordfreq)" if wf
                                   else "wordfreq not installed — ranking by common flag"))

    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
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
        common = any(k.get("common") for k in w.get("kanji", [])) or \
                 any(k.get("common") for k in w.get("kana", []))
        freq = _entry_freq(kanji, kana, common, wf)
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
        rec = {"id": w["id"], "k": kanji, "r": kana, "c": common, "f": freq, "s": senses}
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

    print("  building index...")
    cur.execute("CREATE INDEX idx_terms ON terms(term)")
    con.commit()
    cur.execute("VACUUM")
    con.commit()
    con.close()
    size_mb = os.path.getsize(DB_PATH) >> 20
    print(f"  dict.sqlite built ({size_mb} MB)\n")


def setup_dictionary(common, force=False):
    print("[2/2] JMdict dictionary")
    if not force and os.path.isfile(DB_PATH):
        print("  dict.sqlite already present (use --force to rebuild)\n")
        return
    name, url = find_jmdict_asset(common)
    print(f"  downloading {name}")
    with tempfile.TemporaryDirectory() as tmp:
        archive = os.path.join(tmp, name)
        download(url, archive)
        js = extract_json(archive)
    build_db(js)


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Texthooker setup / dictionary builder")
    ap.add_argument("--common", action="store_true",
                    help="use the smaller common-words-only JMdict edition")
    ap.add_argument("--force", action="store_true", help="redownload / rebuild everything")
    ap.add_argument("--skip-kuromoji", action="store_true", help="only build the dictionary DB")
    args = ap.parse_args()

    print("Texthooker setup\n================")
    if not args.skip_kuromoji:
        setup_kuromoji(force=args.force)
    setup_dictionary(common=args.common, force=args.force)
    print("Done!  Start the app with:  python server.py")


if __name__ == "__main__":
    main()
