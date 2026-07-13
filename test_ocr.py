"""Unit tests for ocr.py's pure logic — the parts that don't need a screen,
an engine, or manga-ocr installed. These encode the documented OCR bug
classes (scrambled reading order, seam placement, edge junk, jitter dedup)
so a refactor can't silently reintroduce them.

Needs no dict.sqlite (coverage tests skip without it) and runs anywhere:
    python test_ocr.py
"""
import os
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


def line(text, x, y, w, h, ws=None):
    return {"text": text, "x": x, "y": y, "w": w, "h": h, "ws": ws or []}


# --------------------------------------------------------------------------- #
# _reading_order: row clustering — the でかい……そして scramble class
# --------------------------------------------------------------------------- #
def test_reading_order():
    # Windows split one visual line at the ellipsis, with 2px of y jitter,
    # and returned the fragments in the wrong order. Must come back as ONE
    # line, left-to-right.
    frags = [line("そして", 260, 102, 180, 40),
             line("でかい……", 20, 100, 220, 42)]
    out = ocr._reading_order(frags)
    check("ellipsis split heals into one line", len(out), 1)
    check("fragments join left-to-right", out[0]["text"], "でかい…… そして")

    # Two real rows stay two lines, top-down, regardless of input order.
    rows = [line("二行目", 20, 200, 300, 40), line("一行目", 20, 100, 300, 40)]
    out = ocr._reading_order(rows)
    check("rows order top-down", [l["text"] for l in out], ["一行目", "二行目"])

    # Same row but a huge gap (> 2x height): separate columns, not one line —
    # NVL games put speaker name and dialogue far apart.
    cols = [line("右", 900, 100, 60, 40), line("左", 20, 100, 60, 40)]
    out = ocr._reading_order(cols)
    check("distant same-row fragments stay separate",
          [l["text"] for l in out], ["左", "右"])


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
# HybridOcr._spans: contiguous tiling (self-free — uses no instance state)
# --------------------------------------------------------------------------- #
def test_spans():
    ws = [{"x": x, "w": 80} for x in range(0, 1000, 100)]
    l = line("x" * 10, 0, 0, 1000, 50, ws)
    spans = ocr.HybridOcr._spans(None, l)
    check("spans start at the line's left edge", spans[0][0], 0)
    check("spans end at the line's right edge", spans[-1][1], 1000)
    check("spans tile contiguously (no glyph can fall between crops)",
          all(a[1] == b[0] for a, b in zip(spans, spans[1:])), True)
    check("multi-chunk line actually splits", len(spans) > 1, True)
    # No word boxes at all (Windows missed every word): one span, whole line.
    solo = ocr.HybridOcr._spans(None, line("あ", 5, 0, 400, 50))
    check("no word boxes -> single whole-line span", solo, [(5, 405)])
    # shrink_first moves the seams (the dual-read voting depends on this).
    shifted = ocr.HybridOcr._spans(None, l, shrink_first=3 * 50)
    check("shrink_first places seams elsewhere",
          shifted[1:] != spans[1:] or len(shifted) != len(spans), True)


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
# _has_japanese gate + _dict_coverage (coverage skips without dict.sqlite)
# --------------------------------------------------------------------------- #
def test_gates():
    check("Japanese passes the gate", ocr._has_japanese("まもなく電車が参ります"), True)
    check("one misread kanji can't open the gate", ocr._has_japanese("ii冊 Program Files"), False)
    check("empty fails the gate", ocr._has_japanese(""), False)

    if not os.path.isfile(ocr._COV_DB_PATH):
        print("SKIP  _dict_coverage: no dict.sqlite (run python setup.py)")
        return
    good = ocr._dict_coverage("まもなく電車が参ります")
    seam = ocr._dict_coverage("もなく電車が参ります")
    check("seam-dropped read covers worse than the full read", good > seam, True)
    check("full read segments nearly clean", good > 0.85, True)


def main():
    for t in (test_reading_order, test_clean, test_same_line, test_spans,
              test_png, test_gates):
        t()
    print(f"\n{TOTAL - FAILURES}/{TOTAL} passed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
