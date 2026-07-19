"""E-book → plain text lines for the reader. Pure stdlib.

parse_book(bytes, filename) dispatches on extension (zip magic rescues a
misnamed epub): .epub follows the OPF spine order; .html/.xhtml is one
document; .txt handles Aozora Bunko markup (｜base《ruby》 ruby, ［＃…］
editorial notes, the ---- 記号 block, the 底本： footer) and falls back from
UTF-8 to cp932. All markup strippers drop ruby readings — the reader adds
its own furigana from kuromoji, baked-in readings would double up. One block
element / text line = one reader line, like one VN textbox advance.
"""

import io
import posixpath
import re
import struct
import zipfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import unquote

# Elements whose boundaries end a reader line.
_BLOCK = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "br",
          "blockquote", "section", "article", "td", "tr"}
# Elements whose text must never appear: rt/rp are ruby furigana.
_SKIP = {"rt", "rp", "script", "style", "head", "title"}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines = []
        self._buf = []
        self._skip = 0

    def _flush(self):
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        self._buf.clear()
        if text:
            self.lines.append(text)

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self._skip += 1
        elif tag in _BLOCK:
            self._flush()

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK:
            self._flush()

    def handle_data(self, data):
        if not self._skip:
            self._buf.append(data)

    def close(self):
        super().close()
        self._flush()


def parse_epub(data):
    """Returns (title, lines). Raises ValueError on anything that isn't an epub."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        container = ET.fromstring(zf.read("META-INF/container.xml"))
        opf_path = container.find(".//{*}rootfile").get("full-path")
        opf = ET.fromstring(zf.read(opf_path))
    except (zipfile.BadZipFile, KeyError, ET.ParseError, AttributeError) as e:
        raise ValueError("not an epub: " + str(e))

    opf_dir = posixpath.dirname(opf_path)
    title_el = opf.find(".//{*}title")
    title = (title_el.text or "").strip() if title_el is not None else ""

    manifest = {}   # id -> (href, media-type)
    man_el = opf.find("{*}manifest")
    for item in (man_el if man_el is not None else []):
        manifest[item.get("id")] = (item.get("href") or "", item.get("media-type") or "")

    lines = []
    spine = opf.find("{*}spine")
    for itemref in (spine if spine is not None else []):
        href, mtype = manifest.get(itemref.get("idref"), ("", ""))
        if not href:
            continue
        # Only text documents — the spine can reference images or the ncx.
        if "html" not in mtype and not re.search(r"\.x?html?$", href, re.I):
            continue
        path = posixpath.normpath(posixpath.join(opf_dir, unquote(href)))
        try:
            raw = zf.read(path)
        except KeyError:
            continue
        p = _TextExtractor()
        p.feed(raw.decode("utf-8", "replace"))
        p.close()
        lines.extend(p.lines)
    return title, lines


def parse_html(data):
    """A single .html/.xhtml document."""
    p = _TextExtractor()
    p.feed(_decode(data))
    p.close()
    return "", p.lines


# Aozora Bunko plain-text markup: ｜漢字《かんじ》 (explicit-base ruby),
# 漢字《かんじ》 (ruby on the preceding run), ［＃…］ editorial notes.
_AOZORA_BASE = re.compile(r"[｜|]([^《》｜|]*)《[^》]*》")
_AOZORA_RUBY = re.compile(r"《[^》]*》")
_AOZORA_NOTE = re.compile(r"［＃[^］]*］")


def _decode(data):
    """UTF-8 (BOM-aware) first, then cp932 — the two encodings JP text files
    actually come in. Last resort: UTF-8 with replacement chars."""
    for enc in ("utf-8-sig", "cp932"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def parse_txt(data):
    """Plain text, one non-empty line per reader line. Aozora Bunko extras:
    the ---- delimited 記号について block and the 底本： footer are cut,
    ruby/notes stripped."""
    text = _AOZORA_NOTE.sub("", _AOZORA_RUBY.sub("", _AOZORA_BASE.sub(r"\1", _decode(data))))
    lines, skipping = [], False
    for ln in text.splitlines():
        ln = ln.strip()
        if ln.startswith("----"):
            skipping = not skipping
            continue
        if ln.startswith("底本："):
            break
        if ln and not skipping:
            lines.append(ln)
    return "", lines


def parse_fb2(data):
    """FictionBook 2: plain XML. <p>/<v>/<subtitle> under each <body>, in
    document order (section titles are <title><p>…, so they come along)."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ValueError("not an FB2: " + str(e))
    title_el = root.find(".//{*}book-title")
    title = (title_el.text or "").strip() if title_el is not None else ""
    lines = []
    for body in root.findall("{*}body"):
        for el in body.iter():
            if el.tag.rsplit("}", 1)[-1] in ("p", "v", "subtitle"):
                t = re.sub(r"\s+", " ", "".join(el.itertext())).strip()
                if t:
                    lines.append(t)
    return title, lines


# ---- MOBI / PalmDOC (.mobi .prc .pdb .azw .azw3) --------------------------- #

def _palmdoc_decompress(data):
    """PalmDOC LZ77: literals, 0x80-0xBF back-references, 0xC0+ space+char."""
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        b = data[i]; i += 1
        if b == 0 or 0x09 <= b <= 0x7F:
            out.append(b)
        elif b <= 0x08:                     # 1-8 literal bytes follow
            out += data[i:i + b]; i += b
        elif b <= 0xBF:                     # 2-byte back-reference
            if i >= n:
                break
            pair = (b << 8) | data[i]; i += 1
            dist, length = (pair >> 3) & 0x7FF, (pair & 7) + 3
            if not dist or dist > len(out):
                continue                     # corrupt pair — skip, keep going
            for _ in range(length):
                out.append(out[-dist])
        else:                               # 0xC0+: space + ASCII char
            out += b" "; out.append(b ^ 0x80)
    return bytes(out)


def _trailing_size(rec):
    """Size of one trailing data entry: variable-width int in the record's last
    bytes, high bit marks the first byte (mobiunpack's algorithm)."""
    num = 0
    for b in rec[-4:]:
        if b & 0x80:
            num = 0
        num = (num << 7) | (b & 0x7F)
    return num


def _trim_extra(rec, flags):
    """Strip MOBI per-record trailing entries: one sized entry per set flag bit
    above bit 0; bit 0 is the multibyte-overlap entry (count in the last byte)."""
    for _ in range(bin(flags >> 1).count("1")):
        v = _trailing_size(rec)
        if 0 < v <= len(rec):
            rec = rec[:-v]
    if flags & 1 and rec:
        rec = rec[:-((rec[-1] & 3) + 1)]
    return rec


def parse_mobi(data):
    """Mobipocket / plain PalmDOC. Handles no-compression and PalmDOC-LZ77
    books (the overwhelming majority); DRM and HUFF/CDIC get a clear error."""
    if len(data) < 78 or data[60:68] not in (b"BOOKMOBI", b"TEXtREAd"):
        raise ValueError("not a MOBI/PalmDOC file")
    num_recs = struct.unpack(">H", data[76:78])[0]
    offs = [struct.unpack(">I", data[78 + 8 * i:82 + 8 * i])[0] for i in range(num_recs)]
    offs.append(len(data))
    rec = lambda i: data[offs[i]:offs[i + 1]]
    r0 = rec(0)
    compression, = struct.unpack(">H", r0[0:2])
    rec_count, = struct.unpack(">H", r0[8:10])
    encryption, = struct.unpack(">H", r0[12:14])
    if encryption:
        raise ValueError("this book is DRM-protected — remove the DRM or convert it to epub with Calibre")
    if compression == 17480:
        raise ValueError("Kindle HUFF/CDIC compression isn't supported — convert to epub with Calibre")
    if compression not in (1, 2):
        raise ValueError(f"unknown MOBI compression ({compression})")

    title, codec, extra_flags = "", "cp1252", 0
    if r0[16:20] == b"MOBI":
        header_len, = struct.unpack(">I", r0[20:24])
        enc, = struct.unpack(">I", r0[28:32])
        codec = "utf-8" if enc == 65001 else "cp1252"
        if header_len >= 0xE4 and len(r0) >= 16 + 0xE4:
            extra_flags, = struct.unpack(">H", r0[16 + 0xE2:16 + 0xE4])
        if header_len >= 92:   # full-name offset/length live at header offset 84/88
            toff, tlen = struct.unpack(">II", r0[16 + 84:16 + 92])
            if 0 < tlen and toff + tlen <= len(r0):
                title = r0[toff:toff + tlen].decode(codec, "replace").strip()
    else:
        title = data[:32].split(b"\0", 1)[0].decode("cp1252", "replace").strip()

    parts = []
    for i in range(1, min(rec_count, num_recs - 1) + 1):
        r = _trim_extra(rec(i), extra_flags)
        parts.append(_palmdoc_decompress(r) if compression == 2 else r)
    html = b"".join(parts).decode(codec, "replace")
    p = _TextExtractor()
    p.feed(html)
    p.close()
    if not p.lines:
        raise ValueError("no text found in this MOBI")
    return title, p.lines


def parse_book(data, filename):
    """Dispatch by content magic first, then extension.
    Raises ValueError on unsupported or unparseable input."""
    ext = (filename or "").lower().rsplit(".", 1)[-1] if "." in (filename or "") else ""
    if data[:2] == b"PK":                    # epub, or a zipped .fb2
        try:
            return parse_epub(data)
        except ValueError:
            try:
                zf = zipfile.ZipFile(io.BytesIO(data))
                fb2 = next(n for n in zf.namelist() if n.lower().endswith(".fb2"))
                return parse_fb2(zf.read(fb2))
            except (zipfile.BadZipFile, StopIteration, KeyError):
                raise ValueError("zip archive with no epub structure or .fb2 inside")
    if len(data) >= 68 and data[60:68] in (b"BOOKMOBI", b"TEXtREAd"):
        return parse_mobi(data)
    if ext == "fb2":
        return parse_fb2(data)
    if ext in ("html", "htm", "xhtml", "xht"):
        return parse_html(data)
    if ext in ("txt", "text", ""):
        return parse_txt(data)
    if ext in ("pdf", "kfx"):
        raise ValueError(f".{ext} isn't supported — convert it to epub with Calibre")
    raise ValueError(f".{ext} isn't supported — use .epub, .mobi/.azw, .fb2, .txt or .html "
                     "(Calibre converts anything else to epub)")
