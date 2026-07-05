"""
OCR text source — the fallback for games Textractor can't hook.

The user drags a box over the game's text area once (tkinter overlay, run as a
subprocess so it can't fight pywebview's main thread). A monitor thread then
screenshots that region (ctypes GDI, no dependencies), skips unchanged frames by
pixel hash, OCRs changed ones, and publishes a line only after two consecutive
identical reads — so a VN's typewriter animation doesn't spam partial lines.

Engines, best first:
  - manga-ocr  (pip install manga-ocr) — transformer model built for Japanese
    game/manga text; by far the best quality. Optional, ~400 MB with torch.
  - Windows built-in OCR (Windows.Media.Ocr via a persistent PowerShell worker)
    — decent, zero install; needs the Japanese language pack.
"""

import base64
import ctypes
import hashlib
import json
import os
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
    cheap changed-frame hash). Windows only."""
    hdc = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    old = gdi32.SelectObject(mem, bmp)
    try:
        gdi32.BitBlt(mem, 0, 0, w, h, hdc, x, y, _SRCCOPY)
        bih = _BITMAPINFOHEADER(biSize=40, biWidth=w, biHeight=-h,  # negative = top-down
                                biPlanes=1, biBitCount=32, biCompression=0)
        buf = (ctypes.c_char * (w * h * 4))()
        gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bih), 0)
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
    $text = ($res.Lines | ForEach-Object { $_.Text }) -join "`n"
    $stream.Dispose()
    [Console]::Out.WriteLine([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($text)))
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

    def recognize(self, bmp_path):
        if self._proc.poll() is not None:
            raise RuntimeError("Windows OCR worker died")
        self._proc.stdin.write(bmp_path.encode("utf-8") + b"\r\n")
        self._proc.stdin.flush()
        out = self._proc.stdout.readline().strip()
        return base64.b64decode(out).decode("utf-8", "replace") if out else ""

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

    def recognize(self, bmp_path):
        return self._m(bmp_path)

    def close(self):
        pass


def make_engine():
    try:
        import manga_ocr  # noqa: F401 — cheap existence check before the heavy load
        return MangaOcr()
    except ImportError:
        return WindowsOcr()


# --------------------------------------------------------------------------- #
# The OCR text source
# --------------------------------------------------------------------------- #
def _has_japanese(s):
    return any("぀" <= ch <= "ヿ" or "一" <= ch <= "鿿" or ch == "々" for ch in s)


def _clean(text):
    """Windows OCR spaces out Japanese 'words'; VN lines never need ASCII spaces.
    Joined OCR lines become one reader line."""
    return text.replace("\n", "").replace(" ", "").replace("　", "").strip()


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
            pending = None                  # stability gate: publish on 2nd identical read
            while not self._stop.is_set():
                if not self._paused.is_set():
                    r = self.region
                    try:
                        pixels = capture_bmp(r["x"], r["y"], r["w"], r["h"], _TMP_BMP)
                    except Exception:
                        pixels = None
                    if pixels:
                        h = hashlib.md5(pixels).digest()
                        if h != last_hash:              # frame changed -> OCR it
                            last_hash = h
                            text = _clean(engine.recognize(_TMP_BMP))
                            if text and _has_japanese(text):
                                # Two identical consecutive reads = the typewriter
                                # animation finished; partial lines never publish.
                                # (publish_line dedupes, so confirming twice is safe.)
                                if text == pending:
                                    self._publish(text)
                                else:
                                    last_hash = None   # unconfirmed — re-read next tick
                                                       # even though the pixels froze
                                pending = text
                            else:
                                pending = None
                time.sleep(0.6)
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
