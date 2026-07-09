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
def capture_bmp(x, y, w, h, path):
    """Screenshot the region into a .bmp; returns the raw pixel bytes (for the
    cheap changed-frame hash). Windows only.

    The capture is upscaled 2x (smooth HALFTONE interpolation): VN fonts are
    small, and Windows OCR loses dakuten dots at that size (だ read as た).
    Twice the glyph size recovers them. Very large regions stay 1:1."""
    scale = 2 if w * h <= 1_200_000 else 1
    sw, sh = w * scale, h * scale
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
        return lines

    def recognize(self, bmp_path):
        return "\n".join(l["text"] for l in self.recognize_lines(bmp_path))

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
    _MAX_LINES = 8    # model calls per frame (~0.5s each on CPU)
    _MAX_ROWS = 6     # rows per canvas; more starves the 224x224 resolution

    def __init__(self):
        self._gate = WindowsOcr()
        self._m = MangaOcr()
        # crop-pixels -> text. A VN screen mostly repeats between frames (a
        # new line appears, old ones don't move), so identical crops skip the
        # model entirely — same pixels, same text, no accuracy risk.
        self._cache = collections.OrderedDict()

    def _spans(self, line):
        """Split one OCR line into x-spans at word-box boundaries, each span
        at most ~6:1 aspect — past ~7:1 the 224x224 resize eats glyphs, and
        narrower chunks mean more manga-ocr calls (~0.35s each on CPU)."""
        max_w = max(6 * line["h"], 200)
        spans = []
        for w in sorted(line.get("ws") or [], key=lambda w: w["x"]):
            if spans and w["x"] + w["w"] - spans[-1][0] <= max_w:
                spans[-1][1] = max(spans[-1][1], w["x"] + w["w"])
            else:
                spans.append([w["x"], w["x"] + w["w"]])
        return spans or [[line["x"], line["x"] + line["w"]]]

    def recognize(self, bmp_path):
        lines = [l for l in self._gate.recognize_lines(bmp_path)
                 if _has_japanese(l["text"])]
        if not lines:
            return ""
        # Furigana ruby OCRs as its own tiny lines above the real ones — drop
        # lines under 55% of the tallest so readings don't duplicate into the
        # transcript. (Name labels are ~70% of dialogue size, they survive.)
        tallest = max(l["h"] for l in lines)
        lines = [l for l in lines if l["h"] >= 0.55 * tallest]
        from PIL import Image   # manga-ocr installed => PIL present
        img = Image.open(bmp_path).convert("RGB")
        out, model_calls = [], 0
        for l in lines:
            crops = []
            for x0, x1 in self._spans(l):
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
                key = hashlib.md5(canvas.tobytes()).digest()
                text = self._cache.get(key)
                if text is None:
                    if model_calls >= self._MAX_LINES:
                        break
                    model_calls += 1
                    text = self._m.recognize(canvas)
                    self._cache[key] = text
                    if len(self._cache) > 256:
                        self._cache.popitem(last=False)
                else:
                    self._cache.move_to_end(key)
                parts.append(text)
            out.append("".join(parts))
        return "\n".join(out)

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
def _has_japanese(s):
    return any("぀" <= ch <= "ヿ" or "一" <= ch <= "鿿" or ch == "々" for ch in s)


# Blinking click-to-continue cursors OCR as stray marks at the line's edges —
# strip them. Sentence enders (。！？…) are NOT in this set.
_EDGE_JUNK = "・･•‥▼▽►◄▶◀◆◇■□●○◎◉⊙⊚★☆♦♢»«‹›"


def _clean(text):
    """Windows OCR spaces out Japanese 'words'; VN lines never need ASCII spaces.
    Joined OCR lines become one reader line. Also repairs the classic Japanese
    OCR confusions: a lone dash before a kanji is a misread 一 (-番 -> 一番)."""
    text = text.replace("\n", "").replace(" ", "").replace("　", "")
    text = re.sub(r"[-−－](?=[一-鿿])", "一", text)
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
        self._stop = threading.Event()
        self._thread = None

    def state(self):
        return {"running": self.running, "starting": self.starting,
                "region": self.region, "engine": self.engine_name, "error": self.error}

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
                if h == last_hash:
                    continue
                # Frame changed. Wait for the PIXELS to settle before spending an
                # OCR — the typewriter animation is over when two samples 0.25s
                # apart match. One OCR per line (not two) keeps latency ~0.6-0.9s
                # and removes the double-read jitter that published near-duplicate
                # lines. A permanent blinker (click-to-continue arrow) never
                # settles, so give up after 4 samples and OCR anyway — the text
                # dedupe below eats the repeats.
                for _ in range(4):
                    if self._stop.is_set():
                        return
                    time.sleep(0.25)
                    try:
                        px = capture_bmp(r["x"], r["y"], r["w"], r["h"], _TMP_BMP)
                    except Exception:
                        break
                    h2 = hashlib.md5(px).digest()
                    if h2 == h:
                        break
                    h = h2
                last_hash = h
                text = _clean(engine.recognize(_TMP_BMP))
                if not text or not _has_japanese(text):
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
                # misread, a blinking ◎ cursor) collapse via _same_line. Republish
                # ONLY when this read extends the fullest version already shown —
                # the maru/tail finally recognized — so the reader upgrades in
                # place. Any equal-or-shorter re-read of that line is jitter.
                same = [raw for k, raw in recent if _same_line(key, k)]
                if same:
                    longest = max(same, key=len)
                    if not (text.startswith(longest) and len(text) > len(longest)):
                        continue
                self._publish(text)
                recent.append((key, text))
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
