"""Turns an uploaded file into plain text, plus the pictures inside it. Standard library only.

Supported: .txt .md .markdown .csv .json .html .htm .docx .epub .pdf
Pictures (in Word, EPUB and PDF files) are marked in the text with ⟦n⟧ and returned separately;
The plant looks at them later, in the background, and her descriptions replace the markers.
"""
from __future__ import annotations

import html
import io
import posixpath
import re
import zipfile
from html.parser import HTMLParser
from pathlib import PurePath
from urllib.parse import unquote

from .pdf import pdf_text
from .pictures import MAX_PICTURE_BYTES, MAX_SIDE, Collector, MARKER, size, sniff, unmark

MAX_UPLOAD_BYTES = 50_000_000  # big PDFs are mostly pictures, which are skipped
MAX_TEXT_CHARS = 2_050_000  # the library keeps 2,000,000, so there's no point reading past that
MAX_UNPACKED_BYTES = 60_000_000  # all the pages of an EPUB together, after unzipping
MAX_PICTURE_TOTAL = 200_000_000  # all the pictures in one Word file or EPUB together
TEXT_TYPES = {".txt", ".md", ".markdown", ".csv", ".json"}
HTML_TYPES = {".html", ".htm"}
PICTURE_TYPES = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
SUPPORTED = sorted(TEXT_TYPES | HTML_TYPES | {".docx", ".epub", ".pdf"})
HELP = ("Your plant can read " + ", ".join(SUPPORTED) + " files, and look at " + ", ".join(sorted(PICTURE_TYPES))
        + " pictures. Export other documents as text, Markdown or Word first.")


def is_picture(name: str) -> bool:
    return PurePath(name or "").suffix.lower() in PICTURE_TYPES | {".heic", ".heif"}


def check_picture(name: str, raw: bytes) -> str:
    """Checks a picture someone is sending her. Returns its media type, or raises ValueError with a message fit
    to show the user."""
    if PurePath(name or "").suffix.lower() in (".heic", ".heif"):
        raise ValueError("That photo is in HEIC format, which your plant can't open. On an iPhone, share it as a JPEG "
                         "(or take a screenshot of it) and send that instead.")
    mime = sniff(raw)
    if not mime:
        raise ValueError("That doesn't look like a JPEG, PNG, GIF or WebP picture. Save it as a JPEG and try again.")
    if len(raw) > MAX_PICTURE_BYTES:
        raise ValueError("That picture is too big to show your plant (the limit is 7 MB). Send a smaller copy or a screenshot.")
    dims = size(raw, mime)
    if dims and max(dims) > MAX_SIDE:
        raise ValueError(f"That picture is too large (over {MAX_SIDE:,} pixels on a side). Send a smaller copy.")
    return mime


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


class _Strip(HTMLParser):
    def __init__(self, hidden=("script", "style"), picture=None) -> None:
        super().__init__()
        self.out: list = []
        self.skip = 0
        self.hidden = hidden
        self.picture = picture  # called with a picture's address; returns its marker, or ""

    def handle_starttag(self, tag, attrs):
        if self.picture and not self.skip and tag in ("img", "image"):
            a = {k.split(":")[-1].lower(): v for k, v in attrs if v}
            mark = self.picture(a.get("src") or a.get("href") or "")
            if mark:
                self.out.append(f"\n{mark}\n")
        if tag in self.hidden:
            self.skip += 1
        elif tag in ("p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"):
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.hidden and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.out.append(unmark(data))


class _ZipPictures:
    """Reads pictures out of a zip file (Word or EPUB) within a size budget."""

    def __init__(self, z: zipfile.ZipFile, pictures: Collector) -> None:
        self.z, self.pictures, self.used = z, pictures, 0
        self.names = set(z.namelist())

    def add(self, path: str) -> str:
        if path not in self.names:
            return ""
        size = self.z.getinfo(path).file_size
        if self.used + size > MAX_PICTURE_TOTAL:
            return self.pictures.skip("over the size limit for one document")
        self.used += size
        return self.pictures.add(self.z.read(path))


def _docx_text(raw: bytes, pictures: Collector) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            info = z.getinfo("word/document.xml")
            if info.file_size > 20_000_000:
                raise ValueError("That Word file is too large to read.")
            xml = unmark(z.read("word/document.xml").decode("utf-8", errors="replace"))
            try:
                rels = z.read("word/_rels/document.xml.rels").decode("utf-8", errors="replace")
            except KeyError:
                rels = ""
            targets = {}
            for tag in re.findall(r"<Relationship\b[^>]*>", rels):
                a = _attrs(tag)
                if a.get("id") and a.get("target") and a.get("targetmode", "").lower() != "external":
                    targets[a["id"]] = posixpath.normpath(posixpath.join("word", unquote(a["target"])))
            found = _ZipPictures(z, pictures)

            def picture(m):
                mark = found.add(targets.get(m.group(1), ""))
                return f"\n{mark}\n" if mark else ""
            xml = re.sub(r'<(?:a:blip|v:imagedata)\b[^>]*?\br:(?:embed|id)="([^"]+)"[^>]*>', picture, xml)
    except (zipfile.BadZipFile, KeyError):
        raise ValueError("That doesn't look like a valid .docx file.")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab\s*/>", "\t", xml)
    xml = re.sub(r"<w:br\s*/>", "\n", xml)
    return html.unescape(re.sub(r"<[^>]+>", "", xml))


_ATTR = re.compile(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_PAGE_TYPES = ("application/xhtml+xml", "text/html")


def _attrs(tag: str) -> dict:
    return {k.split(":")[-1].lower(): html.unescape(a or b) for k, a, b in _ATTR.findall(tag)}


def _epub_text(raw: bytes, pictures: Collector) -> str:
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise ValueError("That doesn't look like a valid EPUB file.")
    with z:
        names = set(z.namelist())
        used = 0

        def read(name: str) -> str:
            nonlocal used
            used += z.getinfo(name).file_size
            if used > MAX_UNPACKED_BYTES:
                raise ValueError("That book is too big to unpack. Try a shorter one.")
            return _decode(z.read(name))

        if "META-INF/encryption.xml" in names:
            locked = re.findall(r'URI\s*=\s*"([^"]+)"', read("META-INF/encryption.xml"))
            if any(u.lower().endswith((".xhtml", ".html", ".htm", ".xml")) for u in locked):
                raise ValueError("That book is copy-protected (DRM), so your plant can't read it. "
                                 "Try a DRM-free copy, or export it as text first.")
        opf = None
        if "META-INF/container.xml" in names:
            m = re.search(r'full-path\s*=\s*"([^"]+)"', read("META-INF/container.xml"))
            opf = m and unquote(m.group(1))
        if opf not in names:
            opf = next((n for n in sorted(names) if n.lower().endswith(".opf")), None)
        pages = []
        if opf:
            package = read(opf)
            folder = posixpath.dirname(opf)
            manifest = {}
            for tag in re.findall(r"<(?:\w+:)?item\b[^>]*>", package):
                a = _attrs(tag)
                if a.get("id") and a.get("href"):
                    manifest[a["id"]] = (posixpath.normpath(posixpath.join(folder, unquote(a["href"]))), a.get("media-type", ""))
            spine = [_attrs(t).get("idref") for t in re.findall(r"<(?:\w+:)?itemref\b[^>]*>", package)]
            pages = [manifest[i][0] for i in spine if i in manifest]
            if not pages:
                pages = [path for path, kind in manifest.values() if kind in _PAGE_TYPES]
        if not pages:
            pages = sorted(n for n in names if n.lower().endswith((".xhtml", ".html", ".htm")))
        out, size = [], 0
        found = _ZipPictures(z, pictures)
        for name in dict.fromkeys(pages):
            if name not in names or not name.lower().endswith((".xhtml", ".html", ".htm", ".xml")):
                continue

            def picture(src, here=posixpath.dirname(name)):
                src = unquote(src.split("#")[0].split("?")[0])
                return found.add(posixpath.normpath(posixpath.join(here, src))) if src and ":" not in src else ""
            p = _Strip(hidden=("script", "style", "head"), picture=picture)
            p.feed(read(name))
            out.append("".join(p.out))
            size += len(out[-1])
            if size > MAX_TEXT_CHARS:
                break
    return "\n\n".join(out)


def extract_text(name: str, raw: bytes) -> str:
    """Returns the document's text (with picture markers), or raises ValueError with a message fit to show the user."""
    return extract_document(name, raw)[0]


def extract_document(name: str, raw: bytes):
    """Returns (text with ⟦n⟧ picture markers, pictures.Collector), or raises ValueError with a message
    fit to show the user."""
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError("That file is too big. The limit is 50 MB.")
    ext = PurePath(name or "").suffix.lower()
    pictures = Collector()
    if ext in TEXT_TYPES:
        text = unmark(_decode(raw))
    elif ext in HTML_TYPES:
        p = _Strip()
        p.feed(_decode(raw))
        text = "".join(p.out)
    elif ext == ".docx":
        text = _docx_text(raw, pictures)
    elif ext in (".epub", ".pdf"):
        try:
            text = _epub_text(raw, pictures) if ext == ".epub" else pdf_text(raw, MAX_TEXT_CHARS, pictures)
        except ValueError:
            raise
        except Exception:  # a damaged file shouldn't break the page
            raise ValueError(f"That {ext[1:].upper()} file couldn't be read. It may be damaged. Try saving it again.")
    else:
        raise ValueError(HELP)
    text = re.sub(r"[ \t]+\n", "\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if "\x00" in text:
        raise ValueError(HELP)
    if not MARKER.sub("", text).strip() and not pictures.pictures:
        if pictures.skipped:
            reasons = ", ".join(pictures.skipped)
            raise ValueError(f"That file is all pictures, and they're {reasons}. If it's a scanned book, try "
                             "scanning or saving it again in colour or greyscale (JPEG), then send it again.")
        raise ValueError("I couldn't find any text or pictures in that file.")
    return text, pictures
