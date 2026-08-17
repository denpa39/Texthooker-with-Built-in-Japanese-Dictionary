"""
Screen OCR for games Textractor cannot hook, with two presentation modes.

Reader mode publishes stable text from the selected area into the app window.
Popup mode retains MeikiOCR's character boxes and shows the existing offline
dictionary beside the mouse while Caps Lock is on. The region picker and the
native popup run in subprocesses so tkinter never fights pywebview's main thread.

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
import queue
import re
import struct
import subprocess
import sys
import threading
import time
import zlib

BASE_DIR = (os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__)))
REGION_PATH = os.path.join(BASE_DIR, "ocr_region.json")

if sys.platform == "win32":
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    _SRCCOPY = 0x00CC0020
    _VK_CAPITAL = 0x14

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
def capture_bmp(x, y, w, h, path=None, scale=None):
    """Capture a region as raw BGRA; optionally also write it as a BMP.

    The returned bytes feed the cheap changed-frame hash and MeikiOCR directly.
    Windows only.

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
    if path:
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
        px = capture_bmp(x, y, w, h, scale=scale)
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
    root.geometry(f"{vw}x{vh}{vx:+d}{vy:+d}")
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
# Native popup — a tiny stdin-driven tkinter process, kept out of screenshots.
# --------------------------------------------------------------------------- #
_DEFAULT_POPUP_THEME = {
    "bg": "#eef4fb", "text": "#213243", "accent": "#3f6fc0",
    "accent2": "#a9762a", "pos": "#2f7d4a", "danger": "#c0392b",
}


def _decode_popup_command(raw):
    """Decode one popup IPC message explicitly as UTF-8.

    The native child inherits CP932 as ``sys.stdin.encoding`` on Japanese
    Windows. Reading the parent's UTF-8 pipe through that text wrapper turns
    every Japanese headword into mojibake while ASCII definitions look fine.
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


def _popup_wheel_units(delta):
    """Translate a Windows wheel delta into a short tkinter scroll."""
    if not isinstance(delta, (int, float)) or not delta:
        return 0
    notches = max(1, int(abs(delta) // 120))
    return (-3 if delta > 0 else 3) * notches


def _mix_popup_colour(foreground, background, amount):
    """Small color-mix equivalent for the native version of the web popup."""
    try:
        fg = [int(foreground[index:index + 2], 16) for index in (1, 3, 5)]
        bg = [int(background[index:index + 2], 16) for index in (1, 3, 5)]
        mixed = [round(f * amount + b * (1 - amount)) for f, b in zip(fg, bg)]
        return "#" + "".join(f"{channel:02x}" for channel in mixed)
    except (TypeError, ValueError):
        return background


def _popup_palette(theme):
    """Derive the same quiet surface/line/muted colours used by style.css."""
    return {
        **theme,
        "surface": _mix_popup_colour(theme["text"], theme["bg"], 0.06),
        "panel": _mix_popup_colour(theme["text"], theme["bg"], 0.10),
        "line": _mix_popup_colour(theme["text"], theme["bg"], 0.16),
        "muted": _mix_popup_colour(theme["text"], theme["bg"], 0.70),
        "accent_soft": _mix_popup_colour(theme["accent"], theme["bg"], 0.12),
        "pos_soft": _mix_popup_colour(theme["pos"], theme["bg"], 0.12),
    }


def popup_window_main():
    """Render line-delimited JSON commands from stdin in a no-focus window.

    Keeping this in a subprocess isolates tkinter from pywebview. On supported
    Windows versions WDA_EXCLUDEFROMCAPTURE also prevents the definition popup
    from feeding back into the next OCR frame.
    """
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    initial_palette = _popup_palette(_DEFAULT_POPUP_THEME)
    root.configure(bg=initial_palette["line"])

    shell = tk.Frame(root, bg=initial_palette["surface"],
                     highlightbackground=initial_palette["line"],
                     highlightthickness=1)
    shell.pack(fill="both", expand=True)
    canvas = tk.Canvas(shell, bg=initial_palette["surface"], bd=0,
                       highlightthickness=0)
    scrollbar = tk.Canvas(shell, width=10, bg=initial_palette["surface"],
                          bd=0, highlightthickness=0)
    palette_state = {"value": initial_palette}

    def draw_scrollbar(first, last):
        scrollbar.delete("all")
        first, last = float(first), float(last)
        if last >= 0.999:
            return
        height = max(1, scrollbar.winfo_height())
        top = 8 + first * max(1, height - 16)
        bottom = 8 + last * max(1, height - 16)
        scrollbar.create_line(5, top, 5, max(top + 14, bottom),
                              fill=palette_state["value"]["line"], width=4,
                              capstyle=tk.ROUND)

    canvas.configure(yscrollcommand=draw_scrollbar)
    canvas.pack(side="left", fill="both", expand=True)
    content = tk.Frame(canvas, bg=initial_palette["surface"], padx=15, pady=13)
    content_window = canvas.create_window((0, 0), window=content, anchor="nw")

    def sync_scroll_region(_event=None):
        bounds = canvas.bbox("all")
        if bounds:
            canvas.configure(scrollregion=bounds)

    def fit_content_width(event):
        canvas.itemconfigure(content_window, width=event.width)

    content.bind("<Configure>", sync_scroll_region)
    canvas.bind("<Configure>", fit_content_width)
    commands = queue.Queue()

    def read_commands():
        try:
            # Bypass TextIOWrapper's locale encoding (usually CP932) and own
            # the byte-to-text boundary explicitly.
            input_stream = (os.fdopen(0, "rb", closefd=False) if sys.stdin is None
                            else getattr(sys.stdin, "buffer", sys.stdin))
            for raw in input_stream:
                try:
                    commands.put(_decode_popup_command(raw))
                except (TypeError, ValueError, UnicodeDecodeError):
                    pass
        except OSError:
            pass
        finally:
            commands.put({"quit": True})

    threading.Thread(target=read_commands, daemon=True).start()
    content_key = None
    popup_state = {"visible": False, "scrollable": False}
    layout_state = {"key": None, "width": 260, "height": 50}
    mouse_hook = {"handle": None, "callback": None}
    pending_scroll = collections.deque()

    def apply_no_activate_flags(width, height):
        if sys.platform != "win32":
            return
        try:
            hwnd = root.winfo_id()
            get_style = ctypes.windll.user32.GetWindowLongW
            set_style = ctypes.windll.user32.SetWindowLongW
            get_style.argtypes = [wintypes.HWND, ctypes.c_int]
            get_style.restype = ctypes.c_long
            set_style.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
            set_style.restype = ctypes.c_long
            style = get_style(hwnd, -20)  # GWL_EXSTYLE
            # Tool window + click-through + never steal focus from the game.
            set_style(hwnd, -20, style | 0x80 | 0x20 | 0x08000000)
            # Windows 10 2004+: omit this popup from screen capture.
            affinity = ctypes.windll.user32.SetWindowDisplayAffinity
            affinity.argtypes = [wintypes.HWND, wintypes.DWORD]
            affinity.restype = wintypes.BOOL
            affinity(hwnd, 0x11)
            # Clip the borderless window to the same rounded-card silhouette as
            # the web popup. SetWindowRgn works on Windows 10 as well as 11.
            create_region = ctypes.windll.gdi32.CreateRoundRectRgn
            create_region.argtypes = [ctypes.c_int] * 6
            create_region.restype = wintypes.HRGN
            set_region = ctypes.windll.user32.SetWindowRgn
            set_region.argtypes = [wintypes.HWND, wintypes.HRGN, wintypes.BOOL]
            set_region.restype = ctypes.c_int
            region = create_region(0, 0, width + 1, height + 1, 26, 26)
            if region and not set_region(hwnd, region, True):
                ctypes.windll.gdi32.DeleteObject(region)
        except Exception:
            pass

    def rebuild(command):
        nonlocal content_key
        entries = command.get("entries") or []
        theme = {**_DEFAULT_POPUP_THEME, **(command.get("theme") or {})}
        palette = _popup_palette(theme)
        key = json.dumps([entries, theme], ensure_ascii=False, sort_keys=True)
        if key == content_key:
            return
        content_key = key
        palette_state["value"] = palette
        root.configure(bg=palette["line"])
        shell.configure(bg=palette["surface"], highlightbackground=palette["line"])
        canvas.configure(bg=palette["surface"])
        content.configure(bg=palette["surface"])
        scrollbar.configure(bg=palette["surface"])
        for child in content.winfo_children():
            child.destroy()
        for index, entry in enumerate(entries):
            if index:
                tk.Frame(content, height=1, bg=palette["line"]).pack(
                    fill="x", pady=(13, 12))
            inflection = entry.get("inflection") or ""
            if inflection:
                tk.Label(content, text=inflection, bg=palette["surface"],
                         fg=theme["accent"], font=("Segoe UI", 9, "italic"),
                         anchor="w", justify="left", wraplength=430).pack(
                             fill="x", anchor="w", pady=(0, 2))
            header = tk.Frame(content, bg=palette["surface"])
            header.pack(fill="x", anchor="w")
            tk.Label(header, text=entry.get("word", ""), bg=palette["surface"],
                     fg=theme["text"], font=("Yu Mincho", 21, "bold"),
                     anchor="w").pack(side="left", anchor="s")
            reading = entry.get("reading") or ""
            if reading and reading != entry.get("word"):
                tk.Label(header, text=reading, bg=palette["surface"],
                         fg=theme["accent2"], font=("Yu Gothic UI", 11),
                         anchor="w").pack(side="left", padx=(8, 0), anchor="s",
                                          pady=(0, 3))
            frequency = entry.get("frequency")
            if isinstance(frequency, int):
                hot = frequency <= 6600
                tk.Label(header, text=f"№{frequency:,}",
                         bg=palette["pos_soft"] if hot else palette["panel"],
                         fg=theme["pos"] if hot else palette["muted"],
                         font=("Segoe UI", 8), padx=6, pady=1).pack(
                             side="left", padx=(8, 0), anchor="s", pady=(0, 4))
            if entry.get("kind") == "name":
                tk.Label(header, text="name", bg=palette["accent_soft"],
                         fg=theme["accent"], font=("Segoe UI", 8),
                         padx=7, pady=1).pack(side="right", anchor="s", pady=(0, 4))
            alternatives = entry.get("alternatives") or []
            if alternatives:
                tk.Label(content, text="also: " + "、".join(alternatives),
                         bg=palette["surface"], fg=palette["muted"],
                         font=("Yu Gothic UI", 9), anchor="w", justify="left",
                         wraplength=430).pack(
                             fill="x", anchor="w", pady=(1, 0))
            senses = entry.get("senses") or [
                {"definition": definition, "number": number}
                for number, definition in enumerate(entry.get("definitions") or [], 1)
            ]
            for sense in senses:
                sense_frame = tk.Frame(content, bg=palette["surface"])
                sense_frame.pack(fill="x", anchor="w", pady=(7, 0))
                pos = sense.get("pos") or ""
                if pos:
                    tk.Label(sense_frame, text=pos, bg=palette["surface"],
                             fg=theme["pos"], font=("Segoe UI", 9, "italic"),
                             anchor="w", justify="left", wraplength=430).pack(
                                 fill="x", anchor="w", pady=(0, 1))
                gloss_row = tk.Frame(sense_frame, bg=palette["surface"])
                gloss_row.pack(fill="x", anchor="w")
                tk.Label(gloss_row, text=str(sense.get("number", "")) + ".",
                         bg=palette["surface"], fg=palette["muted"],
                         font=("Segoe UI", 10), anchor="nw").pack(side="left")
                definition = sense.get("definition") or ""
                misc = sense.get("misc") or ""
                if misc:
                    definition = f"({misc}) {definition}"
                tk.Label(gloss_row, text=definition, bg=palette["surface"],
                         fg=theme["text"], font=("Segoe UI", 10),
                         justify="left", anchor="nw", wraplength=415).pack(
                             side="left", fill="x", expand=True, padx=(6, 0))
        root.update_idletasks()
        sync_scroll_region()
        canvas.yview_moveto(0)

    def place(command):
        layout_changed = layout_state["key"] != content_key
        was_visible = popup_state["visible"]
        if layout_changed:
            root.update_idletasks()
            content_width = content.winfo_reqwidth()
            content_height = content.winfo_reqheight()
            popup_state["scrollable"] = content_height + 2 > 520
            if popup_state["scrollable"]:
                scrollbar.pack(side="right", fill="y")
                root.update_idletasks()
                scroll_width = scrollbar.winfo_reqwidth()
            else:
                scrollbar.pack_forget()
                scroll_width = 0
            layout_state["key"] = content_key
            layout_state["width"] = max(
                260, min(480, content_width + scroll_width + 2))
            layout_state["height"] = max(50, min(520, content_height + 2))
        width, height = layout_state["width"], layout_state["height"]
        try:
            gsm = ctypes.windll.user32.GetSystemMetrics
            vx, vy, vw, vh = gsm(76), gsm(77), gsm(78), gsm(79)
        except Exception:
            vx = vy = 0
            vw, vh = root.winfo_screenwidth(), root.winfo_screenheight()
        mouse_x, mouse_y = int(command.get("x", 0)), int(command.get("y", 0))
        x, y = mouse_x + 18, mouse_y + 24
        if x + width > vx + vw:
            x = mouse_x - width - 18
        if y + height > vy + vh:
            y = mouse_y - height - 18
        # Prefer a position outside the OCR area even on Windows versions that
        # do not support WDA_EXCLUDEFROMCAPTURE.
        region = command.get("region") or {}
        if all(isinstance(region.get(k), int) for k in ("x", "y", "w", "h")):
            rx, ry, rw, rh = (region[k] for k in ("x", "y", "w", "h"))
            overlaps = not (x + width <= rx or x >= rx + rw or
                            y + height <= ry or y >= ry + rh)
            if overlaps:
                alternatives = [
                    (mouse_x + 18, ry - height - 12),
                    (mouse_x + 18, ry + rh + 12),
                    (rx - width - 12, mouse_y + 24),
                    (rx + rw + 12, mouse_y + 24),
                ]
                fitting = [(ax, ay) for ax, ay in alternatives
                           if vx <= ax and ax + width <= vx + vw
                           and vy <= ay and ay + height <= vy + vh]
                if fitting:
                    x, y = min(fitting, key=lambda point:
                               abs(point[0] - mouse_x) + abs(point[1] - mouse_y))
        x = max(vx, min(x, vx + vw - width))
        y = max(vy, min(y, vy + vh - height))
        root.geometry(f"{width}x{height}{x:+d}{y:+d}")
        if layout_changed:
            apply_no_activate_flags(width, height)
        if not was_visible:
            root.deiconify()
            root.lift()
        if layout_changed or not was_visible:
            root.update_idletasks()
            draw_scrollbar(*canvas.yview())
        popup_state["visible"] = True

    def scroll_popup(event):
        units = _popup_wheel_units(getattr(event, "delta", 0))
        if popup_state["scrollable"] and units:
            canvas.yview_scroll(units, "units")
            return "break"
        return None

    root.bind_all("<MouseWheel>", scroll_popup)
    root.bind_all("<Button-4>", lambda _event: (
        canvas.yview_scroll(-3, "units") if popup_state["scrollable"] else None))
    root.bind_all("<Button-5>", lambda _event: (
        canvas.yview_scroll(3, "units") if popup_state["scrollable"] else None))

    def install_mouse_hook():
        """Catch the wheel while the Windows popup remains click-through.

        The cursor stays over the game glyph so the lookup remains open. A
        normal tkinter wheel binding cannot see that input, hence the small
        low-level hook. It swallows wheel events only when overflow exists.
        """
        if sys.platform != "win32":
            return
        try:
            class Point(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

            class MouseHookData(ctypes.Structure):
                _fields_ = [("pt", Point), ("mouseData", wintypes.DWORD),
                            ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                            ("extraInfo", ctypes.c_size_t)]

            mouse_proc = ctypes.WINFUNCTYPE(
                ctypes.c_ssize_t, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t)
            set_hook = ctypes.windll.user32.SetWindowsHookExW
            set_hook.argtypes = [ctypes.c_int, mouse_proc, ctypes.c_void_p, wintypes.DWORD]
            set_hook.restype = ctypes.c_void_p
            call_next = ctypes.windll.user32.CallNextHookEx
            call_next.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                  ctypes.c_size_t, ctypes.c_ssize_t]
            call_next.restype = ctypes.c_ssize_t

            @mouse_proc
            def callback(code, message, data_ptr):
                if (code >= 0 and message == 0x020A and popup_state["visible"]
                        and popup_state["scrollable"]):
                    data = ctypes.cast(
                        data_ptr, ctypes.POINTER(MouseHookData)).contents
                    delta = ctypes.c_short((data.mouseData >> 16) & 0xffff).value
                    units = _popup_wheel_units(delta)
                    if units:
                        # Never call tkinter from inside this re-entrant Windows
                        # callback. Tk's message loop has released the GIL here;
                        # entering Tcl directly can fatally corrupt thread state.
                        pending_scroll.append(units)
                        return 1
                return call_next(mouse_hook["handle"], code, message, data_ptr)

            get_module = ctypes.windll.kernel32.GetModuleHandleW
            get_module.argtypes = [wintypes.LPCWSTR]
            get_module.restype = ctypes.c_void_p
            module = get_module(None)
            mouse_hook["callback"] = callback
            mouse_hook["handle"] = set_hook(14, callback, module, 0)  # WH_MOUSE_LL
        except Exception:
            mouse_hook["handle"] = None
            mouse_hook["callback"] = None

    def close_popup():
        handle = mouse_hook.get("handle")
        if handle and sys.platform == "win32":
            try:
                ctypes.windll.user32.UnhookWindowsHookEx(handle)
            except Exception:
                pass
            mouse_hook["handle"] = None
        root.destroy()

    def poll():
        scroll_units = 0
        while pending_scroll:
            scroll_units += pending_scroll.popleft()
        if scroll_units and popup_state["visible"] and popup_state["scrollable"]:
            canvas.yview_scroll(scroll_units, "units")
        latest = None
        try:
            while True:
                latest = commands.get_nowait()
        except queue.Empty:
            pass
        if latest:
            if latest.get("quit"):
                close_popup()
                return
            if latest.get("show") and latest.get("entries"):
                rebuild(latest)
                place(latest)
            else:
                popup_state["visible"] = False
                root.withdraw()
        root.after(16, poll)

    install_mouse_hook()
    root.after(0, poll)
    root.mainloop()


class _PopupBridge:
    """Own the popup subprocess and send it only changed display commands."""

    def __init__(self):
        self._process = None
        self._last = None

    @staticmethod
    def _command():
        if getattr(sys, "frozen", False):
            return [sys.executable, "--ocr-popup"]
        return [sys.executable, os.path.abspath(__file__), "--ocr-popup"]

    def _spawn(self):
        flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
        self._process = subprocess.Popen(
            self._command(), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1,
            creationflags=flags)

    def send(self, command):
        raw = json.dumps(command, ensure_ascii=False, separators=(",", ":"))
        if raw == self._last:
            return True
        for _attempt in range(2):
            try:
                if self._process is None or self._process.poll() is not None:
                    self._spawn()
                self._process.stdin.write(raw + "\n")
                self._process.stdin.flush()
                self._last = raw
                return True
            except (BrokenPipeError, OSError, AttributeError):
                self._process = None
                self._last = None
        return False

    def hide(self):
        if self._process is not None:
            self.send({"show": False})

    def close(self):
        process, self._process = self._process, None
        self._last = None
        if process is None:
            return
        try:
            process.stdin.write('{"quit":true}\n')
            process.stdin.flush()
            process.stdin.close()
            process.wait(timeout=0.6)
        except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
            try:
                process.terminate()
            except OSError:
                pass


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
        self.hover_lines = []
        # The monitor asks for the same frozen frame again to confirm stable
        # text. Cache complete frame reads so confirmation costs a hash, not a
        # second ONNX pass; animated frames still have distinct keys.
        self._cache = collections.OrderedDict()

    @staticmethod
    def _format_results(results):
        lines = []
        for index, result in enumerate(results or []):
            text = str(result.get("text") or "").strip()
            raw_chars = [char for char in (result.get("chars") or [])
                         if isinstance(char, dict)
                         and isinstance(char.get("bbox"), (list, tuple))
                         and len(char["bbox"]) == 4]
            chars = [{"text": str(char.get("char") or ""),
                      "box": [int(v) for v in char["bbox"]]}
                     for char in raw_chars if str(char.get("char") or "")]
            if not text or not chars or not _has_japanese(text):
                continue
            x1 = min(char["box"][0] for char in chars)
            y1 = min(char["box"][1] for char in chars)
            x2 = max(char["box"][2] for char in chars)
            y2 = max(char["box"][3] for char in chars)
            w, h = max(1, x2 - x1), max(1, y2 - y1)
            confs = [float(char["conf"]) for char in raw_chars
                     if isinstance(char.get("conf"), (int, float))]
            lines.append({"text": text, "x": x1, "y": y1, "w": w, "h": h,
                          "vertical": bool(result.get("is_vertical", h > w)),
                          "conf": sum(confs) / len(confs) if confs else None,
                          "index": index, "chars": chars})

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
        hover = [{"text": "".join(char["text"] for char in line["chars"]),
                  "chars": line["chars"], "vertical": line["vertical"],
                  "_hit": _hover_hit_geometry(line["chars"])}
                 for line in lines]
        return "\n".join(line["text"] for line in lines), trace, hover

    def recognize(self, bmp_path=None, pixels=None, width=None, height=None):
        """Recognize a BMP or an in-memory GDI BGRA frame.

        Live monitoring already owns the raw pixels for change detection. Going
        through a temporary multi-megabyte BMP and decoding it again made large
        regions pay avoidable disk + codec work on every model pass.
        """
        if pixels is not None and isinstance(width, int) and isinstance(height, int):
            data = pixels
            try:
                bgra = self._np.frombuffer(data, dtype=self._np.uint8).reshape(
                    (height, width, 4))
                image = self._np.ascontiguousarray(bgra[:, :, :3])
            except (ValueError, TypeError):
                image = None
        else:
            try:
                with open(bmp_path, "rb") as image_file:
                    data = image_file.read()
            except (OSError, TypeError):
                self.line_trace = []
                self.hover_lines = []
                return ""
            image = self._cv2.imdecode(
                self._np.frombuffer(data, dtype=self._np.uint8), self._cv2.IMREAD_COLOR)

        key = hashlib.md5(data).digest()
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            text, self.line_trace, self.hover_lines = cached
            return text

        if image is None:
            self.line_trace = []
            self.hover_lines = []
            return ""
        results = self._ocr.run_ocr(
            image, det_threshold=self._DET_THRESHOLD,
            rec_threshold=self._REC_THRESHOLD,
            punct_conf_factor=self._PUNCT_CONF_FACTOR)
        text, self.line_trace, self.hover_lines = self._format_results(results)
        self._cache[key] = (text, self.line_trace, self.hover_lines)
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


def _caps_lock_on():
    """Caps Lock toggle state, not whether the physical key is being held."""
    if sys.platform != "win32":
        return False
    try:
        return bool(user32.GetKeyState(_VK_CAPITAL) & 1)
    except Exception:
        return False


def _cursor_pos():
    if sys.platform != "win32":
        return None
    point = wintypes.POINT()
    return (point.x, point.y) if user32.GetCursorPos(ctypes.byref(point)) else None


_POPUP_SCAN_MAX_W = 1024
_POPUP_SCAN_MAX_H = 640
_POPUP_SCAN_EDGE = 72


def _popup_scan_region(region, cursor):
    """A hover-centred OCR tile inside the selected region.

    Popup lookup only needs the text around the pointer, not every animated
    pixel in a fullscreen game. Keeping one third of the tile before the cursor
    leaves ample suffix context to its right/below for horizontal/vertical text.
    """
    width = min(region["w"], _POPUP_SCAN_MAX_W)
    height = min(region["h"], _POPUP_SCAN_MAX_H)
    local_x = cursor[0] - region["x"]
    local_y = cursor[1] - region["y"]
    left = max(0, min(local_x - width // 3, region["w"] - width))
    top = max(0, min(local_y - height // 3, region["h"] - height))
    return {"x": region["x"] + left, "y": region["y"] + top,
            "w": width, "h": height}


def _popup_scan_covers(scan, region, cursor):
    """Whether the cursor remains safely inside an existing OCR tile."""
    left_pad = 0 if scan["x"] <= region["x"] else _POPUP_SCAN_EDGE
    top_pad = 0 if scan["y"] <= region["y"] else _POPUP_SCAN_EDGE
    right_pad = (0 if scan["x"] + scan["w"] >= region["x"] + region["w"]
                 else _POPUP_SCAN_EDGE)
    bottom_pad = (0 if scan["y"] + scan["h"] >= region["y"] + region["h"]
                  else _POPUP_SCAN_EDGE)
    return (scan["x"] + left_pad <= cursor[0] < scan["x"] + scan["w"] - right_pad
            and scan["y"] + top_pad <= cursor[1] < scan["y"] + scan["h"] - bottom_pad)


def _point_segment_distance_sq(px, py, ax, ay, bx, by):
    """Squared distance from a point to a finite 2D segment."""
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if not length_sq:
        return (px - ax) ** 2 + (py - ay) ** 2
    amount = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    nearest_x, nearest_y = ax + amount * dx, ay + amount * dy
    return (px - nearest_x) ** 2 + (py - nearest_y) ** 2


def _point_box_distance_sq(px, py, box):
    """Squared distance to an axis-aligned box (zero inside)."""
    left, top, right, bottom = box
    dx = max(left - px, 0, px - right)
    dy = max(top - py, 0, py - bottom)
    return dx * dx + dy * dy


def _hover_hit_geometry(chars):
    """Precompute the orientation-free hit corridor for one OCR line."""
    boxes = [char.get("box") for char in chars]
    if (not boxes or any(not isinstance(box, (list, tuple)) or len(box) != 4
                         for box in boxes)):
        return None
    centers = [((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
               for box in boxes]
    glyph_sizes = [min(max(1, box[2] - box[0]), max(1, box[3] - box[1]))
                   for box in boxes]
    typical = _median(glyph_sizes)
    pad = max(3, typical * 0.35)
    max_advance_sq = (typical * 2.5) ** 2
    segments = [(start, end) for start, end in zip(centers, centers[1:])
                if (end[0] - start[0]) ** 2 + (end[1] - start[1]) ** 2
                <= max_advance_sq]
    return {"boxes": boxes, "centers": centers, "segments": segments,
            "pad_sq": pad * pad,
            "bounds": (min(box[0] for box in boxes) - pad,
                       min(box[1] for box in boxes) - pad,
                       max(box[2] for box in boxes) + pad,
                       max(box[3] for box in boxes) + pad)}


def _hit_text(lines, screen_x, screen_y, region):
    """Return the OCR suffix under a desktop point, or None outside text.

    MeikiOCR gives one box per character. Picking the nearest character center
    within a line also fills the tiny gaps between glyph boxes, which makes the
    interaction feel like hovering DOM text instead of aiming at ink pixels.
    """
    if not region:
        return None
    x, y = screen_x - region["x"], screen_y - region["y"]
    if x < 0 or y < 0 or x >= region["w"] or y >= region["h"]:
        return None
    hits = []
    for line in lines or []:
        chars = line.get("chars") or []
        if not chars:
            continue
        geometry = line.get("_hit") or _hover_hit_geometry(chars)
        if geometry is None:
            continue
        line["_hit"] = geometry
        left, top, right, bottom = geometry["bounds"]
        if not (left <= x <= right and top <= y <= bottom):
            continue
        boxes, centers = geometry["boxes"], geometry["centers"]
        box_distance_sq = min(_point_box_distance_sq(x, y, box) for box in boxes)
        segment_distance_sq = min(
            (_point_segment_distance_sq(x, y, start[0], start[1], end[0], end[1])
             for start, end in geometry["segments"]), default=float("inf"))
        distance_sq = min(box_distance_sq, segment_distance_sq)
        if distance_sq > geometry["pad_sq"]:
            continue
        # Use both axes. MeikiOCR character centres trace the baseline even for
        # tilted lines, unlike the old horizontal-X / vertical-Y assumptions.
        index = min(range(len(chars)), key=lambda i:
                    (x - centers[i][0]) ** 2 + (y - centers[i][1]) ** 2)
        suffix = "".join(str(char.get("text") or "") for char in chars[index:])
        if suffix:
            hits.append((distance_sq / geometry["pad_sq"], suffix))
    return min(hits, default=(None, None), key=lambda hit: hit[0])[1]


_POPUP_PARTICLE_CHARS = set("はがをにでとへもやのかねよ")
_POPUP_POS = {
    "n": "noun", "pn": "pronoun", "adj-i": "い-adjective",
    "adj-na": "な-adjective", "adj-no": "の-adjective", "adv": "adverb",
    "adv-to": "adverb (と)", "aux": "auxiliary", "aux-v": "auxiliary verb",
    "aux-adj": "auxiliary adjective", "conj": "conjunction", "cop": "copula",
    "ctr": "counter", "exp": "expression", "int": "interjection",
    "prt": "particle", "pref": "prefix", "suf": "suffix", "num": "numeric",
    "v1": "ichidan verb", "v5": "godan verb", "v5r": "godan verb (-る)",
    "v5u": "godan verb (-う)", "v5k": "godan verb (-く)",
    "v5g": "godan verb (-ぐ)", "v5s": "godan verb (-す)",
    "v5t": "godan verb (-つ)", "v5n": "godan verb (-ぬ)",
    "v5b": "godan verb (-ぶ)", "v5m": "godan verb (-む)",
    "vs": "する verb", "vs-i": "する verb (irregular)",
    "vs-s": "する verb (special)", "vk": "くる verb",
    "vi": "intransitive verb", "vt": "transitive verb", "vz": "ずる verb",
}
_POPUP_MISC = {
    "uk": "usu. kana", "col": "colloquial", "sl": "slang",
    "vulg": "vulgar", "fam": "familiar", "hon": "honorific",
    "hum": "humble", "pol": "polite", "arch": "archaic",
    "obs": "obsolete", "rare": "rare", "dated": "dated",
    "hist": "historical", "fem": "female term", "male": "male term",
    "form": "formal", "euph": "euphemistic", "abbr": "abbreviation",
    "on-mim": "onomatopoeia", "joc": "jocular", "derog": "derogatory",
    "poet": "poetic", "chn": "children's term", "yoji": "four-character idiom",
    "proverb": "proverb",
}
_POPUP_DATED = {"arch", "obs", "rare"}
_POPUP_REASON = {
    "-た": "past", "-て": "-te form", "-ば": "conditional",
    "-たら": "conditional (tara)", "-たり": "-tari", "-く": "adverbial",
    "-さ": "-sa nominal", "-ず": "without doing",
    "-ぬ": "negative (archaic)", "-ん": "negative (casual)",
    "-ゃ": "contraction", "-ちゃ": "contraction (-cha)",
    "continuative": "masu stem", "-まい": "won't/probably not",
    "potential or passive": "potential or passive",
}


def _popup_candidates(candidates):
    """Remove name noise caused by popup OCR having no tokenizer boundary.

    ``scan()`` normally receives kuromoji's hovered token. Popup OCR only has a
    character suffix, so a name such as 夢か (Yumeka) can consume the following
    question particle and outrank the common word 夢. Discard that narrow class
    of kana-only name overmatch. If the resulting winner is a real word, also
    hide same-spelling personal names (夢 = Ayumi, etc.) in the compact popup.
    Pure-name hits and katakana names such as レオン remain untouched.
    """
    candidates = list(candidates or [])
    established_words = [candidate for candidate in candidates
                         if candidate.get("kind") == "word"
                         and (candidate.get("entry", {}).get("c") or
                              isinstance(candidate.get("entry", {}).get("vr"), int)
                              and candidate["entry"]["vr"] <= 6600)]

    def particle_name_overmatch(candidate):
        if candidate.get("kind") != "name":
            return False
        matched = candidate.get("matched") or ""
        for word in established_words:
            base = word.get("matched") or ""
            extension = matched[len(base):] if base and matched.startswith(base) else ""
            if 0 < len(extension) <= 2 and all(ch in _POPUP_PARTICLE_CHARS
                                               for ch in extension):
                return True
        return False

    candidates = [candidate for candidate in candidates
                  if not particle_name_overmatch(candidate)]
    if candidates and candidates[0].get("kind") == "word":
        surface = candidates[0].get("matched")
        candidates = [candidate for candidate in candidates
                      if not (candidate.get("kind") == "name"
                              and candidate.get("matched") == surface)]
    return candidates


def _popup_entries(candidates, limit=3):
    """Shape ranked results like the in-app popup for native presentation."""
    rendered = []
    for candidate in _popup_candidates(candidates):
        entry = candidate.get("entry") or {}
        senses = entry.get("s") or []
        readings = entry.get("r") or []
        writings = entry.get("k") or []
        all_usually_kana = (candidate.get("kind") != "name" and writings and senses
                            and all("uk" in (sense.get("misc") or [])
                                    for sense in senses))
        word = ((readings[0] if readings else candidate.get("matched", ""))
                if all_usually_kana else
                ((writings[0] if writings else None)
                 or (readings[0] if readings else None)
                 or candidate.get("matched", "")))
        reading = candidate.get("mr") or (readings[0] if readings else "")
        modern = [sense for sense in senses
                  if not _POPUP_DATED.intersection(sense.get("misc") or [])]
        visible_senses = modern or senses
        definitions = []
        popup_senses = []
        seen_glosses = set()
        for sense in visible_senses:
            glosses = []
            for gloss in (sense.get("gloss") or []):
                gloss = str(gloss).strip()
                key = gloss.casefold()
                if gloss and key not in seen_glosses:
                    seen_glosses.add(key)
                    glosses.append(gloss)
            if glosses:
                definition = "; ".join(glosses)
                definitions.append(definition)
                popup_senses.append({
                    "number": len(popup_senses) + 1,
                    "pos": ", ".join(_POPUP_POS.get(tag, tag)
                                     for tag in (sense.get("pos") or [])),
                    "misc": ", ".join(_POPUP_MISC.get(tag, tag)
                                      for tag in (sense.get("misc") or [])),
                    "definition": definition,
                })
        if not word or not definitions:
            continue
        reasons = candidate.get("reasons") or []
        reason_labels = [_POPUP_REASON.get(str(reason), str(reason)) for reason in reasons]
        tag = "name" if candidate.get("kind") == "name" else ""
        if reason_labels:
            tag = "inflected: " + " > ".join(reason_labels)
        alternatives = ([*writings, *readings[1:]] if all_usually_kana else
                        ([*writings[1:]] if writings else [*readings[1:]]))
        rendered.append({"word": word, "reading": reading,
                         "definitions": definitions, "senses": popup_senses,
                         "tag": tag, "kind": candidate.get("kind") or "word",
                         "inflection": (candidate.get("matched", "") + "  ·  " +
                                        " › ".join(reason_labels))
                                       if reason_labels else "",
                         "frequency": entry.get("vr"),
                         "alternatives": alternatives[:4]})
        if len(rendered) >= limit:
            break
    return rendered


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
    """One MeikiOCR monitor with reader and Caps Lock popup presentation."""

    def __init__(self, publish, paused_flag, lookup=None):
        self._publish = publish
        self._paused = paused_flag
        self._lookup = lookup
        self.region = load_region()
        self.mode = "reader"
        self.popup_theme = dict(_DEFAULT_POPUP_THEME)
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
                "mode": self.mode,
                "caps_lock": _caps_lock_on() if self.mode == "popup" else False,
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

    def set_mode(self, mode, theme=None):
        if mode not in ("reader", "popup"):
            return "OCR mode must be reader or popup"
        if isinstance(theme, dict):
            clean = {key: value for key, value in theme.items()
                     if key in _DEFAULT_POPUP_THEME and isinstance(value, str)
                     and re.fullmatch(r"#[0-9a-fA-F]{6}", value.strip())}
            self.popup_theme.update({key: value.strip() for key, value in clean.items()})
        self.mode = mode
        return None

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
        popup = _PopupBridge()
        try:
            engine = make_engine()          # slow on first start: model load/download
            self.engine_name = engine.name
            self.starting = False
            self.running = True
            last_hash = None
            seen = collections.deque(maxlen=2)     # unconfirmed OCR signatures
            handled = collections.deque(maxlen=4)  # signatures already read
            recent = collections.deque(maxlen=6)   # (key, raw) of recent publishes
            last_mode = None
            popup_hash = None
            popup_scan = None
            next_popup_read = 0.0
            popup_shown = False
            popup_anchor = None
            last_popup_theme = None
            last_popup_region = None
            hover_lines = []
            last_lookup = None
            popup_entries = []
            while not self._stop.is_set():
                mode = self.mode
                if mode != last_mode:
                    popup.hide()
                    if mode == "reader":
                        popup.close()
                    last_mode = mode
                    last_hash = popup_hash = None
                    popup_scan = None
                    next_popup_read = 0.0
                    popup_shown = False
                    popup_anchor = None
                    last_popup_theme = None
                    last_popup_region = None
                    hover_lines = []
                    last_lookup = None
                    popup_entries = []
                    seen.clear()
                    handled.clear()
                    self.trace = {"mode": mode}
                time.sleep(0.08 if mode == "popup" else 0.3)
                if self._paused.is_set():
                    popup.hide()
                    popup_shown = False
                    if mode == "popup":
                        popup_scan = None
                        hover_lines = []
                    continue
                r = self.region

                if mode == "popup":
                    caps_on = _caps_lock_on()
                    self.trace["caps_lock"] = caps_on
                    cursor = _cursor_pos()
                    if not caps_on or cursor is None:
                        popup.hide()
                        popup_shown = False
                        popup_scan = None
                        hover_lines = []
                        last_lookup = None
                        popup_entries = []
                        continue
                    if not (r["x"] <= cursor[0] < r["x"] + r["w"] and
                            r["y"] <= cursor[1] < r["y"] + r["h"]):
                        popup.hide()
                        popup_shown = False
                        popup_anchor = None
                        popup_scan = None
                        hover_lines = []
                        last_lookup = None
                        popup_entries = []
                        continue
                    region_key = (r["x"], r["y"], r["w"], r["h"])
                    if (last_popup_region != region_key or popup_scan is None
                            or not _popup_scan_covers(popup_scan, r, cursor)):
                        popup_scan = _popup_scan_region(r, cursor)
                        last_popup_region = region_key
                        popup_hash = None
                        next_popup_read = 0.0
                        hover_lines = []
                        last_lookup = None
                        popup_entries = []
                        popup_shown = False

                    now = time.monotonic()
                    if now >= next_popup_read:
                        try:
                            px = capture_bmp(popup_scan["x"], popup_scan["y"],
                                             popup_scan["w"], popup_scan["h"])
                        except Exception:
                            popup.hide()
                            popup_shown = False
                            continue
                        frame_hash = hashlib.md5(px).digest()
                        if frame_hash != popup_hash:
                            popup_hash = frame_hash
                            started = time.monotonic()
                            text = _clean(engine.recognize(
                                pixels=px, width=popup_scan["w"],
                                height=popup_scan["h"]))
                            elapsed = time.monotonic() - started
                            # Animated fullscreen scenes used to keep ONNX busy
                            # continuously. Bound model duty cycle near 25%; a
                            # static frame is still checked cheaply five times/s.
                            next_popup_read = time.monotonic() + max(
                                0.30, min(1.50, elapsed * 3.0))
                            hover_lines = engine.hover_lines
                            self.trace["read"] = text
                            self.trace["lines"] = engine.line_trace
                            self.trace["ocr_ms"] = round(elapsed * 1000)
                            self.trace["scan_region"] = popup_scan
                        else:
                            next_popup_read = now + 0.20
                    lookup_text = _hit_text(
                        hover_lines, cursor[0], cursor[1], popup_scan)
                    self.trace["hover"] = lookup_text
                    if not lookup_text:
                        popup.hide()
                        popup_shown = False
                        popup_anchor = None
                        last_lookup = None
                        popup_entries = []
                        continue
                    lookup_changed = lookup_text != last_lookup
                    if lookup_changed:
                        last_lookup = lookup_text
                        candidates = self._lookup(lookup_text) if self._lookup else []
                        popup_entries = _popup_entries(candidates)
                    if popup_entries:
                        theme_key = tuple(sorted(self.popup_theme.items()))
                        moved_far = (popup_anchor is not None and
                                     abs(cursor[0] - popup_anchor[0]) +
                                     abs(cursor[1] - popup_anchor[1]) > 160)
                        if (lookup_changed or not popup_shown or moved_far
                                or theme_key != last_popup_theme):
                            if not popup.send({"show": True, "x": cursor[0],
                                               "y": cursor[1], "region": r,
                                               "entries": popup_entries,
                                               "theme": self.popup_theme}):
                                raise RuntimeError(
                                    "the on-screen OCR popup could not start")
                            popup_shown = True
                            popup_anchor = cursor
                            last_popup_theme = theme_key
                    else:
                        popup.hide()
                        popup_shown = False
                        popup_anchor = None
                    continue

                try:
                    px = capture_bmp(r["x"], r["y"], r["w"], r["h"])
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
                text = _clean(engine.recognize(
                    pixels=px, width=r["w"], height=r["h"]))
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
            popup.close()
            if engine:
                engine.close()
            self.running = False
            self.starting = False
            self.engine_name = None


if __name__ == "__main__":
    if "--pick-region" in sys.argv:
        pick_region_main()
    elif "--ocr-popup" in sys.argv:
        popup_window_main()
