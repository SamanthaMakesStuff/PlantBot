"""Pictures found inside documents. Standard library only.

The plant can't shrink or convert pictures without an extra library, so this only keeps the ones a brain
can open as they are (JPEG, PNG, GIF, WebP), checks their size, and can wrap raw PDF picture data
into a PNG. Everything else is counted as skipped, with a reason her human can read.
"""
from __future__ import annotations

import re
import struct
import zlib

MAX_PICTURE_BYTES = 7_000_000   # the API takes 10 MB once base64 has made it a third bigger
MAX_SIDE = 8000                 # the API's limit, in pixels
MIN_SIDE = 48                   # smaller than this is a bullet, an icon or a rule, not a picture
MAX_PICTURES = 1000             # per document

MARKER = re.compile("⟦(\\d+)⟧")  # where a picture sat in the text: ⟦12⟧


def marker(n: int) -> str:
    return f"⟦{n}⟧"


def unmark(text: str) -> str:
    """Stops a document's own text from pretending to be a picture marker."""
    return text.replace("⟦", "[").replace("⟧", "]")


def sniff(data: bytes) -> str:
    """The picture's media type from its first bytes, or "" if it isn't one a brain can open."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def size(data: bytes, mime: str):
    """(width, height) in pixels, or None if it can't be worked out."""
    try:
        if mime == "image/png":
            return struct.unpack(">II", data[16:24])
        if mime == "image/gif":
            return struct.unpack("<HH", data[6:10])
        if mime == "image/webp":
            kind = data[12:16]
            if kind == b"VP8X":
                return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
            if kind == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30])
                return w & 0x3FFF, h & 0x3FFF
            if kind == b"VP8L":
                b = int.from_bytes(data[21:25], "little")
                return (b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1
            return None
        if mime == "image/jpeg":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                m = data[i + 1]
                if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7 or m == 0xFF:
                    i += 1 if m == 0xFF else 2
                    continue
                if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    except struct.error:
        return None
    return None


def png(width: int, height: int, depth: int, colour: int, idat: bytes, palette: bytes = b"") -> bytes:
    """A PNG file around already-compressed, already-filtered rows."""
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    head = struct.pack(">IIBBBBB", width, height, depth, colour, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", head) + (chunk(b"PLTE", palette) if palette else b"")
            + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


class Collector:
    """Gathers a document's pictures as it is read. add() returns the marker to put in the text."""

    def __init__(self) -> None:
        self.pictures: list = []   # {"kind", "mime", "data"}; marker n is position n-1
        self.skipped: dict = {}    # reason -> how many
        self._seen: dict = {}      # the same picture again (a logo on every page) gets the same marker

    def skip(self, reason: str) -> str:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1
        return ""

    def add(self, data: bytes, kind: str = "picture", mime: str = "", dims=None) -> str:
        mime = mime or sniff(data)
        if not mime:
            return self.skip("in a format the plant can't open")
        dims = dims or size(data, mime)
        if dims and min(dims) < MIN_SIDE:
            return ""  # decoration, not worth mentioning
        if not dims or max(dims) > MAX_SIDE or len(data) > MAX_PICTURE_BYTES:
            return self.skip("too big to send")
        key = (len(data), zlib.crc32(data))
        if key in self._seen:
            return marker(self._seen[key])
        if len(self.pictures) >= MAX_PICTURES:
            return self.skip("over the limit of 1,000 per document")
        self.pictures.append({"kind": kind, "mime": mime, "data": data})
        self._seen[key] = len(self.pictures)
        return marker(len(self.pictures))

    def make_page(self, m: str) -> None:
        """Marks a picture as a scanned page, whose words she should write out rather than describe."""
        found = MARKER.fullmatch(m)
        if found:
            self.pictures[int(found.group(1)) - 1]["kind"] = "page"
