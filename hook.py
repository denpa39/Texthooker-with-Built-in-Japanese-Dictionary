"""
Embedded Textractor: attach to a running game and stream its text directly,
no clipboard round-trip and no separate Textractor window.

Drives TextractorCLI.exe (downloaded by setup.py into textractor/) as a child
process. Flow:

    list_processes()  -> windowed processes the user can pick from
    attach(pid)       -> spawn the right-bitness CLI, hook the game
    hooks()           -> live snapshot {hook key: last line} for the picker UI
    pick(key)         -> only that hook's lines are published to the reader
    detach()          -> kill the CLI

Every hooked function in the game produces its own text stream; most are junk
(UI labels, backlog re-renders). Like Textractor's own dropdown, the user picks
the hook whose preview shows the actual dialogue.
"""

import ctypes
import io
import os
import subprocess
import sys
import threading
from ctypes import wintypes

# Frozen (PyInstaller) builds keep textractor/ beside the .exe.
BASE_DIR = (os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__)))
CLI_X64 = os.path.join(BASE_DIR, "textractor", "x64", "TextractorCLI.exe")
CLI_X86 = os.path.join(BASE_DIR, "textractor", "x86", "TextractorCLI.exe")


def available():
    """Textractor CLIs downloaded? (setup.py --textractor)"""
    return os.path.isfile(CLI_X64) or os.path.isfile(CLI_X86)


# --------------------------------------------------------------------------- #
# Windowed-process enumeration (ctypes, no dependencies)
# --------------------------------------------------------------------------- #
if sys.platform == "win32":
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    _EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

_SKIP_EXE = {
    "explorer.exe", "textinputhost.exe", "applicationframehost.exe",
    "systemsettings.exe", "shellexperiencehost.exe", "searchhost.exe",
    "startmenuexperiencehost.exe", "python.exe", "pythonw.exe",
}


def _exe_name(pid):
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(260)
        size = wintypes.DWORD(260)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        kernel32.CloseHandle(h)


def _is_32bit(pid):
    """True if `pid` is a 32-bit process (on 64-bit Windows) -> use the x86 CLI."""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        wow64 = wintypes.BOOL()
        if kernel32.IsWow64Process(h, ctypes.byref(wow64)):
            return bool(wow64.value)
        return False
    finally:
        kernel32.CloseHandle(h)


def list_processes():
    """Visible top-level windows -> [{pid, exe, title}], deduped by pid.
    This is what the 'Attach' picker shows; VN games always have a window."""
    if sys.platform != "win32":
        return []
    results = {}

    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        pid = pid.value
        if pid == os.getpid() or pid in results:
            return True
        exe = _exe_name(pid)
        if exe.lower() in _SKIP_EXE:
            return True
        results[pid] = {"pid": pid, "exe": exe, "title": buf.value}
        return True

    user32.EnumWindows(_EnumWindowsProc(cb), 0)
    return sorted(results.values(), key=lambda p: p["title"].lower())


# --------------------------------------------------------------------------- #
# TextractorCLI driver
# --------------------------------------------------------------------------- #
class Hooker:
    """One attached game at a time. Lines from the *picked* hook go to `publish`."""

    def __init__(self, publish):
        self._publish = publish
        self._lock = threading.Lock()
        self._proc = None
        self._reader = None
        self.pid = None
        self.exe = ""
        self.picked = None      # hook key whose lines reach the reader
        self._hooks = {}        # key -> {"last": str, "count": int}

    # -- state for the UI -------------------------------------------------- #
    def state(self):
        with self._lock:
            alive = self._proc is not None and self._proc.poll() is None
            return {
                "attached": alive, "pid": self.pid, "exe": self.exe,
                "picked": self.picked,
                "hooks": [{"key": k, "last": v["last"][-80:], "count": v["count"]}
                          for k, v in sorted(self._hooks.items(),
                                             key=lambda kv: -kv[1]["count"])],
            }

    def pick(self, key):
        with self._lock:
            self.picked = key or None

    # -- lifecycle ---------------------------------------------------------- #
    def attach(self, pid):
        if sys.platform != "win32":
            return "hooking is Windows-only"
        cli = CLI_X86 if _is_32bit(pid) else CLI_X64
        if not os.path.isfile(cli):
            return ("Textractor not downloaded — run:  "
                    "python setup.py --skip-kuromoji --textractor")
        self.detach()
        try:
            proc = subprocess.Popen(
                [cli], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, cwd=os.path.dirname(cli),
                creationflags=subprocess.CREATE_NO_WINDOW)
            proc.stdin.write(f"attach -P{pid}\n".encode("utf-16-le"))
            proc.stdin.flush()
        except OSError as e:
            return f"could not start TextractorCLI: {e}"
        with self._lock:
            self._proc = proc
            self.pid = pid
            self.exe = _exe_name(pid)
            self.picked = None
            self._hooks = {}
        self._reader = threading.Thread(target=self._read_loop, args=(proc,), daemon=True)
        self._reader.start()
        return None

    def detach(self):
        with self._lock:
            proc, self._proc = self._proc, None
            self.pid, self.exe, self.picked = None, "", None
            self._hooks = {}
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    # -- output pump --------------------------------------------------------- #
    def _read_loop(self, proc):
        """CLI stdout is UTF-16-LE, one sentence per line:  [hook info] text.
        TextIOWrapper decodes incrementally — never split the raw byte stream on
        b'\\n' yourself, the newline is two bytes and everything shears off-by-one."""
        out = io.TextIOWrapper(proc.stdout, encoding="utf-16-le", errors="replace")
        for line in out:
            line = line.strip("\r\n\x00 ﻿")
            if not line:
                continue
            key, text = self._split(line)
            if key is None:
                continue
            with self._lock:
                if proc is not self._proc:
                    return              # a newer attach superseded this reader
                h = self._hooks.setdefault(key, {"last": "", "count": 0})
                h["last"] = text
                h["count"] += 1
                picked = self.picked
            if text and key == picked:
                self._publish(text)

    @staticmethod
    def _split(line):
        """'[19:1A2C:...:GetGlyphOutlineW] こんにちは' -> (hook key, text).
        The console-ish hooks Textractor logs about itself have no bracket prefix."""
        if not line.startswith("["):
            return None, None
        end = line.find("]")
        if end < 0:
            return None, None
        return line[1:end], line[end + 1:].lstrip()
