"""
Down the Rabbit Hole — texthooker server: watches the Windows clipboard for new
(game) text, streams it
to the browser over Server-Sent Events, and serves offline JMdict lookups.

Pure standard library. Run `python setup.py` once first to build dict.sqlite and
download the kuromoji tokenizer into static/kuromoji/.

Usage:
    python server.py            # http://127.0.0.1:3939
    python server.py --port 7000 --no-browser
"""

import argparse
import ctypes
import json
import os
import queue
import sqlite3
import sys
import threading
import time
import webbrowser
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import deinflect

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DB_PATH = os.path.join(BASE_DIR, "dict.sqlite")

# --------------------------------------------------------------------------- #
# Windows clipboard reader (ctypes, no dependencies)
# --------------------------------------------------------------------------- #
CF_UNICODETEXT = 13

if sys.platform == "win32":
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL


def get_clipboard_text():
    """Return current clipboard text (unicode) or None."""
    if sys.platform != "win32":
        return None
    # OpenClipboard can fail if another process holds it; retry briefly.
    for _ in range(5):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.01)
    else:
        return None
    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.c_wchar_p(ptr).value
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


# --------------------------------------------------------------------------- #
# Broadcaster: fan out clipboard updates to all connected SSE clients
# --------------------------------------------------------------------------- #
class Broadcaster:
    def __init__(self):
        self._subs = set()
        self._lock = threading.Lock()
        self.last_text = None

    def subscribe(self):
        q = queue.Queue(maxsize=100)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._subs.discard(q)

    def publish(self, text):
        self.last_text = text
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(text)
            except queue.Full:
                pass


broadcaster = Broadcaster()


def clipboard_monitor(paused_flag):
    last_seq = None
    last_text = None
    while True:
        if not paused_flag.is_set():
            try:
                seq = user32.GetClipboardSequenceNumber() if sys.platform == "win32" else None
            except Exception:
                seq = None
            if seq != last_seq:
                last_seq = seq
                text = get_clipboard_text()
                if text and text != last_text:
                    last_text = text
                    broadcaster.publish(text)
        time.sleep(0.3)


PAUSED = threading.Event()  # set => clipboard monitoring paused


# --------------------------------------------------------------------------- #
# Dictionary lookup (SQLite, read-only, one connection per thread)
# --------------------------------------------------------------------------- #
_thread_local = threading.local()


def get_db():
    conn = getattr(_thread_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _thread_local.conn = conn
    return conn


_KATA_TO_HIRA = {chr(c): chr(c - 0x60) for c in range(0x30A1, 0x30F7)}


def to_hiragana(s):
    return "".join(_KATA_TO_HIRA.get(ch, ch) for ch in s)


# kuromoji part-of-speech (品詞) -> JMdict partOfSpeech tags, used to surface the
# right homograph (は the particle, not 羽 "feather"; た the auxiliary, not 多).
_POS_MAP = {
    # 'exp' (set expression) is added to the grammatical roles a phrase can fill, so
    # a multi-word expression earns the POS tiebreak when the tokenizer tagged the
    # token as a particle/conjunction/adverb/adnominal (様に over the rare 陽に).
    "助詞": {"prt", "exp"},
    "助動詞": {"aux", "aux-v", "aux-adj", "cop", "cop-da"},
    "形容詞": {"adj-i", "adj-ix"},
    "副詞": {"adv", "adv-to", "exp"},
    "連体詞": {"adj-pn", "exp"},
    "接続詞": {"conj", "exp"},
    "感動詞": {"int"},
    "接頭詞": {"pref"},
    "名詞": {"n", "pn", "n-suf", "n-pref", "n-t"},
    "代名詞": {"pn"},
}


def _pos_match(entry, allowed, is_verb):
    for s in entry["s"]:
        for p in s.get("pos", []):
            if is_verb:
                if p.startswith("v"):
                    return True
            elif p in allowed:
                return True
    return False


def _reading_match(entry, reading_h):
    return any(to_hiragana(rk) == reading_h for rk in entry.get("r", []))


# When a verb is written in kana, frequency-by-spelling can't tell which homograph
# is meant (居る vs 入る vs 射る all read いる). These are the dominant reading.
_KANA_PREF = {
    "いる": "居る", "くる": "来る", "できる": "出来る",
    "ある": "有る", "なる": "成る", "みる": "見る",
}


def _fetch_entries(term):
    """All JMdict entries whose kanji/kana exactly equals `term` (kana fallback)."""
    db = get_db()
    out, seen = [], set()
    cands = [term]
    h = to_hiragana(term)
    if h != term:
        cands.append(h)
    for cand in cands:
        # LIMIT must exceed the largest homograph cluster (こう has 51) so a common
        # word with poor frequency attribution (斯う「こう」, stored freq 1) is never
        # truncated away — otherwise hovering it would surface only rarer homographs.
        rows = db.execute(
            "SELECT DISTINCT e.id, e.json FROM terms t JOIN entries e ON e.id = t.id "
            "WHERE t.term = ? ORDER BY e.freq DESC LIMIT 80", (cand,)).fetchall()
        for eid, js in rows:
            if eid not in seen:
                seen.add(eid)
                out.append(json.loads(js))
        if out:
            break
    return out


def _fetch_names(term):
    """JMnedict name entries (empty if the names table hasn't been built)."""
    db = get_db()
    try:
        rows = db.execute(
            "SELECT DISTINCT n.id, n.json FROM nameterms t JOIN names n ON n.id = t.id "
            "WHERE t.term = ? ORDER BY n.id LIMIT 8", (term,)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [json.loads(js) for _id, js in rows]


# --------------------------------------------------------------------------- #
# Ranking
#
# One sort key decides every lookup. The guiding idea is "longest *plausible*
# match, anchored on the tokenizer's own segmentation": kuromoji already analysed
# the sentence and told us this token's dictionary form, part-of-speech and
# reading, so we trust that and only let a *longer* match win when it is itself a
# known, common word. That single rule dissolves two whole bug classes —
# rare over-extensions (hover そこ -> 底荷「そこに」"ballast") and names burying a
# real word (hover 村 -> the surnames むらさき/たくみ) — without any rarity tiers.
# --------------------------------------------------------------------------- #
# A longer match is only trusted to out-rank the tokenizer's own word if it is
# itself *common*. The VN frequency list has 100k+ entries, so mere membership means
# nothing (底荷「そこに」"ballast" sits at rank 105,228); we require a word near the top
# of the VN list. ~6,600 keeps real compounds the tokenizer splits (一日中 #6249,
# について #557, 縫いぐるみ #6506, 絡まる #6504) trusted, while capping rarer
# over-extensions — the nearest over-match trap (好奇) sits at #13,450, leaving a clean
# gap. Empirically near-minimises the over-match audit without dropping a genuine compound.
_VN_COMMON_RANK = 6600


def _established(e):
    """A known, frequently-used word: flagged common in JMdict, or near the top of
    the VN frequency list. Only established words keep their length when they extend
    *past* the tokenizer's segment (一日中, という); a rare longer over-match
    (底荷「そこに」) does not, so it cannot bury the short word the tokenizer found."""
    if e.get("c"):
        return True
    vr = e.get("vr")
    return isinstance(vr, int) and vr <= _VN_COMMON_RANK


def _commonness(e):
    """A single sortable commonness signal, higher = more common. A JMdict-common word
    outranks a bare VN rank, because raw VN position can put an obscure homograph above
    the obvious everyday word (御先「みさき」#2593 vs the common 岬「みさき」). Within a
    tier, lower VN rank (or higher wordfreq) wins."""
    vr = e.get("vr")
    if isinstance(vr, int):
        return (3 if e.get("c") else 2, -vr)   # common-flagged VN words tier above the rest
    f = e.get("f") or 0
    if f:
        return (1, f)        # general wordfreq signal
    return (0, 0)            # no signal at all (names, very obscure entries)


def _sort_key(c, tok_len, pos, reading_h, pref):
    """The one ranking key for a candidate (lower sorts first)."""
    e = c["entry"]
    is_name = c["kind"] == "name"
    is_verb = pos == "動詞"
    allowed = _POS_MAP.get(pos)
    pos_ok = bool((allowed or is_verb) and _pos_match(e, allowed or set(), is_verb))
    read_ok = bool(reading_h and _reading_match(e, reading_h))
    # The hovered reading is the entry's *primary* (first-listed) reading. A multi-reading
    # homograph carries a single frequency tied to its dominant reading, so when it matches
    # only on a secondary reading it must not out-rank the word for which this reading is
    # primary (追う「おう」 over 合う「あう・おう」, whose frequency is really あう's).
    primary_read_ok = bool(reading_h and e.get("r") and to_hiragana(e["r"][0]) == reading_h)
    pref_ok = bool(pref and pref in e.get("k", []))

    # Cap a rare word that matches *past* the tokenizer's segment: it is almost
    # always an over-extension into the next token (そこ + に). Established longer
    # words (一日中) and names keep their real length.
    eff_len = c["len"]
    if not is_name and c["len"] > tok_len and not _established(e):
        eff_len = tok_len

    common_lvl, common_mag = _commonness(e)
    return (
        -eff_len,                          # 1. longest *plausible* match first
        1 if is_name else 0,               # 2. a real word beats a same-length name
        0 if (pos_ok or read_ok) else 1,   # 3. tokenizer-confirmed candidate first
        0 if pos_ok else 1,                # 4. part-of-speech agreement (其処 pronoun > 底 noun)
        0 if read_ok else 1,               # 5. reading agreement (any reading)
        0 if primary_read_ok else 1,       # 6. the hovered reading is the entry's primary one
        0 if pref_ok else 1,               # 7. known dominant kanji for a kana verb (居る > 射る)
        len(c["reasons"]),                 # 8. fewer de-inflection steps (exact > inflected)
        -common_lvl, -common_mag,          # 9. more common (JMdict-common tier > raw VN rank)
        -c["len"],                         # 10. true length
        e.get("id", ""),                   # 11. stable final tiebreak -> deterministic order
    )


def lookup(term, pos=None, reading=None):
    """Rank every entry whose spelling equals `term` (used by the /lookup route)."""
    if not term:
        return []
    reading_h = to_hiragana(reading) if reading else None
    pref = _KANA_PREF.get(term) or _KANA_PREF.get(to_hiragana(term))
    cands = [{"len": len(term), "reasons": [], "kind": "word", "entry": e}
             for e in _fetch_entries(term)]
    cands.sort(key=lambda c: _sort_key(c, len(term), pos, reading_h, pref))
    return [c["entry"] for c in cands]


def scan(text, pos=None, reading=None, base=None, surface=None):
    """Longest-match scan from the start of `text`, returning ranked candidates
    (words via de-inflection + names). `surface` is the tokenizer's surface form of
    the hovered token; its length anchors the over-extension cap in _sort_key."""
    text = (text or "").replace("\n", "")[:32]
    if not text:
        return []
    reading_h = to_hiragana(reading) if reading else None
    pref = _KANA_PREF.get(base or "") or _KANA_PREF.get(to_hiragana(base or ""))
    tok_len = len(surface or base or "") or 1

    cands, seen = [], set()
    for end in range(len(text), 0, -1):
        prefix = text[:end]
        for form, reasons in deinflect.deinflect(prefix).items():
            # deinflect() lists reasons outermost-first (the order rules peeled off);
            # reverse so the displayed trail reads stem-outward, the way a learner
            # derives it: 食べる › causative › passive › past (not past › passive › …).
            trail = list(reversed(reasons))
            for e in _fetch_entries(form):
                key = ("w", e["id"])
                if key not in seen:
                    seen.add(key)
                    cands.append({"len": end, "matched": prefix, "reasons": trail,
                                  "kind": "word", "entry": e})
        for e in _fetch_names(prefix):
            key = ("n", e["id"])
            if key not in seen:
                seen.add(key)
                cands.append({"len": end, "matched": prefix, "reasons": [],
                              "kind": "name", "entry": e})

    # Safety net: the tokenizer's dictionary form, in case the de-inflector can't
    # reach it (bare ichidan stems written in kanji: 見 -> 見る). Ranked like any
    # short match — longer real matches still rank above it.
    if base:
        for e in _fetch_entries(base):
            key = ("w", e["id"])
            if key not in seen:
                seen.add(key)
                cands.append({"len": 1, "matched": base, "reasons": [],
                              "kind": "word", "entry": e})

    # Tag the reading that actually matched when it isn't the entry's primary one,
    # so the popup can show 口【こう】 (hovered こう) instead of the primary 口【くち】.
    if reading_h:
        for c in cands:
            rs = c["entry"].get("r") or []
            if rs and to_hiragana(rs[0]) != reading_h:
                for rk in rs:
                    if to_hiragana(rk) == reading_h:
                        c["mr"] = rk
                        break

    cands.sort(key=lambda c: _sort_key(c, tok_len, pos, reading_h, pref))
    return cands[:12]


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".ico": "image/x-icon",
    ".gz": "application/octet-stream",  # kuromoji *.dat.gz must NOT be re-encoded
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter logging
        pass

    # -- helpers ----------------------------------------------------------- #
    def _send_bytes(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, status=200):
        self._send_bytes(
            json.dumps(obj, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            status,
        )

    def _serve_file(self, rel_path):
        # Prevent path traversal.
        full = os.path.normpath(os.path.join(BASE_DIR, rel_path.lstrip("/")))
        if not full.startswith(BASE_DIR) or not os.path.isfile(full):
            self._send_bytes(b"Not found", "text/plain; charset=utf-8", 404)
            return
        ext = os.path.splitext(full)[1].lower()
        ctype = CONTENT_TYPES.get(ext, "application/octet-stream")
        with open(full, "rb") as f:
            body = f.read()
        self._send_bytes(body, ctype)

    # -- routes ------------------------------------------------------------ #
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            self._serve_file("static/index.html")
        elif path == "/lookup":
            qs = parse_qs(parsed.query)
            term = (qs.get("term") or [""])[0]
            pos = (qs.get("pos") or [""])[0]
            reading = (qs.get("reading") or [""])[0]
            try:
                self._send_json({"term": term, "results": lookup(term, pos, reading)})
            except Exception as e:
                self._send_json({"term": term, "results": [], "error": str(e)}, 500)
        elif path == "/scan":
            qs = parse_qs(parsed.query)
            text = (qs.get("text") or [""])[0]
            pos = (qs.get("pos") or [""])[0]
            reading = (qs.get("reading") or [""])[0]
            base = (qs.get("base") or [""])[0]
            surface = (qs.get("surface") or [""])[0]
            try:
                self._send_json({"candidates": scan(text, pos, reading, base, surface)})
            except Exception as e:
                self._send_json({"candidates": [], "error": str(e)}, 500)
        elif path == "/state":
            self._send_json({"paused": PAUSED.is_set()})
        elif path == "/events":
            self._serve_events()
        elif path.startswith("/static/"):
            self._serve_file(path)
        else:
            self._send_bytes(b"Not found", "text/plain; charset=utf-8", 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/pause":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                want = bool(json.loads(raw or b"{}").get("paused"))
            except Exception:
                want = not PAUSED.is_set()
            if want:
                PAUSED.set()
            else:
                PAUSED.clear()
            self._send_json({"paused": PAUSED.is_set()})
        else:
            self._send_bytes(b"Not found", "text/plain; charset=utf-8", 404)

    # -- SSE (manual chunked transfer encoding) ---------------------------- #
    def _serve_events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def write_chunk(payload: bytes):
            self.wfile.write(b"%X\r\n" % len(payload) + payload + b"\r\n")
            self.wfile.flush()

        q = broadcaster.subscribe()
        try:
            write_chunk(b": connected\n\n")
            if broadcaster.last_text:
                write_chunk(b"data: " + json.dumps({"text": broadcaster.last_text}).encode("utf-8") + b"\n\n")
            while True:
                try:
                    text = q.get(timeout=15)
                    payload = b"data: " + json.dumps({"text": text}).encode("utf-8") + b"\n\n"
                    write_chunk(payload)
                except queue.Empty:
                    write_chunk(b": ping\n\n")  # heartbeat keeps connection alive
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            broadcaster.unsubscribe(q)


# --------------------------------------------------------------------------- #
def main():
    try:
        sys.stdout.reconfigure(errors="replace")   # never crash printing to a non-UTF-8 console
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Down the Rabbit Hole - VN texthooker server")
    ap.add_argument("--port", type=int, default=3939)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    if not os.path.isfile(DB_PATH):
        print("dict.sqlite not found. Run:  python setup.py")
        sys.exit(1)
    if not os.path.isfile(os.path.join(STATIC_DIR, "kuromoji", "kuromoji.js")):
        print("kuromoji tokenizer missing. Run:  python setup.py")
        sys.exit(1)

    if sys.platform != "win32":
        print("Warning: clipboard monitoring only works on Windows. "
              "The UI and dictionary still work; paste text manually.")

    threading.Thread(target=clipboard_monitor, args=(PAUSED,), daemon=True).start()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"Down the Rabbit Hole - running at {url}")
    print("Clipboard monitoring is ON. Copy Japanese text (or hook a game with "
          "Textractor) and it will appear in the browser.")
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
        server.shutdown()


if __name__ == "__main__":
    main()
