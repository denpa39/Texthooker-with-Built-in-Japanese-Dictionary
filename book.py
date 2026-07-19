"""EPUB → plain text lines for the reader. Pure stdlib.

parse_epub(bytes) -> (title, lines): follows the OPF spine order, strips markup
with html.parser, and drops <rt>/<rp> so ruby furigana never leaks into the
text (the reader adds its own furigana from kuromoji). One block element
(<p>, <h1>… <br>) = one reader line, like one VN textbox advance.
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
