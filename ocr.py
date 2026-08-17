"""
OCR text source — the fallback for games Textractor can't hook.

The user drags a box over the game's text area once (tkinter overlay, run as a
subprocess so it can't fight pywebview's main thread). A monitor thread then
screenshots that region (ctypes GDI, no dependencies), skips unchanged frames by
pixel hash, OCRs changed ones, and publishes a line only after two consecutive
identical reads — so a VN's typewriter animation doesn't spam partial lines.

Engine: meikiocr (installed by setup.py) — a fast two-stage ONNX detector and
recognizer trained specifically on Japanese video-game text. Models download on
first use. There is deliberately no legacy OCR fallback: startup errors surface
in the UI instead of silently switching to a worse engine.
"""

import collections
import ctypes
import difflib
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib

BASE_DIR = (os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__)))
REGION_PATH = os.path.join(BASE_DIR, "ocr_region.json")
_TMP_BMP = os.path.join(tempfile.gettempdir(), "rabbithole_ocr.bmp")

if sys.platform == "win32":
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    _SRCCOPY = 0x00CC0020

    class _BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
            ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
            ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]


# --------------------------------------------------------------------------- #
# Region persistence (survives restarts — reselecting every launch is misery)
# --------------------------------------------------------------------------- #
def load_region():
    try:
        with open(REGION_PATH, encoding="utf-8") as f:
            r = json.load(f)
        if all(isinstance(r.get(k), int) for k in ("x", "y", "w", "h")) and r["w"] > 0 and r["h"] > 0:
            return r
    except (OSError, ValueError):
        pass
    return None


def save_region(r):
    try:
        with open(REGION_PATH, "w", encoding="utf-8") as f:
            json.dump(r, f)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Screen capture (GDI): region -> 32-bit top-down BMP file + raw pixels
# --------------------------------------------------------------------------- #
def capture_bmp(x, y, w, h, path, scale=None):
    """Screenshot the region into a .bmp; returns the raw pixel bytes (for the
    cheap changed-frame hash). Windows only.

    MeikiOCR performs its own fixed-size detection/recognition preprocessing,
    so the live OCR capture stays 1:1. Pass `scale` explicitly (may be < 1) to
    override — window snapshots for Anki cards downscale instead."""
    if scale is None:
        scale = 1
    sw, sh = int(w * scale), int(h * scale)
    hdc = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, sw, sh)
    old = gdi32.SelectObject(mem, bmp)
    try:
        if scale == 1:
            gdi32.BitBlt(mem, 0, 0, w, h, hdc, x, y, _SRCCOPY)
        else:
            gdi32.SetStretchBltMode(mem, 4)          # HALFTONE
            gdi32.SetBrushOrgEx(mem, 0, 0, None)
            gdi32.StretchBlt(mem, 0, 0, sw, sh, hdc, x, y, w, h, _SRCCOPY)
        bih = _BITMAPINFOHEADER(biSize=40, biWidth=sw, biHeight=-sh,  # negative = top-down
                                biPlanes=1, biBitCount=32, biCompression=0)
        buf = (ctypes.c_char * (sw * sh * 4))()
        gdi32.GetDIBits(mem, bmp, 0, sh, buf, ctypes.byref(bih), 0)
        pixels = bytes(buf)
    finally:
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(None, hdc)
    with open(path, "wb") as f:
        f.write(struct.pack("<2sIHHI", b"BM", 54 + len(pixels), 0, 0, 54))
        f.write(bytes(bih))
        f.write(pixels)
    return pixels


# --------------------------------------------------------------------------- #
# Whole-window snapshot (for Anki cards): the full game scene, not just the
# OCR text box. PNG is encoded with zlib by hand — no PIL requirement.
# --------------------------------------------------------------------------- #
def _window_rect_at(x, y):
    """(x, y, w, h) of the top-level window under a desktop point, or None."""
    user32.WindowFromPoint.restype = wintypes.HWND
    hwnd = user32.WindowFromPoint(wintypes.POINT(x, y))
    if not hwnd:
        return None
    root = user32.GetAncestor(hwnd, 2) or hwnd    # GA_ROOT
    r = wintypes.RECT()
    if not user32.GetWindowRect(root, ctypes.byref(r)):
        return None
    return (r.left, r.top, r.right - r.left, r.bottom - r.top)


def _window_rect_for_pid(pid):
    """Biggest visible top-level window of a process (the hooked game)."""
    rects = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lp):
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and user32.IsWindowVisible(hwnd):
            r = wintypes.RECT()
            if user32.GetWindowRect(hwnd, ctypes.byref(r)):
                rects.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
        return True

    user32.EnumWindows(cb, 0)
    rects = [r for r in rects if r[2] >= 100 and r[3] >= 100]
    return max(rects, key=lambda r: r[2] * r[3]) if rects else None


def _encode_png(pixels_bgra, w, h):
    """Minimal PNG (RGBA, filter 0) from GDI's 32-bit BGRA pixels."""
    b = bytearray(pixels_bgra)
    b[0::4], b[2::4] = b[2::4], b[0::4]      # BGRA -> RGBA
    b[3::4] = b"\xff" * (len(b) // 4)        # GDI leaves alpha 0 = transparent
    stride = w * 4
    raw = b"".join(b"\x00" + bytes(b[y * stride:(y + 1) * stride]) for y in range(h))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data)))

    return (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(bytes(raw), 6)) +
            chunk(b"IEND", b""))


_TMP_SNAP = os.path.join(tempfile.gettempdir(), "rabbithole_snap.bmp")
# ^ NOT _TMP_BMP: /snap runs on an HTTP thread while the OCR loop keeps
# rewriting _TMP_BMP — sharing the path would corrupt an in-flight OCR read.


def snap_window_png(region=None, pid=None):
    """PNG bytes of the whole game window: the window under the OCR region's
    center, else the hooked process's biggest visible window. None when
    neither source identifies a game (clipboard-only sessions)."""
    if sys.platform != "win32":
        return None
    rect = None
    if region:
        rect = _window_rect_at(region["x"] + region["w"] // 2,
                               region["y"] + region["h"] // 2)
    if rect is None and pid:
        rect = _window_rect_for_pid(pid)
    if rect is None:
        return None
    x, y, w, h = rect
    if w < 50 or h < 50:
        return None
    scale = min(1.0, 1280 / w)    # cards don't need 4K; keeps PNGs ~100-400 KB
    try:
        px = capture_bmp(x, y, w, h, _TMP_SNAP, scale=scale)
    except Exception:
        return None
    return _encode_png(px, int(w * scale), int(h * scale))


# --------------------------------------------------------------------------- #
# Region picker — fullscreen translucent overlay, drag a rectangle.
# Runs as a SUBPROCESS (tkinter + pywebview must not share a main thread).
# --------------------------------------------------------------------------- #
def pick_region_main():
    """Entry point for the picker subprocess: prints {"x","y","w","h"} JSON on
    success, nothing on cancel (Esc / click without drag). The overlay spans the
    whole VIRTUAL screen (every monitor) — "-fullscreen" only covered the
    primary, so a game on a second monitor couldn't be picked. Coordinates are
    kept in the same DPI-unaware space the capture side uses."""
    import tkinter as tk
    root = tk.Tk()
    # Virtual-screen bounds (can start at negative x/y when a monitor sits
    # left of / above the primary). SM_*VIRTUALSCREEN = 76-79.
    try:
        gsm = ctypes.windll.user32.GetSystemMetrics
        vx, vy, vw, vh = gsm(76), gsm(77), gsm(78), gsm(79)
    except Exception:                        # non-Windows: primary screen only
        vx = vy = 0
        vw, vh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.overrideredirect(True)              # no decorations; spans monitors
    root.geometry(f"{vw}x{vh}+{vx}+{vy}")
    root.attributes("-alpha", 0.3)
    root.attributes("-topmost", True)
    root.configure(bg="black", cursor="crosshair")
    cv = tk.Canvas(root, bg="black", highlightthickness=0)
    cv.pack(fill="both", expand=True)
    # Canvas coords = screen coords - (vx, vy). Instructions on the primary.
    cv.create_text(-vx + root.winfo_screenwidth() // 2, -vy + 60, fill="white",
                   font=("Segoe UI", 16),
                   text="Drag a box over the game's TEXT area (skip the UI) — Esc cancels")
    sel = {"x0": None, "y0": None, "rect": None}

    def press(e):
        sel["x0"], sel["y0"] = e.x_root, e.y_root
        sel["rect"] = cv.create_rectangle(e.x_root - vx, e.y_root - vy,
                                          e.x_root - vx, e.y_root - vy,
                                          outline="#5f93de", width=3)

    def drag(e):
        if sel["rect"] is not None:
            cv.coords(sel["rect"], sel["x0"] - vx, sel["y0"] - vy,
                      e.x_root - vx, e.y_root - vy)

    def release(e):
        if sel["x0"] is None:
            return
        x = min(sel["x0"], e.x_root)
        y = min(sel["y0"], e.y_root)
        w = abs(e.x_root - sel["x0"])
        h = abs(e.y_root - sel["y0"])
        root.destroy()
        if w >= 20 and h >= 20:
            print(json.dumps({"x": x, "y": y, "w": w, "h": h}))

    cv.bind("<ButtonPress-1>", press)
    cv.bind("<B1-Motion>", drag)
    cv.bind("<ButtonRelease-1>", release)
    root.bind("<Escape>", lambda e: root.destroy())
    # overrideredirect windows don't get keyboard focus by default — Esc needs it
    root.after(50, root.focus_force)
    root.mainloop()


def pick_region_subprocess():
    """Run the overlay picker in its own process; returns the region dict or None.
    Frozen builds re-invoke the exe with --pick-region (handled in server.main)."""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--pick-region"]
    else:
        cmd = [sys.executable, os.path.abspath(__file__), "--pick-region"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                             encoding="utf-8").stdout.strip()
        return json.loads(out.splitlines()[-1]) if out else None
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return None


# --------------------------------------------------------------------------- #
# MeikiOCR engine
# --------------------------------------------------------------------------- #
def _median(values):
    """Small dependency-free median for OCR line-size classification."""
    values = sorted(values)
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2


def _filter_furigana_lines(lines, ratio=0.65):
    """Drop unusually small OCR lines, separately by writing direction.

    Meikipop does this before joining detected lines into paragraphs: ruby is
    usually a separate, much thinner/shorter detection.  A median baseline is
    less likely than the old tallest-line heuristic to discard a legitimate
    speaker label when an oversized title or UI element enters the region.
    Single-line groups are always retained.
    """
    keep = []
    for vertical in (False, True):
        group = [line for line in lines if bool(line.get("vertical")) == vertical]
        if len(group) < 2:
            keep.extend(group)
            continue
        sizes = [line["w"] if vertical else line["h"] for line in group]
        threshold = _median(sizes) * ratio
        keep.extend(line for line, size in zip(group, sizes) if size >= threshold)
    return keep


class MeikiOcr:
    """Meikipop-inspired OCR using its purpose-built ``meikiocr`` backend.

    The backend detects text lines on the complete selected region, then batch
    recognizes the detected crops as individual characters. Character boxes
    provide reliable line geometry for furigana filtering and reading order.
    """

    name = "meikiocr"
    _DET_THRESHOLD = 0.5
    _REC_THRESHOLD = 0.1
    _PUNCT_CONF_FACTOR = 0.2

    def __init__(self):
        import cv2
        import numpy as np
        from meikiocr import MeikiOCR

        self._cv2 = cv2
        self._np = np
        self._ocr = MeikiOCR()
        provider = getattr(self._ocr, "active_provider", None)
        if provider:
            self.name = "meikiocr (" + provider.replace("ExecutionProvider", "") + ")"
        self.line_trace = []
        # The monitor asks for the same frozen frame again to confirm stable
        # text. Cache complete frame reads so confirmation costs a hash, not a
        # second ONNX pass; animated frames still have distinct keys.
        self._cache = collections.OrderedDict()

    @staticmethod
    def _format_results(results):
        lines = []
        for index, result in enumerate(results or []):
            text = str(result.get("text") or "").strip()
            chars = [char for char in (result.get("chars") or [])
                     if isinstance(char, dict)
                     and isinstance(char.get("bbox"), (list, tuple))
                     and len(char["bbox"]) == 4]
            if not text or not chars or not _has_japanese(text):
                continue
            x1 = min(char["bbox"][0] for char in chars)
            y1 = min(char["bbox"][1] for char in chars)
            x2 = max(char["bbox"][2] for char in chars)
            y2 = max(char["bbox"][3] for char in chars)
            w, h = max(1, x2 - x1), max(1, y2 - y1)
            confs = [float(char["conf"]) for char in chars
                     if isinstance(char.get("conf"), (int, float))]
            lines.append({"text": text, "x": x1, "y": y1, "w": w, "h": h,
                          "vertical": bool(result.get("is_vertical", h > w)),
                          "conf": sum(confs) / len(confs) if confs else None,
                          "index": index})

        lines = _filter_furigana_lines(lines)
        vertical = sum(bool(line["vertical"]) for line in lines)
        if lines and vertical > len(lines) / 2:
            # Japanese vertical columns are read from right to left.
            lines.sort(key=lambda line: (-line["x"], line["y"]))
        else:
            lines.sort(key=lambda line: (line["y"], line["x"]))
        trace = [{"engine": "meikiocr", "pick": line["text"],
                  "conf": line["conf"],
                  "box": [line["x"], line["y"], line["w"], line["h"]]}
                 for line in lines]
        return "\n".join(line["text"] for line in lines), trace

    def recognize(self, bmp_path):
        try:
            with open(bmp_path, "rb") as image_file:
                data = image_file.read()
        except OSError:
            return ""
        key = hashlib.md5(data).digest()
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            text, self.line_trace = cached
            return text

        image = self._cv2.imdecode(
            self._np.frombuffer(data, dtype=self._np.uint8), self._cv2.IMREAD_COLOR)
        if image is None:
            return ""
        results = self._ocr.run_ocr(
            image, det_threshold=self._DET_THRESHOLD,
            rec_threshold=self._REC_THRESHOLD,
            punct_conf_factor=self._PUNCT_CONF_FACTOR)
        text, self.line_trace = self._format_results(results)
        self._cache[key] = (text, self.line_trace)
        if len(self._cache) > 64:
            self._cache.popitem(last=False)
        return text

    def close(self):
        pass


def make_engine():
    try:
        return MeikiOcr()
    except ImportError as e:
        raise RuntimeError("MeikiOCR is missing — run: python setup.py --ocr") from e
    except Exception as e:
        raise RuntimeError("MeikiOCR could not start: " + str(e)) from e


# --------------------------------------------------------------------------- #
# The OCR text source
# --------------------------------------------------------------------------- #
_JP_RE = re.compile(r"[ぁ-ゖァ-ヶー一-鿿々]")   # real kana/kanji — NOT ・ or 。


def _has_japanese(s):
    """True when the text is substantially Japanese — at least a third of its
    letter-like chars. 'Any single Japanese char' let English UI text through
    whenever ONE glyph misread as a kanji (ⅱ冊 → gate open → the reader got a
    fullwidth transcription of another window)."""
    jp = len(_JP_RE.findall(s))
    letters = sum(1 for ch in s if ch.isalnum())
    return jp > 0 and jp * 3 >= letters


# Blinking click-to-continue cursors OCR as stray marks at the line's edges —
# strip them. Sentence enders (。！？…) are NOT in this set.
_EDGE_JUNK = "・･•‥▼▽►◄▶◀◆◇■□●○◎◉⊙⊚★☆♦♢»«‹›"


def _clean(text):
    """Normalize MeikiOCR output into one reader line and trim UI artifacts."""
    text = text.replace("\n", "").replace(" ", "").replace("　", "")
    text = re.sub(r"[-−－](?=[一-鿿])", "一", text)
    # Some fonts/recognizers render VN ellipses as runs of dots — map back.
    text = re.sub(r"[.．]{3,}", "……", text)
    text = text.strip(_EDGE_JUNK).strip()
    # A short ASCII/dash tail after Japanese is almost always UI junk — a page
    # marker or a cursor glyph read as "-6". Drop it (then re-strip any mark it
    # was hiding). Pure-ASCII lines are left for the _has_japanese gate to reject.
    if _has_japanese(text):
        text = re.sub(r"[-−–—_=+~A-Za-z0-9]{1,6}$", "", text).strip(_EDGE_JUNK).strip()
    return text


# Trailing chars stripped to form a dedup KEY. OCR catches the sentence-ending 。
# only intermittently, so one line reads as "…した" then "…した。" frame to frame;
# both share a key, so the line is published once (upgraded when the maru lands).
_TRAIL = "。．.｡、，,！？!?…‥・ 　　\n"


def _norm(text):
    return text.rstrip(_TRAIL)


def _merge_reads(a, b):
    """The best single line obtainable from two reads of the same on-screen
    text. Containment picks the fuller read (covers end-growth した→した。,
    front-growth 油断…→……く、油断…, and shorter re-reads); a substantial
    head/tail overlap — at least half the shorter read — splices reads that
    each miss a different end into one superstring. None means the reads
    differ some other way (kana-flip jitter): nothing upgradeable."""
    if b in a:
        return a
    if a in b:
        return b
    lo = max(4, (min(len(a), len(b)) + 1) // 2)
    for k in range(min(len(a), len(b)), lo - 1, -1):
        if a.endswith(b[:k]):
            return a + b[k:]
        if b.endswith(a[:k]):
            return b + a[k:]
    return None


def _same_line(a, b):
    """Two OCR reads of (probably) the same on-screen line? Poor OCR flips one
    kana per read (だ↔た, 葵 dropped…), which on a short line barely dents the
    similarity ratio — so the threshold loosens as the line shrinks. A short
    string fully contained in a longer one is a truncated re-read, also same."""
    if a == b:
        return True
    short, long = sorted((a, b), key=len)
    if short and short in long:
        return True
    thr = 0.7 if len(long) <= 6 else 0.82
    return difflib.SequenceMatcher(None, a, b).ratio() >= thr


class OcrSource:
    """Screenshot-diff-OCR loop. publish() is server.publish_line — it dedupes,
    logs and broadcasts like every other text source."""

    def __init__(self, publish, paused_flag):
        self._publish = publish
        self._paused = paused_flag
        self.region = load_region()
        self.running = False
        self.starting = False
        self.engine_name = None
        self.error = None
        self.trace = {}   # last peek/read/publish — live debugging via /ocr
        self._stop = threading.Event()
        self._thread = None

    def state(self):
        return {"running": self.running, "starting": self.starting,
                "region": self.region, "engine": self.engine_name, "error": self.error,
                "trace": self.trace}

    # -- field debug data (logs/ is gitignored — stays on this PC) ---------- #
    # Every OCR decision appends to logs/ocr-debug-YYYY-MM-DD.jsonl so a real
    # session can be diagnosed without exposing its text in the repository.
    def _debug(self, event, **kw):
        try:
            d = os.path.join(BASE_DIR, "logs")
            os.makedirs(d, exist_ok=True)
            rec = {"t": time.strftime("%H:%M:%S"), "e": event, **kw}
            path = os.path.join(d, "ocr-debug-" + time.strftime("%Y-%m-%d") + ".jsonl")
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def set_region(self, region):
        self.region = region
        save_region(region)

    def start(self):
        if self.running or self.starting:
            return None
        if not self.region:
            return "select the text area first"
        if sys.platform != "win32":
            return "OCR capture is Windows-only"
        self.error = None
        self.starting = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return None

    def stop(self):
        self._stop.set()

    def _loop(self):
        engine = None
        try:
            engine = make_engine()          # slow on first start: model load/download
            self.engine_name = engine.name
            self.starting = False
            self.running = True
            last_hash = None
            seen = collections.deque(maxlen=2)     # unconfirmed OCR signatures
            handled = collections.deque(maxlen=4)  # signatures already read
            recent = collections.deque(maxlen=6)   # (key, raw) of recent publishes
            while not self._stop.is_set():
                time.sleep(0.3)
                if self._paused.is_set():
                    continue
                r = self.region
                try:
                    px = capture_bmp(r["x"], r["y"], r["w"], r["h"], _TMP_BMP)
                except Exception:
                    continue
                h = hashlib.md5(px).digest()
                # Unconfirmed text must get its confirming read even if the
                # pixels froze right after it appeared (scene back from a white
                # fade, nothing blinking in-region) — skipping on hash alone
                # left that text pending forever.
                if h == last_hash and not seen:
                    continue
                last_hash = h
                # A new MeikiOCR result must repeat among the last two reads.
                # Its frame cache makes the frozen-frame confirmation cheap,
                # while mid-typewriter frames keep changing and never qualify.
                text = _clean(engine.recognize(_TMP_BMP))
                self.trace["peek"] = text
                if not _has_japanese(text):
                    seen.clear()
                    continue
                if text in handled:         # blink re-showing processed text
                    continue
                if text not in seen:
                    seen.append(text)
                    continue
                seen.clear()
                handled.append(text)
                self.trace["read"] = text
                lt = engine.line_trace
                self.trace["lines"] = lt
                self._debug("read", text=text, lines=lt)
                if not text or not _has_japanese(text):
                    if text:
                        self._debug("gate_drop", text=text)
                    continue
                # Jitter guards against the last few published lines. A blinking
                # cursor re-OCRs the same screen text repeatedly, each read a bit
                # different (だ/た, cursor dot, and above all the 。 blinking in
                # and out). Compare on a KEY with trailing punctuation stripped so
                # "…した" and "…した。" count as one line:
                if any(text == raw for _, raw in recent):
                    continue                                   # exact re-read
                key = _norm(text)
                if not key:
                    continue
                # Reads of the same on-screen line (punctuation flicker, a kana
                # misread, a blinking ◎ cursor) collapse via _same_line. A read
                # that RECONCILES with the fullest version already shown — grows
                # it at either end, or splices with it when each read missed a
                # different end — publishes the merged superstring, which the
                # reader swaps in place of the partial. Un-mergeable re-reads
                # of the same line are jitter.
                same = [raw for k, raw in recent if _same_line(key, k)]
                if same:
                    longest = max(same, key=len)
                    merged = _merge_reads(longest, text)
                    if merged is None or merged == longest:
                        self._debug("jitter_drop", text=text, kept=longest)
                        continue
                    text = merged
                    recent = collections.deque(
                        ((k, r) for k, r in recent if r != longest), maxlen=6)
                    self._debug("merge", merged=text, was=longest)
                self._publish(text)
                recent.append((_norm(text), text))
                self.trace["published"] = text
                self._debug("publish", text=text)
        except Exception as e:
            self.error = str(e)
        finally:
            if engine:
                engine.close()
            self.running = False
            self.starting = False
            self.engine_name = None


if __name__ == "__main__" and "--pick-region" in sys.argv:
    pick_region_main()
