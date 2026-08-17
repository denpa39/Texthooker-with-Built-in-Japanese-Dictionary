"""Unit tests for ocr.py's pure logic — the parts that don't need a screen or
loaded MeikiOCR models. These cover result shaping, hover hit-testing, compact
popup data, edge cleanup, and jitter dedup.

Needs no dict.sqlite and runs anywhere:
    python test_ocr.py
"""
import json
import struct
import sys
import threading
import zlib

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

import ocr

FAILURES = 0
TOTAL = 0


def check(label, got, want):
    global FAILURES, TOTAL
    TOTAL += 1
    if got == want:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}: got {got!r}, want {want!r}")
        FAILURES += 1


# --------------------------------------------------------------------------- #
# _clean: OCR artifact repair
# --------------------------------------------------------------------------- #
def test_clean():
    check("spaces stripped", ocr._clean("ま もなく　電車が"), "まもなく電車が")
    check("dash before kanji is 一", ocr._clean("-番だ"), "一番だ")
    check("dot runs become ……", ocr._clean("きた．．．．"), "きた……")
    check("edge cursor junk stripped", ocr._clean("▼こんにちは▼"), "こんにちは")
    check("ASCII tail junk dropped", ocr._clean("行くぞ-6"), "行くぞ")
    check("sentence enders survive", ocr._clean("行った。"), "行った。")


# --------------------------------------------------------------------------- #
# _norm + _same_line: the jitter dedup key
# --------------------------------------------------------------------------- #
def test_same_line():
    check("trailing maru shares a key", ocr._norm("…した。"), ocr._norm("…した"))
    check("kana flip is the same line", ocr._same_line("だから言った", "たから言った"), True)
    check("truncated re-read is the same line", ocr._same_line("油断していた", "していた"), True)
    check("different short lines stay apart", ocr._same_line("はい", "まさか"), False)


# --------------------------------------------------------------------------- #
# MeikiOcr result shaping: geometry, ruby filtering, and reading order
# --------------------------------------------------------------------------- #
def test_meiki_results():
    def chars(text, x, y, size, conf=0.9):
        return [{"char": ch, "bbox": [x + i * size, y,
                                        x + (i + 1) * size, y + size],
                 "conf": conf}
                for i, ch in enumerate(text)]

    results = [
        {"text": "ほんぶん", "chars": chars("ほんぶん", 10, 100, 30),
         "is_vertical": False},
        {"text": "よみ", "chars": chars("よみ", 10, 80, 10),
         "is_vertical": False},
        {"text": "次の行", "chars": chars("次の行", 10, 150, 30),
         "is_vertical": False},
    ]
    text, trace, hover = ocr.MeikiOcr._format_results(results)
    check("meiki result drops small furigana line", text, "ほんぶん\n次の行")
    check("meiki trace preserves line confidence", trace[0]["conf"], 0.9)
    check("meiki hover keeps character boxes", hover[0]["chars"][1],
          {"text": "ん", "box": [40, 100, 70, 130]})

    vertical = [
        {"text": "左列", "chars": chars("左列", 50, 10, 20), "is_vertical": True},
        {"text": "右列", "chars": chars("右列", 100, 10, 20), "is_vertical": True},
    ]
    text, _, _ = ocr.MeikiOcr._format_results(vertical)
    check("vertical meiki columns read right-to-left", text, "右列\n左列")


def test_hover_lookup():
    horizontal = [{"text": "日本語", "vertical": False, "chars": [
        {"text": "日", "box": [0, 0, 20, 30]},
        {"text": "本", "box": [24, 0, 44, 30]},
        {"text": "語", "box": [48, 0, 68, 30]},
    ]}]
    region = {"x": 100, "y": 200, "w": 300, "h": 100}
    check("hover maps desktop point to OCR suffix",
          ocr._hit_text(horizontal, 130, 215, region), "本語")
    check("hover fills the gap between character boxes",
          ocr._hit_text(horizontal, 122, 215, region), "日本語")
    check("hover outside selected region does nothing",
          ocr._hit_text(horizontal, 50, 215, region), None)

    vertical = [{"text": "縦書き", "vertical": True, "chars": [
        {"text": "縦", "box": [5, 0, 25, 20]},
        {"text": "書", "box": [5, 22, 25, 42]},
        {"text": "き", "box": [5, 44, 25, 64]},
    ]}]
    check("vertical hover uses the y axis",
          ocr._hit_text(vertical, 115, 250, region), "き")

    diagonal = [{"text": "斜め文", "vertical": False, "chars": [
        {"text": "斜", "box": [0, 40, 20, 60]},
        {"text": "め", "box": [25, 30, 45, 50]},
        {"text": "文", "box": [50, 20, 70, 40]},
    ]}]
    check("diagonal hover follows both character axes",
          ocr._hit_text(diagonal, 135, 240, region), "め文")
    check("blank corner of a diagonal line is not an invisible hit target",
          ocr._hit_text(diagonal, 105, 223, region), None)
    check("diagonal hover still bridges a normal character gap",
          ocr._hit_text(diagonal, 123, 246, region), "め文")

    fullscreen = {"x": 0, "y": 0, "w": 3840, "h": 2160}
    tile = ocr._popup_scan_region(fullscreen, (1920, 1080))
    check("fullscreen popup OCR uses a bounded local tile",
          (tile["w"], tile["h"]), (1024, 640))
    check("hover tile leaves suffix context after the pointer",
          (1920 - tile["x"], 1080 - tile["y"]), (341, 213))
    check("cursor stays inside the reusable hover tile",
          ocr._popup_scan_covers(tile, fullscreen, (2000, 1100)), True)
    check("cursor near a tile edge requests a fresh OCR tile",
          ocr._popup_scan_covers(tile, fullscreen,
                                 (tile["x"] + tile["w"] - 20, 1100)), False)
    small = {"x": 100, "y": 200, "w": 600, "h": 180}
    check("normal text-box selections still scan at full resolution",
          ocr._popup_scan_region(small, (400, 250)), small)


def test_popup_entries():
    candidates = [{"matched": "食べた", "kind": "word", "reasons": ["past"],
                   "entry": {"k": ["食べる"], "r": ["たべる"],
                             "s": [{"gloss": ["to eat", "to consume"],
                                    "pos": ["v1", "vt"], "misc": ["col"]}]}}]
    rendered = ocr._popup_entries(candidates)
    check("popup uses ranked dictionary headword", rendered[0]["word"], "食べる")
    check("popup includes reading", rendered[0]["reading"], "たべる")
    check("popup keeps compact definitions", rendered[0]["definitions"],
          ["to eat; to consume"])
    check("popup labels inflection", rendered[0]["tag"], "inflected: past")
    check("popup mirrors numbered in-app senses",
          rendered[0]["senses"][0]["number"], 1)
    check("popup expands the in-app part-of-speech labels",
          rendered[0]["senses"][0]["pos"], "ichidan verb, transitive verb")
    check("popup expands the in-app usage labels",
          rendered[0]["senses"][0]["misc"], "colloquial")

    dream = {"matched": "夢", "len": 1, "kind": "word", "reasons": [],
             "entry": {"k": ["夢"], "r": ["ゆめ"], "c": True, "vr": 474,
                       "s": [{"gloss": ["dream"], "misc": []}]}}
    candidates = [
        {"matched": "夢か", "len": 2, "kind": "name", "reasons": [],
         "entry": {"k": ["夢か"], "r": ["ゆめか"],
                   "s": [{"gloss": ["Yumeka"]}]}},
        dream,
        {"matched": "夢", "len": 1, "kind": "name", "reasons": [],
         "entry": {"k": ["夢"], "r": ["あゆみ"],
                   "s": [{"gloss": ["Ayumi"]}]}}
    ]
    rendered = ocr._popup_entries(candidates)
    check("question particle cannot create a longer person-name hit",
          [entry["word"] for entry in rendered], ["夢"])
    check("common word definition wins the compact popup",
          rendered[0]["definitions"], ["dream"])

    dream["entry"]["s"].append(
        {"gloss": ["dream", "hope", "wish"], "misc": []})
    dream["entry"]["s"].append(
        {"gloss": ["a rare dated sense"], "misc": ["arch"]})
    rendered = ocr._popup_entries([dream])
    check("later senses do not repeat an earlier gloss",
          rendered[0]["definitions"], ["dream", "hope; wish"])
    check("native popup keeps the full modern sense list",
          [sense["definition"] for sense in rendered[0]["senses"]],
          ["dream", "hope; wish"])


def test_popup_ipc_encoding():
    command = {"show": True, "entries": [
        {"word": "天使", "reading": "てんし", "definitions": ["angel"]}
    ]}
    raw = json.dumps(command, ensure_ascii=False).encode("utf-8")
    check("popup UTF-8 pipe preserves Japanese headword and reading",
          ocr._decode_popup_command(raw), command)
    check("popup wheel scrolls down", ocr._popup_wheel_units(-120), 3)
    check("popup wheel scrolls up", ocr._popup_wheel_units(120), -3)
    check("popup ignores an empty wheel event", ocr._popup_wheel_units(0), 0)
    palette = ocr._popup_palette(ocr._DEFAULT_POPUP_THEME)
    check("popup derives a softer app-style card surface", palette["surface"], "#e2e8f0")


def test_modes():
    source = ocr.OcrSource(lambda _text: None, threading.Event(), lambda _text: [])
    check("OCR defaults to reader mode", source.mode, "reader")
    check("OCR switches to popup mode", source.set_mode("popup"), None)
    check("OCR state exposes popup mode", source.state()["mode"], "popup")
    source.set_mode("popup", {"bg": "#123456", "text": "not-a-colour", "hack": "#ffffff"})
    check("popup accepts core theme colours", source.popup_theme["bg"], "#123456")
    check("popup rejects invalid theme colours", source.popup_theme["text"],
          ocr._DEFAULT_POPUP_THEME["text"])
    check("popup ignores unknown theme keys", "hack" in source.popup_theme, False)
    check("OCR rejects unknown modes", source.set_mode("telepathy"),
          "OCR mode must be reader or popup")
    check("rejected mode does not alter state", source.mode, "popup")


# --------------------------------------------------------------------------- #
# _encode_png: the Anki screenshot encoder (decode it back and check pixels)
# --------------------------------------------------------------------------- #
def test_png():
    # 2x1: pure red then pure blue, BGRA order, alpha 0 as GDI leaves it.
    png = ocr._encode_png(b"\x00\x00\xff\x00" + b"\xff\x00\x00\x00", 2, 1)
    check("PNG signature", png[:8], b"\x89PNG\r\n\x1a\n")
    w, h = struct.unpack(">II", png[16:24])
    check("IHDR dimensions", (w, h), (2, 1))
    idat_at = png.index(b"IDAT") + 4
    idat_len = struct.unpack(">I", png[idat_at - 8:idat_at - 4])[0]
    raw = zlib.decompress(png[idat_at:idat_at + idat_len])
    check("scanline filter byte 0", raw[0], 0)
    check("BGRA swapped to RGBA, alpha forced opaque",
          raw[1:9], b"\xff\x00\x00\xff" + b"\x00\x00\xff\xff")


# --------------------------------------------------------------------------- #
# _has_japanese gate
# --------------------------------------------------------------------------- #
def test_gates():
    check("Japanese passes the gate", ocr._has_japanese("まもなく電車が参ります"), True)
    check("one misread kanji can't open the gate", ocr._has_japanese("ii冊 Program Files"), False)
    check("empty fails the gate", ocr._has_japanese(""), False)


def main():
    for t in (test_clean, test_same_line, test_meiki_results, test_hover_lookup,
              test_popup_entries, test_popup_ipc_encoding, test_modes,
              test_png, test_gates):
        t()
    print(f"\n{TOTAL - FAILURES}/{TOTAL} passed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
