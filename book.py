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


def parse_book(data, filename):
    """Dispatch by extension; zip magic rescues a misnamed epub.
    Raises ValueError on unsupported or unparseable input."""
    ext = (filename or "").lower().rsplit(".", 1)[-1] if "." in (filename or "") else ""
    if ext == "epub" or data[:2] == b"PK":
        return parse_epub(data)
    if ext in ("html", "htm", "xhtml", "xht"):
        return parse_html(data)
    if ext in ("txt", "text", ""):
        return parse_txt(data)
    raise ValueError(f".{ext} isn't supported — use .epub, .txt or .html "
                     "(Calibre converts .mobi/.azw/.pdf to epub)")
