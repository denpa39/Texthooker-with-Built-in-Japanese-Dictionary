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
import base64
import ctypes
import functools
import json
import os
import queue
import socket
import sqlite3
import struct
import sys
import threading
import time
import urllib.request
import webbrowser
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import deinflect
import hook

# Frozen (PyInstaller) builds live next to their data: static/, dict.sqlite and
# textractor/ sit beside the .exe, not inside the unpacked bundle.
BASE_DIR = (os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DB_PATH = os.path.join(BASE_DIR, "dict.sqlite")
LOG_DIR = os.path.join(BASE_DIR, "logs")

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

_last_published = [None]


def _log_line(text):
    """Append every published line to a per-day file on disk (named after the
    attached game when hooking). Survives a browser-storage wipe and the reader's
    300-line DOM cap."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        game = os.path.splitext(hooker.exe)[0] if hooker.exe else ""
        game = "".join(ch for ch in game if ch not in '\\/:*?"<>|').strip()
        fname = time.strftime("%Y-%m-%d") + (f" - {game}" if game else "") + ".txt"
        with open(os.path.join(LOG_DIR, fname), "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError:
        pass


def publish_line(text):
    """Single entry point for all text sources (clipboard + embedded Textractor +
    websocket): clean the classic hook artifacts, drop consecutive repeats,
    log to disk, broadcast."""
    text = clean_hook_text((text or "").strip())
    if not text or text == _last_published[0]:
        return
    _last_published[0] = text
    _log_line(text)
    broadcaster.publish(text)


hooker = hook.Hooker(publish_line)


def clean_hook_text(text):
    """Undo the two classic Textractor artifacts: the whole line emitted twice
    back-to-back (ABCABC), and every character doubled (ああねね). The artifacts
    affect whole hooked sentences, so short texts are left alone — otherwise real
    reduplicated words the user copies by hand (ばらばら, はいはい) would be halved."""
    n = len(text)
    if n >= 10 and n % 2 == 0:
        h = n // 2
        if text[:h] == text[h:]:
            text = text[:h]
            n = h
    if n >= 8 and n % 2 == 0 and all(text[i] == text[i + 1] for i in range(0, n, 2)):
        text = text[::2]
    return text


def clipboard_monitor(paused_flag):
    # Seed with the CURRENT sequence number: whatever already sits in the clipboard
    # predates the app and must not be published (it used to appear — and get
    # logged — on every launch).
    try:
        last_seq = user32.GetClipboardSequenceNumber() if sys.platform == "win32" else None
    except Exception:
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
                    publish_line(text)
        time.sleep(0.3)


PAUSED = threading.Event()  # set => clipboard monitoring paused


# --------------------------------------------------------------------------- #
# WebSocket text input (Textractor's websocket plugin :6677, Agent :9001,
# LunaTranslator…). Those tools run a tiny WS *server*; we connect out to it as
# a client — no clipboard race, works when another app owns the clipboard.
# Minimal RFC 6455 client, stdlib only.
# --------------------------------------------------------------------------- #
def _ws_send(sock, opcode, payload=b""):
    """One masked client frame (clients MUST mask, RFC 6455 §5.3)."""
    mask = os.urandom(4)
    n = len(payload)
    if n < 126:
        header = bytes([0x80 | opcode, 0x80 | n])
    elif n < 65536:
        header = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack(">H", n)
    else:
        header = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack(">Q", n)
    sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


def _ws_messages(url):
    """Connect to a websocket URL, yield each complete text message. Raises on
    any connection problem — the caller reconnects."""
    u = urlparse(url)
    sock = socket.create_connection((u.hostname, u.port or 80), timeout=4)
    try:
        sock.settimeout(45)
        f = sock.makefile("rb")
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((
            f"GET {u.path or '/'} HTTP/1.1\r\n"
            f"Host: {u.hostname}:{u.port or 80}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        status = f.readline()
        if b"101" not in status:
            raise OSError("websocket handshake refused")
        while f.readline() not in (b"\r\n", b""):
            pass
        msg = b""
        while True:
            try:
                hdr = f.read(2)
            except socket.timeout:
                _ws_send(sock, 0x9)          # idle: ping to probe the connection
                continue
            if len(hdr) < 2:
                return
            fin, opcode = hdr[0] & 0x80, hdr[0] & 0x0F
            ln = hdr[1] & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", f.read(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", f.read(8))[0]
            if hdr[1] & 0x80:                # masked server frame (non-standard)
                mask = f.read(4)
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(f.read(ln)))
            else:
                payload = f.read(ln)
            if opcode == 0x8:                # close
                return
            if opcode == 0x9:                # ping -> pong
                _ws_send(sock, 0xA, payload)
                continue
            if opcode in (0x0, 0x1):
                msg += payload
                if fin:
                    yield msg.decode("utf-8", "replace")
                    msg = b""
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _ws_extract_text(msg):
    """Sources send plain text or a small JSON envelope; pull the sentence out."""
    msg = (msg or "").strip()
    if msg.startswith("{"):
        try:
            j = json.loads(msg)
            for k in ("sentence", "text", "data", "message"):
                if isinstance(j.get(k), str):
                    return j[k]
            return ""
        except ValueError:
            pass
    return msg


def websocket_monitor(url, paused_flag):
    """One reconnect-forever reader per websocket source. Quiet when the source
    isn't running; announces connects/drops so the user can tell it's live."""
    was_connected = False
    while True:
        try:
            for msg in _ws_messages(url):
                if not was_connected:
                    was_connected = True
                    print(f"WebSocket source connected: {url}")
                if not paused_flag.is_set():
                    text = _ws_extract_text(msg)
                    if text:
                        publish_line(text)
        except Exception:
            # Not just OSError: a truncated frame raises struct.error, a garbled
            # one UnicodeDecodeError… anything here means "reconnect", never
            # "kill the reader thread forever".
            pass
        if was_connected:
            print(f"WebSocket source lost: {url} (retrying)")
            was_connected = False
        time.sleep(5)


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


def _fetch_entries_batch(terms):
    """Same lookup as _fetch_entries (direct match, else hiragana fallback), for many
    terms in one query instead of one query per term — scan() checks up to 32 prefixes
    per hover, so this turns dozens of round trips into a single one."""
    terms = list(dict.fromkeys(t for t in terms if t))
    if not terms:
        return {}
    hira_of = {t: to_hiragana(t) for t in terms}
    query_terms = set(terms) | {h for h in hira_of.values()}
    db = get_db()
    placeholders = ",".join("?" * len(query_terms))
    rows = db.execute(
        f"SELECT t.term, e.id, e.json, e.freq FROM terms t JOIN entries e ON e.id = t.id "
        f"WHERE t.term IN ({placeholders})", tuple(query_terms)).fetchall()
    by_term = {}
    for term, eid, js, freq in rows:
        by_term.setdefault(term, []).append((freq, eid, js))
    result = {}
    for t in terms:
        group = by_term.get(t) or by_term.get(hira_of[t]) or []
        group = sorted(group, key=lambda row: -row[0])[:80]
        out, seen = [], set()
        for _freq, eid, js in group:
            if eid not in seen:
                seen.add(eid)
                out.append(json.loads(js))
        result[t] = out
    return result


def _fetch_names_batch(prefixes):
    """Batched form of _fetch_names — one query for every prefix scan() considers."""
    prefixes = list(dict.fromkeys(prefixes))
    if not prefixes:
        return {}
    db = get_db()
    placeholders = ",".join("?" * len(prefixes))
    try:
        rows = db.execute(
            f"SELECT t.term, n.id, n.json FROM nameterms t JOIN names n ON n.id = t.id "
            f"WHERE t.term IN ({placeholders}) ORDER BY n.id", tuple(prefixes)).fetchall()
    except sqlite3.OperationalError:
        return {p: [] for p in prefixes}
    by_term = {}
    for term, nid, js in rows:
        by_term.setdefault(term, []).append((nid, js))
    result = {}
    for p in prefixes:
        out, seen = [], set()
        for nid, js in by_term.get(p, []):
            if nid not in seen:
                seen.add(nid)
                out.append(json.loads(js))
                if len(out) >= 8:
                    break
        result[p] = out
    return result


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
    # ...and the sharper per-element signal: JMdict marks priority on each *specific*
    # reading (口 is common as くち but not as こう), so a word whose matched reading
    # carries the priority tag beats one that only matches on an obscure reading.
    read_pri_ok = bool(reading_h and
                       any(to_hiragana(r) == reading_h for r in e.get("rc", [])))
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
        0 if read_pri_ok else 1,           # 7. the matched reading carries JMdict priority
        0 if pref_ok else 1,               # 8. known dominant kanji for a kana verb (居る > 射る)
        len(c["reasons"]),                 # 9. fewer de-inflection steps (exact > inflected)
        -common_lvl, -common_mag,          # 10. more common (JMdict-common tier > raw VN rank)
        -c["len"],                         # 11. true length
        e.get("id", ""),                   # 12. stable final tiebreak -> deterministic order
    )


def lookup(term, pos=None, reading=None):
    """Rank every entry whose spelling equals `term` (used by the /lookup route)."""
    if not term:
        return []
    reading_h = to_hiragana(reading) if reading else None
    pref = _KANA_PREF.get(term) or _KANA_PREF.get(to_hiragana(term))
    cands = [{"len": len(term), "reasons": [], "kind": "word", "entry": e}
             for e in _fetch_entries_batch([term]).get(term, [])]
    cands.sort(key=lambda c: _sort_key(c, len(term), pos, reading_h, pref))
    return [c["entry"] for c in cands]


@functools.lru_cache(maxsize=1024)   # each hit holds ~12 full JSON entries; 1k ≈ plenty for a session
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

    # Pass 1 (pure Python, no DB): de-inflect every prefix up front so the two DB
    # tables are each hit once for the whole hover instead of once per prefix —
    # a 14-char sentence used to cost 30-100ms in round trips, now ~2ms.
    prefixes = [text[:end] for end in range(len(text), 0, -1)]
    forms_by_prefix = {p: deinflect.deinflect(p) for p in prefixes}
    all_forms = {f for forms in forms_by_prefix.values() for f in forms}
    if base:
        all_forms.add(base)
    entries_by_form = _fetch_entries_batch(all_forms)
    names_by_prefix = _fetch_names_batch(prefixes)

    cands, seen = [], set()
    for end, prefix in zip(range(len(text), 0, -1), prefixes):
        for form, reasons in forms_by_prefix[prefix].items():
            # deinflect() lists reasons outermost-first (the order rules peeled off);
            # reverse so the displayed trail reads stem-outward, the way a learner
            # derives it: 食べる › causative › passive › past (not past › passive › …).
            trail = list(reversed(reasons))
            for e in entries_by_form.get(form, []):
                key = ("w", e["id"])
                if key not in seen:
                    seen.add(key)
                    cands.append({"len": end, "matched": prefix, "reasons": trail,
                                  "kind": "word", "entry": e})
        for e in names_by_prefix.get(prefix, []):
            key = ("n", e["id"])
            if key not in seen:
                seen.add(key)
                cands.append({"len": end, "matched": prefix, "reasons": [],
                              "kind": "name", "entry": e})

    # Safety net: the tokenizer's dictionary form, in case the de-inflector can't
    # reach it (bare ichidan stems written in kanji: 見 -> 見る). Ranked like any
    # short match — longer real matches still rank above it.
    if base:
        for e in entries_by_form.get(base, []):
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


def kanji_info(ch):
    """KANJIDIC2 card for one kanji (None without the table / unknown char)."""
    if not ch:
        return None
    db = get_db()
    try:
        row = db.execute("SELECT json FROM kanji WHERE literal = ?", (ch[0],)).fetchone()
    except sqlite3.OperationalError:
        return None
    return json.loads(row[0]) if row else None


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".ico": "image/x-icon",
    ".gz": "application/gzip",  # kuromoji *.dat.gz: gzip MIME type, not octet-stream (prevents download dialog)
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter logging
        pass

    # -- helpers ----------------------------------------------------------- #
    def _send_bytes(self, body, content_type, status=200, cache=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Default no-cache: the app window (WebView2) otherwise happily serves a
        # stale app.js/index.html after an update — "new feature isn't working".
        # Immutable assets (kuromoji dict, fonts — ~26 MB) override this so they
        # aren't re-downloaded and re-parsed on every single startup.
        self.send_header("Cache-Control", cache or "no-cache")
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
        if not full.startswith(BASE_DIR + os.sep) or not os.path.isfile(full):
            self._send_bytes(b"Not found", "text/plain; charset=utf-8", 404)
            return
        ext = os.path.splitext(full)[1].lower()
        ctype = CONTENT_TYPES.get(ext, "application/octet-stream")
        with open(full, "rb") as f:
            body = f.read()
        # kuromoji's dictionary and the bundled fonts never change between setups —
        # let the webview keep them for a year instead of refetching 26 MB per launch.
        rel = rel_path.replace("\\", "/")
        immutable = rel.startswith(("/static/kuromoji/", "/static/fonts/"))
        self._send_bytes(body, ctype,
                         cache="public, max-age=31536000, immutable" if immutable else None)

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
        elif path == "/kanji":
            qs = parse_qs(parsed.query)
            ch = (qs.get("c") or [""])[0]
            self._send_json({"kanji": ch[:1], "info": kanji_info(ch)})
        elif path == "/state":
            self._send_json({"paused": PAUSED.is_set()})
        elif path == "/processes":
            self._send_json({"available": hook.available(),
                             "processes": hook.list_processes()})
        elif path == "/hooks":
            self._send_json(hooker.state())
        elif path == "/events":
            self._serve_events()
        elif path.startswith("/static/"):
            self._serve_file(path)
        else:
            self._send_bytes(b"Not found", "text/plain; charset=utf-8", 404)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except Exception:
            return {}

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/attach":
            try:
                pid = int(self._read_json_body().get("pid") or 0)
            except (TypeError, ValueError):
                pid = 0
            err = hooker.attach(pid) if pid > 0 else "no pid"
            self._send_json({"error": err, **hooker.state()}, 400 if err else 200)
        elif parsed.path == "/detach":
            hooker.detach()
            self._send_json(hooker.state())
        elif parsed.path == "/hookpick":
            hooker.pick(self._read_json_body().get("key"))
            self._send_json(hooker.state())
        elif parsed.path == "/anki":
            # Proxy to AnkiConnect: the browser page can't call it directly
            # (AnkiConnect's CORS allowlist doesn't include this origin by default).
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                req = urllib.request.Request(
                    "http://127.0.0.1:8765", data=raw,
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    self._send_bytes(resp.read(), "application/json; charset=utf-8")
            except Exception:
                self._send_json(
                    {"result": None,
                     "error": "AnkiConnect unreachable — is Anki running with the AnkiConnect add-on?"},
                    502)
        elif parsed.path == "/export":
            # The app window (WebView2) swallows blob downloads, so the page posts
            # its lines here; we save to exports/ and reveal the file in Explorer.
            lines = self._read_json_body().get("lines") or []
            if not lines:
                self._send_json({"error": "nothing to export"}, 400)
                return
            try:
                exp_dir = os.path.join(BASE_DIR, "exports")
                os.makedirs(exp_dir, exist_ok=True)
                fname = "rabbit-hole-" + time.strftime("%Y-%m-%d-%H-%M-%S") + ".txt"
                path = os.path.join(exp_dir, fname)
                with open(path, "w", encoding="utf-8") as f:
                    f.write("\n".join(str(l) for l in lines) + "\n")
                if sys.platform == "win32":
                    import subprocess
                    subprocess.Popen(["explorer", "/select,", path])
                self._send_json({"path": path})
            except OSError as e:
                self._send_json({"error": str(e)}, 500)
        elif parsed.path == "/pause":
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
def _fatal(msg):
    """Print AND, when running console-less (pythonw / --noconsole exe), show a
    message box — otherwise startup errors would be completely invisible."""
    print(msg)
    if sys.platform == "win32":
        try:
            if ctypes.windll.kernel32.GetConsoleWindow() == 0:
                ctypes.windll.user32.MessageBoxW(None, msg, "Down the Rabbit Hole", 0x10)
        except Exception:
            pass
    sys.exit(1)


def main():
    try:
        sys.stdout.reconfigure(errors="replace")   # never crash printing to a non-UTF-8 console
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Down the Rabbit Hole - VN texthooker server")
    ap.add_argument("--port", type=int, default=3939)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--lan", action="store_true",
                    help="listen on all interfaces so a phone/tablet on the same "
                         "Wi-Fi can read along")
    ap.add_argument("--ws", default="ws://127.0.0.1:6677,ws://127.0.0.1:9001",
                    metavar="URLS",
                    help="comma-separated websocket text sources to read from "
                         "(Textractor websocket plugin :6677, Agent :9001; "
                         "'' disables)")
    ap.add_argument("--browser", action="store_true",
                    help="open in the web browser instead of the app window")
    ap.add_argument("--no-browser", action="store_true",
                    help="serve only; open nothing")
    args = ap.parse_args()
    if args.lan:
        args.host = "0.0.0.0"

    if not os.path.isfile(DB_PATH):
        _fatal("dict.sqlite not found. Run:  python setup.py")
    if not os.path.isfile(os.path.join(STATIC_DIR, "kuromoji", "kuromoji.js")):
        _fatal("kuromoji tokenizer missing. Run:  python setup.py")

    if sys.platform != "win32":
        print("Warning: clipboard monitoring only works on Windows. "
              "The UI and dictionary still work; paste text manually.")

    threading.Thread(target=clipboard_monitor, args=(PAUSED,), daemon=True).start()
    for ws_url in filter(None, (u.strip() for u in args.ws.split(","))):
        threading.Thread(target=websocket_monitor, args=(ws_url, PAUSED),
                         daemon=True).start()

    class QuietServer(ThreadingHTTPServer):
        # On Windows, the default SO_REUSEADDR lets a second launch bind the same
        # port while an older instance still owns it — requests then silently go
        # to the OLD process (stale routes, "it's not working" confusion). Fail
        # loudly instead.
        allow_reuse_address = False

        def handle_error(self, request, client_address):
            # WebView/browser tabs abort in-flight requests on reload — routine,
            # not worth a stack trace. Anything else still prints.
            import traceback
            exc = sys.exception()
            if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
                return
            traceback.print_exc()

    try:
        server = QuietServer((args.host, args.port), Handler)
    except OSError:
        _fatal(f"Port {args.port} is already in use — the app is probably already "
               f"running.\nClose the other window/terminal (or Task Manager -> "
               f"python) and try again,\nor start with a different port:  "
               f"python server.py --port 4040")
    # 0.0.0.0 listens everywhere but isn't a browsable address — open localhost.
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}/"
    print(f"Down the Rabbit Hole - running at {url}")
    if args.host in ("0.0.0.0", "::"):
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect(("8.8.8.8", 80))          # no traffic sent; just picks the LAN interface
            lan_ip = probe.getsockname()[0]
            probe.close()
            print(f"LAN: http://{lan_ip}:{args.port}/  (open on your phone/tablet)")
        except OSError:
            pass
    print("Text sources: Attach button (embedded Textractor), clipboard (Textractor's "
          "Copy to Clipboard extension), or websocket (Textractor/Agent plugins).")

    # Standalone app window (pywebview / Edge WebView2). Falls back to the browser
    # when pywebview isn't installed or fails to open.
    if not (args.browser or args.no_browser):
        try:
            import webview
            threading.Thread(target=server.serve_forever, daemon=True).start()
            webview.create_window("Down the Rabbit Hole", url,
                                  width=1100, height=800, min_size=(420, 500))
            webview.start()          # blocks until the window is closed
            print("Window closed. Stopping.")
            hooker.detach()
            server.shutdown()
            # Hard-exit: any straggler thread (SSE handler, pywebview internals)
            # would otherwise keep a zombie python process squatting the port.
            os._exit(0)
        except ImportError:
            print("(pywebview not installed — falling back to the browser. "
                  "For the app window:  python -m pip install pywebview)")
            args.browser = True
        except Exception as e:
            print(f"(app window failed: {e} — falling back to the browser)")
            args.browser = True

    print("Press Ctrl+C to stop.")
    if args.browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
        hooker.detach()
        os._exit(0)   # same hard exit as the window path — no straggler threads


if __name__ == "__main__":
    main()
