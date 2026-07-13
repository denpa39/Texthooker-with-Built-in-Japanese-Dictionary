"""
OCR text source — the fallback for games Textractor can't hook.

The user drags a box over the game's text area once (tkinter overlay, run as a
subprocess so it can't fight pywebview's main thread). A monitor thread then
screenshots that region (ctypes GDI, no dependencies), skips unchanged frames by
pixel hash, OCRs changed ones, and publishes a line only after two consecutive
identical reads — so a VN's typewriter animation doesn't spam partial lines.

Engines:
  - manga-ocr  (pip install manga-ocr) — transformer model built for Japanese
    game/manga text; by far the best quality. Optional, ~400 MB with torch.
    Generative, so it hallucinates text from no-text frames — always used
    behind a Windows-OCR text-presence gate (HybridOcr).
  - Windows built-in OCR (Windows.Media.Ocr via a persistent PowerShell worker)
    — decent, zero install; needs the Japanese language pack.
"""

import base64
import collections
import ctypes
import difflib
import functools
import hashlib
import json
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib

import deinflect

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

    By default the capture is upscaled 2x (smooth HALFTONE interpolation): VN
    fonts are small, and Windows OCR loses dakuten dots at that size (だ read
    as た). Twice the glyph size recovers them. Very large regions stay 1:1.
    Pass `scale` explicitly (may be < 1) to override — window snapshots for
    Anki cards downscale instead."""
    if scale is None:
        scale = 2 if w * h <= 1_200_000 else 1
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
    success, nothing on cancel (Esc / click without drag)."""
    import tkinter as tk
    root = tk.Tk()
    root.attributes("-fullscreen", True)
    root.attributes("-alpha", 0.3)
    root.attributes("-topmost", True)
    root.configure(bg="black", cursor="crosshair")
    cv = tk.Canvas(root, bg="black", highlightthickness=0)
    cv.pack(fill="both", expand=True)
    cv.create_text(root.winfo_screenwidth() // 2, 60, fill="white",
                   font=("Segoe UI", 16),
                   text="Drag a box over the game's TEXT area (skip the UI) — Esc cancels")
    sel = {"x0": None, "y0": None, "rect": None}

    def press(e):
        sel["x0"], sel["y0"] = e.x_root, e.y_root
        sel["rect"] = cv.create_rectangle(e.x, e.y, e.x, e.y, outline="#5f93de", width=3)

    def drag(e):
        if sel["rect"] is not None:
            cv.coords(sel["rect"], sel["x0"], sel["y0"], e.x_root, e.y_root)

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
# Engines
# --------------------------------------------------------------------------- #
_PS_WORKER = r"""
$ErrorActionPreference = "Stop"
[Console]::InputEncoding = [Text.Encoding]::UTF8
$null = [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
$null = [Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language,Windows.Globalization,ContentType=WindowsRuntime]
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
  $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $t) {
  $task = $asTaskGeneric.MakeGenericMethod($t).Invoke($null, @($op))
  $task.Wait() | Out-Null
  $task.Result
}
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new("ja"))
if (-not $engine) { [Console]::Out.WriteLine("NOJA"); exit 1 }
[Console]::Out.WriteLine("READY")
while ($true) {
  $path = [Console]::In.ReadLine()
  if ($null -eq $path -or $path -eq "") { break }
  try {
    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $dec = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bmp = Await ($dec.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $res = Await ($engine.RecognizeAsync($bmp)) ([Windows.Media.Ocr.OcrResult])
    $lines = @()
    foreach ($l in $res.Lines) {
      $x1 = [double]::MaxValue; $y1 = [double]::MaxValue; $x2 = 0.0; $y2 = 0.0
      $ws = @()
      foreach ($w in $l.Words) {
        $r = $w.BoundingRect
        if ($r.X -lt $x1) { $x1 = $r.X }
        if ($r.Y -lt $y1) { $y1 = $r.Y }
        if ($r.X + $r.Width -gt $x2) { $x2 = $r.X + $r.Width }
        if ($r.Y + $r.Height -gt $y2) { $y2 = $r.Y + $r.Height }
        $ws += @{ x = [int]$r.X; w = [int]$r.Width }
      }
      $lines += @{ t = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($l.Text));
                   x = [int]$x1; y = [int]$y1; w = [int]($x2 - $x1); h = [int]($y2 - $y1);
                   ws = $ws }
    }
    $stream.Dispose()
    $json = ConvertTo-Json -InputObject @($lines) -Compress -Depth 5
    [Console]::Out.WriteLine([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json)))
  } catch {
    [Console]::Out.WriteLine("")
  }
}
"""


def _reading_order(lines):
    """Put OCR lines in reading order and heal Windows' split lines.

    Windows guarantees neither line order nor one-object-per-visual-line: it
    splits a single sentence at an ellipsis into fragments with a couple px
    of y jitter, and a naive (y, x) sort then reorders the sentence
    (でかい……そして -> そして…でかい). Cluster into visual rows by
    vertical-center proximity, order rows top-down and fragments
    left-to-right, and MERGE same-row fragments whose gap is small enough to
    be one sentence run — so the reader gets the sentence whole and
    manga-ocr reads it with full context instead of split-off shards."""
    rows = []
    for l in sorted(lines, key=lambda q: q["y"]):
        for row in rows:
            r0 = row[-1]
            if abs((l["y"] + l["h"] / 2) - (r0["y"] + r0["h"] / 2)) \
                    < 0.6 * max(l["h"], r0["h"]):
                row.append(l)
                break
        else:
            rows.append([l])
    out = []
    for row in rows:
        row.sort(key=lambda q: q["x"])
        merged = [row[0]]
        for l in row[1:]:
            p = merged[-1]
            if l["x"] - (p["x"] + p["w"]) <= 2 * max(p["h"], l["h"]):
                bottom = max(p["y"] + p["h"], l["y"] + l["h"])
                p["text"] += " " + l["text"]
                p["w"] = l["x"] + l["w"] - p["x"]
                p["y"] = min(p["y"], l["y"])
                p["h"] = bottom - p["y"]
                p["ws"] = (p.get("ws") or []) + (l.get("ws") or [])
            else:
                merged.append(l)
        out.extend(merged)
    return out


class WindowsOcr:
    """Persistent PowerShell worker around Windows.Media.Ocr (ja). Spawning
    PowerShell per frame would cost ~300ms; one worker answers in ~50ms."""

    name = "Windows OCR"

    def __init__(self):
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self._proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", _PS_WORKER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=flags)
        line = self._proc.stdout.readline().strip()
        if line != b"READY":
            self.close()
            raise RuntimeError(
                "Windows OCR has no Japanese support on this PC — install the Japanese "
                "language pack (Settings > Language), or:  pip install manga-ocr")

    def recognize_lines(self, bmp_path):
        """OCR lines with their pixel bounding boxes (in the BMP's own,
        already-upscaled coordinate space): [{"text","x","y","w","h"}, ...]."""
        if self._proc.poll() is not None:
            raise RuntimeError("Windows OCR worker died")
        self._proc.stdin.write(bmp_path.encode("utf-8") + b"\r\n")
        self._proc.stdin.flush()
        out = self._proc.stdout.readline().strip()
        if not out:
            return []
        try:
            lines = json.loads(base64.b64decode(out).decode("utf-8", "replace"))
        except ValueError:
            return []
        for l in lines:
            l["text"] = base64.b64decode(l.pop("t")).decode("utf-8", "replace")
        return _reading_order(lines)

    def recognize(self, bmp_path):
        return "\n".join(l["text"] for l in self.recognize_lines(bmp_path))

    # For WindowsOcr the full read IS cheap — peek and recognize coincide.
    peek = recognize

    def close(self):
        try:
            self._proc.kill()
        except OSError:
            pass


class MangaOcr:
    """manga-ocr: the Japanese-specialist model. Import + model load take ~10s,
    done inside the monitor thread so the UI shows 'starting'."""

    name = "manga-ocr"

    def __init__(self):
        from manga_ocr import MangaOcr as _M   # heavy import (torch)
        self._m = _M()

    def recognize(self, img_or_path):
        return self._m(img_or_path)   # manga_ocr takes a path or a PIL Image

    def peek(self, bmp_path):
        return None                   # no cheap text detector without Windows OCR

    def close(self):
        pass


class HybridOcr:
    """Windows OCR finds WHERE the text is, manga-ocr reads WHAT it says.

    manga-ocr is a generative single-text-block model: fed a whole region
    screenshot (backgrounds, multiple lines, art) it invents plausible
    Japanese instead of reading — even when real text is on screen. And its
    ViT encoder resizes everything to 224x224, so a wide thin VN line gets
    squished into garble too. So: each Windows-OCR line containing Japanese
    is split at Windows' word boxes into chunks no wider than ~6x the line
    height, and the chunks are stacked VERTICALLY into one near-square
    canvas that manga-ocr reads in a single call — a shape it was trained
    on (multi-row manga bubbles), and one where its decoder sees the whole
    sentence as context instead of isolated 6-glyph fragments (fragment
    reads swap in plausible wrong chars: 海水 -> 海２). One line per call;
    stuffing the whole frame into one canvas starves the 224x224 input of
    resolution and misreads again. No Japanese found by Windows = frame
    skipped (the hallucination gate)."""

    name = "manga-ocr"
    _MAX_CALLS = 16   # model calls per frame (~0.5s each on CPU)
    _MAX_ROWS = 6     # rows per canvas; more starves the 224x224 resolution

    def __init__(self):
        self._gate = WindowsOcr()
        self._m = MangaOcr()
        self.line_trace = []   # last frame's per-line (win, r1, r2, pick)
        # crop-pixels -> text. A VN screen mostly repeats between frames (a
        # new line appears, old ones don't move), so identical crops skip the
        # model entirely — same pixels, same text, no accuracy risk.
        self._cache = collections.OrderedDict()

    def _spans(self, line, shrink_first=0):
        """Contiguous x-spans tiling the WHOLE line bbox, split at midpoints
        of gaps between word-box groups, each group at most ~6:1 aspect —
        past ~7:1 the 224x224 resize eats glyphs. Contiguity matters: a
        glyph whose word box Windows missed (fancy backgrounds) still lands
        inside some crop. shrink_first narrows the first group so a second
        read places its seams elsewhere (seam-error voting)."""
        max_w = max(6 * line["h"], 200)
        x0, x1 = line["x"], line["x"] + line["w"]
        words = sorted(line.get("ws") or [], key=lambda w: w["x"])
        cuts, start, prev_end = [], x0 - shrink_first, None
        for w in words:
            if prev_end is not None and w["x"] + w["w"] - start > max_w:
                cut = (prev_end + w["x"]) // 2
                if x0 < cut < x1 and (not cuts or cut > cuts[-1]):
                    cuts.append(cut)
                start = w["x"]
            prev_end = w["x"] + w["w"]
        xs = [x0] + cuts + [x1]
        return list(zip(xs, xs[1:]))

    def _lines(self, bmp_path):
        lines = [l for l in self._gate.recognize_lines(bmp_path)
                 if _has_japanese(l["text"])]
        if not lines:
            return []
        # Furigana ruby OCRs as its own tiny lines above the real ones — drop
        # lines under 55% of the tallest so readings don't duplicate into the
        # transcript. (Name labels are ~70% of dialogue size, they survive.)
        tallest = max(l["h"] for l in lines)
        return [l for l in lines if l["h"] >= 0.55 * tallest]

    def peek(self, bmp_path):
        """Cheap (~0.1s) text signature of the frame — the Windows read, no
        manga-ocr. The monitor loop uses it to tell 'text changed' apart from
        'a cursor blinked': blinkers churn pixels every frame, text doesn't."""
        return "\n".join("".join(l["text"].split()) for l in self._lines(bmp_path))

    def recognize(self, bmp_path):
        lines = self._lines(bmp_path)
        if not lines:
            return ""
        from PIL import Image   # manga-ocr installed => PIL present
        img = Image.open(bmp_path).convert("RGB")
        out = []
        budget = [self._MAX_CALLS]
        self.line_trace = []
        for l in lines:
            win = "".join(l["text"].split())
            r1 = self._read_line(Image, img, l, 0, budget)
            if r1 is not None and r1 == win:
                # Two INDEPENDENT engines produced the same string — stronger
                # evidence than a second manga read (which shares the model's
                # biases), so the extra read and the repair pass prove
                # nothing. This is the common case on clean fonts: one model
                # call per new line instead of 3-4.
                out.append(r1)
                self.line_trace.append({"win": win, "r1": r1, "pick": r1})
                continue
            if len(self._spans(l)) == 1:      # no seams, nothing to vote on
                pick = self._repair(Image, img, l, r1 or "", win, budget)
                pre = pick
                pick = self._rescue(Image, img, l, pick, win, budget)
                out.append(pick)
                self.line_trace.append({"win": win, "r1": r1, "pick": pick,
                                        "rescued": pick != pre})
                continue
            # Second read with seams elsewhere. The decoder occasionally
            # drops a glyph at a canvas row seam (まもなく -> もなく); the
            # two reads then disagree, and Windows OCR — which garbles
            # shapes but rarely misses that a char EXISTS — arbitrates.
            r2 = self._read_line(Image, img, l, 3 * l["h"], budget)
            if r1 is None or r2 is None or r1 == r2:
                pick = r1 or r2 or ""
            else:
                def score(r):
                    # Similarity to the Windows read first — but COARSE
                    # (1 decimal): when the reads sit within ~0.05 of each
                    # other Windows can't really tell them apart, and there
                    # dictionary coverage arbitrates instead (まもなく
                    # segments into real words, the seam-dropped もなく
                    # doesn't) — a semantic signal independent of both
                    # engines, immune to Windows garbling the same spot.
                    # Final tiebreak: whose LENGTH matches Windows — a
                    # seam-doubled glyph (空空) reads longer than Windows'
                    # char count.
                    cov = _dict_coverage(r)
                    return (round(difflib.SequenceMatcher(None, r, win).ratio(), 1),
                            -1.0 if cov is None else cov,
                            -abs(len(r) - len(win)))
                pick = max((r1, r2), key=score)
            pick = self._repair(Image, img, l, pick, win, budget)
            pre = pick
            pick = self._rescue(Image, img, l, pick, win, budget)
            out.append(pick)
            self.line_trace.append({"win": win, "r1": r1, "r2": r2, "pick": pick,
                                    "rescued": pick != pre})
        return "\n".join(out)

    def _rescue(self, Image, img, l, pick, win, budget):
        """Last resort for whole-line manga whiffs. On a dark flash frame the
        model read 「うぐっ！？」 as ．．． while Windows read it fine — a
        single-span line gets no dual read, and repair only fixes 1-2 char
        subs, so garbage sailed through (then died at the Japanese gate and
        the line vanished). When the read barely resembles the Windows text,
        retry with transformed canvases — 2x upscale, inverted (manga-ocr is
        trained on black-on-white), both — and keep the best scorer. If every
        attempt still whiffs, publish the Windows text itself: garbled beats
        vanished."""
        if not win or not _has_japanese(win):
            return pick
        def sim(r):
            return difflib.SequenceMatcher(None, r or "", win).ratio()
        best, best_s = pick, sim(pick)
        if best_s >= 0.4:
            return pick
        from PIL import ImageOps
        up = lambda c: c.resize((c.width * 2, c.height * 2), Image.LANCZOS)
        inv = lambda c: ImageOps.invert(c.convert("L")).convert("RGB")
        for t in (up, inv, lambda c: up(inv(c))):
            r = self._read_line(Image, img, l, 0, budget, transform=t)
            s = sim(r)
            if s > best_s:
                best, best_s = r, s
            if best_s >= 0.7:
                break
        if best_s < 0.35:
            # Every manga attempt whiffed. Do NOT fall back to the Windows
            # text: publishing its garble (罰いぞ品。 for セコいぞ……！) reads
            # worse than a missing line. Return "" so nothing publishes.
            return ""
        return best

    def _repair(self, Image, img, l, pick, win, budget):
        """Fix single-char substitutions the dual read can't catch (both
        reads misread the same glyph the same way: 空 -> 望). Where pick and
        the Windows text disagree on exactly one char, a tight ~3-glyph crop
        around that spot gets its own context-free read as the third opinion;
        the Windows char wins only when that local read backs it. Windows
        alone must never override — it garbles shapes too (ブ -> プ)."""
        if not pick or not win:
            return pick
        subs = [(i1, j1) for tag, i1, i2, j1, j2
                in difflib.SequenceMatcher(None, pick, win).get_opcodes()
                if tag == "replace" and i2 - i1 == 1 and j2 - j1 == 1
                and _JP_RE.match(pick[i1]) and _JP_RE.match(win[j1])]
        if not subs or len(subs) > 2:    # many diffs = Windows garble, not us
            return pick
        chars = list(pick)
        for i1, j1 in subs:
            if budget[0] <= 0:
                break
            xc = l["x"] + l["w"] * (j1 + 0.5) / max(len(win), 1)
            vpad = max(6, l["h"] // 6)
            crop = img.crop((max(0, int(xc - 1.7 * l["h"])), max(0, l["y"] - vpad),
                             min(img.width, int(xc + 1.7 * l["h"])),
                             min(img.height, l["y"] + l["h"] + vpad)))
            key = hashlib.md5(crop.tobytes()).digest()
            local = self._cache.get(key)
            if local is None:
                budget[0] -= 1
                local = self._m.recognize(crop)
                self._cache[key] = local
                if len(self._cache) > 256:
                    self._cache.popitem(last=False)
            if win[j1] in local and chars[i1] not in local:
                chars[i1] = win[j1]
        return "".join(chars)

    def _refine_cuts(self, img, l, spans):
        """Nudge every interior span boundary to the least-inky pixel column
        nearby. A cut through a glyph puts half of it in BOTH canvas rows and
        the decoder reads the char twice (空 -> 空空) — word-box gap midpoints
        usually fall between glyphs, but misaligned boxes on decorated fonts
        don't. Ink = per-column min-max range inside the line band; background
        (even a gradient) is smooth top-to-bottom, glyph columns are not."""
        if len(spans) < 2:
            return spans
        band = img.crop((0, max(0, l["y"] - 2),
                         img.width, min(img.height, l["y"] + l["h"] + 2))).convert("L")
        px = band.load()
        rows = range(band.height)

        def ink(x):
            col = [px[x, yy] for yy in rows]
            return max(col) - min(col)

        reach = max(6, l["h"] // 2)
        xs = [spans[0][0]]
        for (a0, a1), (b0, b1) in zip(spans, spans[1:]):
            lo = max(xs[-1] + 8, a1 - reach)
            hi = min(b1 - 8, a1 + reach)
            xs.append(min(range(lo, hi), key=ink) if lo < hi else a1)
        xs.append(spans[-1][1])
        return list(zip(xs, xs[1:]))

    def _read_line(self, Image, img, l, shrink_first, budget, transform=None):
        """One manga-ocr read of one text line (chunks stacked into canvases).
        Returns None if the per-frame model-call budget ran out."""
        spans = self._refine_cuts(img, l, self._spans(l, shrink_first))
        # Windows routinely misses the trailing 。(and sometimes the first
        # glyph's left edge) from its word boxes — a span edge hole the
        # contiguous tiling can't cover. Extend outward into background;
        # empty background adds nothing to the read.
        spans[0] = (max(0, spans[0][0] - l["h"] // 3), spans[0][1])
        spans[-1] = (spans[-1][0], min(img.width, spans[-1][1] + l["h"]))
        crops = []
        for x0, x1 in spans:
            # Vertical pad helps full glyphs; horizontal pad must stay
            # tiny or the crop grabs half the neighbour span's glyph,
            # which manga-ocr reads as a stray 「 or ｉ.
            vpad = max(6, l["h"] // 6)
            crops.append(img.crop((max(0, x0 - 2), max(0, l["y"] - vpad),
                                   min(img.width, x1 + 2),
                                   min(img.height, l["y"] + l["h"] + vpad))))
        parts = []
        for i in range(0, len(crops), self._MAX_ROWS):
            canvas = self._stack(Image, crops[i:i + self._MAX_ROWS])
            if transform is not None:
                canvas = transform(canvas)
            key = hashlib.md5(canvas.tobytes()).digest()
            text = self._cache.get(key)
            if text is None:
                if budget[0] <= 0:
                    return None
                budget[0] -= 1
                text = self._m.recognize(canvas)
                self._cache[key] = text
                if len(self._cache) > 256:
                    self._cache.popitem(last=False)
            else:
                self._cache.move_to_end(key)
            parts.append(text)
        return "".join(parts)

    @staticmethod
    def _stack(Image, crops):
        """Stack chunk crops of one text line into a single top-to-bottom
        canvas, backgrounded with the first crop's corner pixel."""
        gap = 8
        w = max(c.width for c in crops) + 16
        h = sum(c.height for c in crops) + gap * (len(crops) + 1)
        canvas = Image.new("RGB", (w, h), crops[0].getpixel((2, 2)))
        y = gap
        for c in crops:
            canvas.paste(c, (8, y))
            y += c.height + gap
        return canvas

    def close(self):
        self._gate.close()
        self._m.close()


def make_engine():
    try:
        import manga_ocr  # noqa: F401
    except ImportError:
        return WindowsOcr()
    try:
        return HybridOcr()
    except RuntimeError:
        # No Japanese language pack = no gate. Ungated manga-ocr hallucinates
        # on no-text frames, so plain manga-ocr is worse than it looks — but
        # it's the only engine left. The jitter guards catch some of it.
        return MangaOcr()


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


# --------------------------------------------------------------------------- #
# Dictionary-coverage scorer: dict.sqlite as a third, semantic arbiter.
# A seam-dropped read (まもなく → もなく) stops segmenting into real words,
# while the similarity-to-Windows arbiter goes blind exactly when Windows
# itself garbled. Independent of both OCR engines.
# --------------------------------------------------------------------------- #
_COV_DB_PATH = os.path.join(BASE_DIR, "dict.sqlite")
_cov_db = None          # lazy; stays None-able — OCR must work without the dict


def _cov_conn():
    global _cov_db
    if _cov_db is None and os.path.isfile(_COV_DB_PATH):
        try:
            # Only the OCR monitor thread queries it, but be permissive anyway.
            _cov_db = sqlite3.connect(_COV_DB_PATH, check_same_thread=False)
        except sqlite3.Error:
            pass
    return _cov_db


@functools.lru_cache(maxsize=4096)
def _seg_hit(seg):
    """`seg` is a dictionary word — directly, or via de-inflection (食べた)."""
    db = _cov_conn()
    if db is None:
        return False
    q = "SELECT 1 FROM terms WHERE term=? LIMIT 1"
    if db.execute(q, (seg,)).fetchone():
        return True
    return any(form != seg and db.execute(q, (form,)).fetchone()
               for form in deinflect.deinflect(seg))


def _dict_coverage(text):
    """Fraction of the Japanese chars in `text` that greedy longest-match
    segments into dictionary words of 2+ chars (single kana are particles and
    inflection tails — they match everything and prove nothing). None when
    dict.sqlite is absent or the text has no Japanese."""
    if _cov_conn() is None:
        return None
    covered = total = i = 0
    n = len(text)
    while i < n:
        if not _JP_RE.match(text[i]):
            i += 1
            continue
        for ln in range(min(8, n - i), 1, -1):
            seg = text[i:i + ln]
            if all(_JP_RE.match(ch) for ch in seg) and _seg_hit(seg):
                covered += ln
                total += ln
                i += ln
                break
        else:
            total += 1
            i += 1
    return covered / total if total else None


# Blinking click-to-continue cursors OCR as stray marks at the line's edges —
# strip them. Sentence enders (。！？…) are NOT in this set.
_EDGE_JUNK = "・･•‥▼▽►◄▶◀◆◇■□●○◎◉⊙⊚★☆♦♢»«‹›"


def _clean(text):
    """Windows OCR spaces out Japanese 'words'; VN lines never need ASCII spaces.
    Joined OCR lines become one reader line. Also repairs the classic Japanese
    OCR confusions: a lone dash before a kanji is a misread 一 (-番 -> 一番)."""
    text = text.replace("\n", "").replace(" ", "").replace("　", "")
    text = re.sub(r"[-−－](?=[一-鿿])", "一", text)
    # manga-ocr renders VN ellipses (……) as runs of dots — map back.
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
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    if ratio >= thr:
        return True
    # Near-miss where one read is dictionary-clean and the other isn't: the
    # poor one is a garbled re-read, not a new line — a couple of misread
    # glyphs drag the ratio just under the threshold while also breaking the
    # read's segmentation into real words. The evidence is the coverage GAP,
    # not absolute levels (a garble can still hit stray words: 行つた covers
    # via つた "vine"); two comparably-covered reads this far apart stay
    # separate lines (だから/たから both segment fully, gap 0).
    # ponytail: 0.12 band + 0.7/0.3 gap are eyeballed, tune via /ocr trace.
    if ratio >= thr - 0.12:
        ca, cb = _dict_coverage(a), _dict_coverage(b)
        if ca is not None and cb is not None and \
                max(ca, cb) >= 0.7 and max(ca, cb) - min(ca, cb) >= 0.3:
            return True
    return False


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
    # Every OCR decision appends to logs/ocr-debug-YYYY-MM-DD.jsonl, and
    # frames the pipeline found suspicious (a whiff, a seam disagreement, a
    # rescue) are copied to logs/ocr-frames/ (capped). This is the raw
    # material the tuned-by-eyeball thresholds and the fixture backlog need:
    # after a real session, the jsonl says what was decided and why, and a
    # saved frame + its trace line is a ready-made regression fixture.
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

    _MAX_FRAMES = 40

    def _save_frame(self, why):
        """Keep the exact BMP the pipeline just read (post-upscale, the real
        model input) so a misread is reproducible offline. Oldest pruned."""
        try:
            d = os.path.join(BASE_DIR, "logs", "ocr-frames")
            os.makedirs(d, exist_ok=True)
            name = time.strftime("%Y%m%d-%H%M%S") + "-" + why + ".bmp"
            shutil.copyfile(_TMP_BMP, os.path.join(d, name))
            frames = sorted(os.listdir(d))
            while len(frames) > self._MAX_FRAMES:
                os.remove(os.path.join(d, frames.pop(0)))
            return name
        except OSError:
            return None

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
            engine = make_engine()          # slow: model load / worker handshake
            self.engine_name = engine.name
            self.starting = False
            self.running = True
            last_hash = None
            pending_text = None   # None-peek engines: text awaiting confirmation
            seen = collections.deque(maxlen=2)     # unconfirmed peek signatures
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
                # Unconfirmed text must get its confirming peek even if the
                # pixels froze right after it appeared (scene back from a white
                # fade, nothing blinking in-region) — skipping on hash alone
                # left that text pending forever.
                if h == last_hash and not seen and pending_text is None:
                    continue
                last_hash = h
                # Pixels changed — but is it TEXT? A blinking click-cursor or
                # animated background churns the pixel hash forever (this used
                # to stall a 2s settle loop and re-OCR every blink). The cheap
                # peek (Windows OCR, ~0.1s) answers without touching the model.
                # New text must peek the same twice among the last few polls
                # before the expensive read: mid-transition frames peek
                # differently EVERY poll and never qualify, while a blinking
                # cursor that nudges Windows into alternating reads (だから/
                # たから every other frame — exact-consecutive matching starved
                # that line forever) converges in three polls.
                sig = engine.peek(_TMP_BMP)
                if sig is not None:
                    self.trace["peek"] = sig
                    if not _has_japanese(sig):
                        seen.clear()
                        continue
                    if sig in handled:      # blink re-showing processed text
                        continue
                    if sig not in seen:
                        seen.append(sig)
                        continue
                    seen.clear()
                    text = _clean(engine.recognize(_TMP_BMP))
                    handled.append(sig)
                    self.trace["read"] = text
                    lt = getattr(engine, "line_trace", None)
                    self.trace["lines"] = lt
                    # Suspicious frames become offline fixtures: a whole-line
                    # whiff, a seam disagreement the arbiter had to settle,
                    # or a rescue. Routine clean reads log text only.
                    why = None
                    for e in lt or []:
                        if e.get("pick") == "":
                            why = "whiff"; break
                        if e.get("rescued"):
                            why = "rescue"; break
                        if "r2" in e and e.get("r1") != e.get("r2"):
                            why = "seam"
                    frame = self._save_frame(why) if why else None
                    self._debug("read", text=text, lines=lt,
                                **({"frame": frame} if frame else {}))
                else:
                    # Engine without a cheap peek (bare manga-ocr, no Japanese
                    # language pack): confirm on the expensive read itself.
                    text = _clean(engine.recognize(_TMP_BMP))
                    self.trace["read"] = text
                    if not text or text != pending_text:
                        pending_text = text
                        continue
                    pending_text = None
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
