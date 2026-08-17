"""Unit tests for ocr.py's pure logic — the parts that don't need a screen or
loaded MeikiOCR models. These cover result shaping, edge cleanup, and jitter
dedup so a refactor cannot silently reintroduce those bug classes.

Needs no dict.sqlite (coverage tests skip without it) and runs anywhere:
    python test_ocr.py
"""
import struct
import sys
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
    text, trace = ocr.MeikiOcr._format_results(results)
    check("meiki result drops small furigana line", text, "ほんぶん\n次の行")
    check("meiki trace preserves line confidence", trace[0]["conf"], 0.9)

    vertical = [
        {"text": "左列", "chars": chars("左列", 50, 10, 20), "is_vertical": True},
        {"text": "右列", "chars": chars("右列", 100, 10, 20), "is_vertical": True},
    ]
    text, _ = ocr.MeikiOcr._format_results(vertical)
    check("vertical meiki columns read right-to-left", text, "右列\n左列")


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
    for t in (test_clean, test_same_line, test_meiki_results, test_png, test_gates):
        t()
    print(f"\n{TOTAL - FAILURES}/{TOTAL} passed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
