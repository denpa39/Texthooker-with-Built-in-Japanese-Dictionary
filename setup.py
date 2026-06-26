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
# Innocent Corpus: frequency from a large corpus of visual novels + light novels.
# Auto-downloaded as the VN-flavored default when no --freq is given.
INNOCENT_URL = ("https://raw.githubusercontent.com/MarvNC/yomitan-dictionaries/"
                "master/japanese/freq/innocent_corpus/innocent_corpus.zip")
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
    print("[1/3] kuromoji tokenizer")
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
    """Ranking score — prefer the kanji spelling, fall back to kana. Scored by
    spelling so 居る (frequent) beats 射る (rare), even though both read いる."""
    forms = kanji if kanji else kana
    score = max((wf.get(t, 0.0) for t in forms), default=0.0)
    f = int(round(score * 1e8))
    if f == 0 and common:
        f = 1  # keep common-but-unscored words above rare homographs
    return f


def _display_freq(kanji, kana, common, wf):
    """Overall commonness for the rarity badge — max over ALL spellings, so words
    usually written in kana (する) aren't judged rare by their rare kanji (為る)."""
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
        common = any(k.get("common") for k in w.get("kanji", [])) or \
                 any(k.get("common") for k in w.get("kana", []))
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
        if vn_freq is not None:
            kr = [vn_freq[t] for t in kanji if t in vn_freq] or \
                 [vn_freq[t] for t in kana if t in vn_freq]
            allr = [vn_freq[t] for t in (kanji + kana) if t in vn_freq]
            rec["vr"] = min(kr) if kr else None     # ranking (kanji-preferred)
            rec["vrd"] = min(allr) if allr else None  # rarity + display (any spelling)
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


def setup_dictionary(common, force=False, freq_zip=None, innocent=False):
    print("[2/3] JMdict dictionary + frequency")
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


def setup_names(force=False):
    print("[3/3] JMnedict names")
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
    ap = argparse.ArgumentParser(description="Texthooker setup / dictionary builder")
    ap.add_argument("--common", action="store_true",
                    help="use the smaller common-words-only JMdict edition")
    ap.add_argument("--force", action="store_true", help="redownload / rebuild everything")
    ap.add_argument("--skip-kuromoji", action="store_true", help="only build the dictionary DB")
    ap.add_argument("--no-names", action="store_true", help="skip the JMnedict names dictionary")
    ap.add_argument("--freq", metavar="ZIP",
                    help="path to a Yomitan frequency dictionary .zip (e.g. the Visual "
                         "Novel list from jiten.moe) to drive ranking + rarity tiers")
    ap.add_argument("--innocent", action="store_true",
                    help="auto-download the Innocent Corpus VN/novel frequency list "
                         "(no manual step, but coarser than the jiten.moe VN list)")
    args = ap.parse_args()

    print("Texthooker setup\n================")
    if not args.skip_kuromoji:
        setup_kuromoji(force=args.force)
    setup_dictionary(common=args.common, force=args.force, freq_zip=args.freq,
                     innocent=args.innocent)
    if not args.no_names:
        setup_names(force=args.force)
    print("Done!  Start the app with:  python server.py")


if __name__ == "__main__":
    main()
