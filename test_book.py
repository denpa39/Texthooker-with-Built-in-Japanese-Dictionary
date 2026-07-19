"""E-book parser units: epub spine order, ruby stripping, block splitting,
title; txt (Aozora markup, cp932); html; fb2 (+zipped); mobi (PalmDB +
PalmDOC decompression); format dispatch.
Pure stdlib, no network, no dict.sqlite — runs in CI."""

import io
import struct
import zipfile

import book

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""

OPF = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>吾輩は猫である</dc:title>
  </metadata>
  <manifest>
    <item id="ch2" href="text/ch2.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch1" href="text/ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="cover" href="cover.jpg" media-type="image/jpeg"/>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
  </manifest>
  <spine toc="ncx">
    <itemref idref="ch1"/>
    <itemref idref="cover"/>
    <itemref idref="ch2"/>
  </spine>
</package>"""

CH1 = """<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">
<head><title>chapter one title must not leak</title><style>p{color:red}</style></head>
<body>
  <p><ruby>吾輩<rt>わがはい</rt></ruby>は<ruby>猫<rt>ねこ</rt><rp>(</rp><rp>)</rp></ruby>である。</p>
  <p>名前は
まだ無い。</p>
  <p>ライン1<br/>ライン2</p>
  <p>   </p>
</body></html>"""

CH2 = """<html><body><h1>第二章</h1><div>どこで生れたかとんと見当がつかぬ。</div></body></html>"""


def make_epub(container=CONTAINER, opf=OPF, files=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("OEBPS/content.opf", opf)
        for path, content in (files or {"OEBPS/text/ch1.xhtml": CH1,
                                        "OEBPS/text/ch2.xhtml": CH2}).items():
            zf.writestr(path, content)
    return buf.getvalue()


def main():
    title, lines = book.parse_epub(make_epub())
    assert title == "吾輩は猫である", title
    # ruby bases kept, furigana <rt> and <rp> gone
    assert lines[0] == "吾輩は猫である。", lines[0]
    # internal newline collapsed, empty <p> dropped
    assert lines[1] == "名前は まだ無い。", lines[1]
    # <br/> splits a paragraph into two reader lines
    assert lines[2] == "ライン1" and lines[3] == "ライン2", lines[2:4]
    # spine order (ch1 before ch2 despite manifest listing ch2 first),
    # non-html spine items skipped, <head> text never leaks
    assert lines[4] == "第二章" and lines[5].startswith("どこで生れたか"), lines[4:]
    assert len(lines) == 6, lines
    assert not any("わがはい" in l or "must not leak" in l for l in lines)

    # not-an-epub inputs raise ValueError, never crash
    for bad in (b"", b"not a zip", make_epub(container="<broken")):
        try:
            book.parse_epub(bad)
            assert False, "expected ValueError"
        except ValueError:
            pass

    # --- txt: Aozora Bunko markup --------------------------------------- #
    aozora = """吾輩は猫である
夏目漱石

-------------------------------------------------------
【テキスト中に現れる記号について】
《》：ルビ
-------------------------------------------------------

　｜吾輩《わがはい》は猫《ねこ》である。［＃「猫」に傍点］
　名前はまだ無い。

底本：「吾輩は猫である」新潮文庫
入力：もんじゅ
"""
    _, tl = book.parse_txt(aozora.encode("utf-8"))
    assert tl == ["吾輩は猫である", "夏目漱石", "吾輩は猫である。", "名前はまだ無い。"], tl

    # cp932 fallback + BOM'd utf-8
    _, tl = book.parse_txt("猫である。".encode("cp932"))
    assert tl == ["猫である。"], tl
    _, tl = book.parse_txt("﻿猫である。".encode("utf-8"))
    assert tl == ["猫である。"], tl

    # --- html ------------------------------------------------------------ #
    _, hl = book.parse_html(CH1.encode("utf-8"))
    assert hl[0] == "吾輩は猫である。" and len(hl) == 4, hl

    # --- dispatch --------------------------------------------------------- #
    t, dl = book.parse_book(make_epub(), "misnamed.txt")   # zip magic wins over ext
    assert t == "吾輩は猫である" and len(dl) == 6
    _, dl = book.parse_book("猫。".encode("cp932"), "novel.TXT")
    assert dl == ["猫。"], dl
    _, dl = book.parse_book(CH2.encode("utf-8"), "page.HTML")
    assert dl == ["第二章", "どこで生れたかとんと見当がつかぬ。"], dl
    for name in ("book.pdf", "book.kfx", "book.xyz"):
        try:
            book.parse_book(b"\x00\x01binary", name)
            assert False, "expected ValueError for " + name
        except ValueError as e:
            assert "Calibre" in str(e)

    # --- fb2 (+ zipped fb2) ----------------------------------------------- #
    fb2 = """<?xml version="1.0" encoding="utf-8"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">
  <description><title-info><book-title>星の王子さま</book-title></title-info></description>
  <body>
    <section><title><p>第一章</p></title>
      <p>ぼくが六つのとき、</p><p>すばらしい絵を見た。</p>
      <empty-line/><p>  </p>
    </section>
  </body>
  <body name="notes"><section><p>注釈です。</p></section></body>
</FictionBook>"""
    t, fl = book.parse_fb2(fb2.encode("utf-8"))
    assert t == "星の王子さま", t
    assert fl == ["第一章", "ぼくが六つのとき、", "すばらしい絵を見た。", "注釈です。"], fl

    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as zf:
        zf.writestr("novel.fb2", fb2)
    t, fl = book.parse_book(zbuf.getvalue(), "novel.fb2.zip")   # PK magic, no epub inside
    assert t == "星の王子さま" and len(fl) == 4

    # --- PalmDOC LZ77 decompressor ---------------------------------------- #
    # literals a b c, back-reference dist=3 len=6, then 0xC1 = space + 'A'
    assert book._palmdoc_decompress(b"abc\x80\x1b\xc1") == b"abcabcabc A"
    assert book._palmdoc_decompress(b"\x02xy") == b"xy"          # literal run
    assert book._palmdoc_decompress(b"") == b""

    # --- mobi (PalmDB container) ------------------------------------------ #
    def make_mobi(html, compression=1, encryption=0, kind=b"BOOKMOBI", encoding=65001):
        recs = [html[i:i + 4096] for i in range(0, len(html), 4096)] or [b""]
        if kind == b"BOOKMOBI":
            # PalmDOC(16) + minimal MOBI header: magic, header_len 24, type, encoding
            r0 = struct.pack(">HHIHHHH", compression, 0, len(html), len(recs), 4096, encryption, 0)
            r0 += b"MOBI" + struct.pack(">II", 24, 2) + struct.pack(">I", encoding) + b"\0" * 8
        else:
            r0 = struct.pack(">HHIHHHH", compression, 0, len(html), len(recs), 4096, encryption, 0)
        records = [r0] + recs
        header = b"testbook".ljust(32, b"\0") + struct.pack(">HH", 0, 0) + b"\0" * 24
        header += kind[:4] + kind[4:] + struct.pack(">II", 0, 0) + struct.pack(">H", len(records))
        off = len(header) + 8 * len(records)
        info = b""
        for i, r in enumerate(records):
            info += struct.pack(">IBBH", off, 0, 0, i)
            off += len(r)
        return header + info + b"".join(records)

    html_jp = "<html><body><p>吾輩は猫である。</p><p>名前はまだ無い。</p></body></html>".encode("utf-8")
    t, ml = book.parse_mobi(make_mobi(html_jp))
    assert ml == ["吾輩は猫である。", "名前はまだ無い。"], ml

    # PalmDOC-compressed record (compression=2): compress = pass-through-safe
    # bytes only (all < 0x80 stay literal), so ASCII html works as-is
    html_en = b"<html><body><p>Hello book.</p></body></html>"
    t, ml = book.parse_mobi(make_mobi(html_en, compression=2, encoding=1252))
    assert ml == ["Hello book."], ml

    # DRM and HUFF refuse with a clear message; dispatch sniffs the magic
    for kwargs, msg in (({"encryption": 2}, "DRM"), ({"compression": 17480}, "HUFF")):
        try:
            book.parse_mobi(make_mobi(html_en, **kwargs))
            assert False, "expected ValueError"
        except ValueError as e:
            assert msg in str(e), (msg, str(e))
    _, ml = book.parse_book(make_mobi(html_jp), "whatever.azw")
    assert ml[0] == "吾輩は猫である。"

    print("test_book: all ok")


if __name__ == "__main__":
    main()
