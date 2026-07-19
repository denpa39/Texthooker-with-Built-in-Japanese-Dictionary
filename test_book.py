"""E-book parser units: epub spine order, ruby stripping, block splitting,
title; txt (Aozora markup, cp932); html; format dispatch.
Pure stdlib, no network, no dict.sqlite — runs in CI."""

import io
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
    for name in ("book.mobi", "book.azw3", "book.pdf"):
        try:
            book.parse_book(b"\x00\x01binary", name)
            assert False, "expected ValueError for " + name
        except ValueError as e:
            assert "Calibre" in str(e)

    print("test_book: all ok")


if __name__ == "__main__":
    main()
