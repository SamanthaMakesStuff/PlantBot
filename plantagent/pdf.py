"""Pulls the typed text out of a PDF. Standard library only.

This is a best effort, not a full PDF reader. It handles the PDFs that Word,
Google Docs, LibreOffice and "Save as PDF" in a browser make. It can't read
scanned pages (they are pictures of text) or password-protected files.

Everything here treats the file as hostile: sizes, depths and counts are all
capped, and any surprise becomes a ValueError with a message fit for the user.
"""
from __future__ import annotations

import base64
import re
import unicodedata
import zlib
from math import hypot

from .pictures import MIN_SIDE, png, unmark

MAX_STREAM_BYTES = 20_000_000   # one stream, after unpacking
MAX_TOTAL_BYTES = 150_000_000   # all streams together, after unpacking
MAX_OBJECTS = 300_000
MAX_DEPTH = 60                  # nesting of arrays and dictionaries
MAX_FORM_DEPTH = 6              # forms drawn inside forms

LOCKED = ("That PDF is locked or protected, so your plant can't read it. "
          "Open it, print it to a new PDF (or export it as Word), and send that instead.")
NOT_PDF = "That doesn't look like a valid PDF file."


class _Stop(Exception):
    """Raised to stop reading early, keeping the text found so far."""


class Name(str):
    pass


class Kw(str):
    pass


class Ref(tuple):
    pass


class Stream:
    __slots__ = ("dict", "raw")

    def __init__(self, d: dict, raw: bytes) -> None:
        self.dict, self.raw = d, raw


_END_ARRAY, _END_DICT, _EOF = object(), object(), object()
_DELIM = rb"\x00\t\n\x0c\r ()<>\[\]{}/%"
_WS = re.compile(rb"(?:[\x00\t\n\x0c\r ]+|%[^\r\n]*)+")
_REGULAR = re.compile(rb"[^" + _DELIM + rb"]+")
_NAME = re.compile(rb"/[^" + _DELIM + rb"]*")
_REF = re.compile(rb"[\x00\t\n\x0c\r ]+(\d+)[\x00\t\n\x0c\r ]+R(?![^" + _DELIM + rb"])")
_NUMBER = re.compile(rb"[+-]?(?:\d+\.?\d*|\.\d+)\Z")
_LIT_RUN = re.compile(rb"[^()\\]+")
_OCTAL = re.compile(rb"[0-7]{1,3}")
_HEX_JUNK = re.compile(rb"[^0-9A-Fa-f]")
_ESCAPES = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f", b"(": b"(", b")": b")", b"\\": b"\\"}
_INLINE_IMAGE_END = re.compile(rb"[\x00\t\n\x0c\r ]EI(?![^" + _DELIM + rb"])")


def _hex(raw: bytes) -> bytes:
    raw = _HEX_JUNK.sub(b"", raw)
    return bytes.fromhex((raw + b"0" if len(raw) % 2 else raw).decode())


def _literal(data: bytes, pos: int):
    out, depth, n = bytearray(), 1, len(data)
    while pos < n:
        m = _LIT_RUN.match(data, pos)
        if m:
            out += m.group()
            pos = m.end()
            continue
        c = data[pos]
        pos += 1
        if c == 0x28:
            depth += 1
            out.append(c)
        elif c == 0x29:
            depth -= 1
            if depth == 0:
                break
            out.append(c)
        else:
            e = data[pos:pos + 1]
            if e in _ESCAPES:
                out += _ESCAPES[e]
                pos += 1
            elif e in (b"\r", b"\n"):
                pos += 2 if data[pos:pos + 2] == b"\r\n" else 1
            elif (m := _OCTAL.match(data, pos)):
                out.append(int(m.group(), 8) & 255)
                pos = m.end()
            else:
                out += e
                pos += 1
    return bytes(out), pos


def parse(data: bytes, pos: int, depth: int = 0):
    """Reads one PDF value starting at pos. Returns (value, position after it)."""
    if depth > MAX_DEPTH:
        raise ValueError(NOT_PDF)
    m = _WS.match(data, pos)
    if m:
        pos = m.end()
    if pos >= len(data):
        return _EOF, pos
    c = data[pos]
    if c == 0x2F:  # /
        m = _NAME.match(data, pos)
        name = re.sub(rb"#([0-9A-Fa-f]{2})", lambda h: bytes([int(h.group(1), 16)]), m.group()[1:])
        return Name(name.decode("latin-1")), m.end()
    if c == 0x28:  # (
        return _literal(data, pos + 1)
    if c == 0x3C:  # <
        if data[pos + 1:pos + 2] == b"<":
            d, pos = {}, pos + 2
            while True:
                key, pos = parse(data, pos, depth + 1)
                if key is _END_DICT or key is _EOF:
                    return d, pos
                if not isinstance(key, Name):
                    continue
                value, pos = parse(data, pos, depth + 1)
                if value is _END_DICT or value is _EOF:
                    d[key] = None
                    return d, pos
                d[key] = value
        end = data.find(b">", pos)
        end = len(data) if end < 0 else end
        try:
            return _hex(data[pos + 1:end]), end + 1
        except ValueError:
            return b"", end + 1
    if c == 0x3E:  # >
        if data[pos + 1:pos + 2] == b">":
            return _END_DICT, pos + 2
        return Kw(">"), pos + 1
    if c == 0x5B:  # [
        items, pos = [], pos + 1
        while True:
            value, pos = parse(data, pos, depth + 1)
            if value is _END_ARRAY or value is _EOF:
                return items, pos
            if value is not _END_DICT:
                items.append(value)
    if c == 0x5D:  # ]
        return _END_ARRAY, pos + 1
    m = _REGULAR.match(data, pos)
    if not m:  # a stray ) { or }
        return Kw(chr(c)), pos + 1
    tok, pos = m.group(), m.end()
    if _NUMBER.match(tok):
        if b"." not in tok:
            num = int(tok)
            r = _REF.match(data, pos)
            if r and num >= 0:
                return Ref((num, int(r.group(1)))), r.end()
            return num, pos
        return float(tok), pos
    if tok == b"true":
        return True, pos
    if tok == b"false":
        return False, pos
    if tok == b"null":
        return None, pos
    return Kw(tok.decode("latin-1")), pos


# ------------------------------------------------------------------ the file

_OBJ = re.compile(rb"(\d+)[\x00\t\n\x0c\r ]+(\d+)[\x00\t\n\x0c\r ]+obj(?![^" + _DELIM + rb"])")


class _Doc:
    def __init__(self, data: bytes) -> None:
        if b"%PDF-" not in data[:1024]:
            raise ValueError(NOT_PDF)
        self.data = data
        self.objects: dict = {}
        self.unpacked = 0
        trailers = self._scan()
        for t in trailers:
            if t.get("Encrypt") is not None:
                raise ValueError(LOCKED)
        self._unpack_object_streams()
        self.root = None
        for t in trailers:
            if isinstance(self.get(t.get("Root")), dict):
                self.root = self.get(t.get("Root"))
        if self.root is None:
            for value in self.objects.values():
                if isinstance(value, dict) and value.get("Type") == "Catalog":
                    self.root = value

    def _scan(self) -> list:
        """Finds every 'n 0 obj' in the file. Later copies win, as they do in an edited PDF."""
        data, pos, trailers = self.data, 0, []
        while len(self.objects) < MAX_OBJECTS:
            m = _OBJ.search(data, pos)
            if not m:
                break
            pos = m.end()
            try:
                value, after = parse(data, pos)
            except (ValueError, RecursionError):
                continue
            if isinstance(value, dict):
                ws = _WS.match(data, after)
                at = ws.end() if ws else after
                if data.startswith(b"stream", at):
                    value, after = self._stream(value, at + 6)
                    if value.dict.get("Type") == "XRef":
                        trailers.append(value.dict)
            self.objects[int(m.group(1))] = value
            pos = max(pos, after)
        for m in re.finditer(rb"trailer[\x00\t\n\x0c\r ]*<<", data):
            try:
                value, _ = parse(data, m.end() - 2)
            except (ValueError, RecursionError):
                continue
            if isinstance(value, dict):
                trailers.append(value)
        return trailers

    def _stream(self, d: dict, start: int):
        data = self.data
        if data[start:start + 2] == b"\r\n":
            start += 2
        elif data[start:start + 1] in (b"\n", b"\r"):
            start += 1
        length = d.get("Length")
        if isinstance(length, int) and 0 <= length <= len(data) - start:
            ws = _WS.match(data, start + length)
            if data.startswith(b"endstream", ws.end() if ws else start + length):
                return Stream(d, data[start:start + length]), start + length
        end = data.find(b"endstream", start)
        end = len(data) if end < 0 else end
        stop = end
        if data[stop - 2:stop] == b"\r\n":
            stop -= 2
        elif data[stop - 1:stop] in (b"\n", b"\r"):
            stop -= 1
        return Stream(d, data[start:max(start, stop)]), end

    def _unpack_object_streams(self) -> None:
        for value in list(self.objects.values()):
            if not (isinstance(value, Stream) and value.dict.get("Type") == "ObjStm"):
                continue
            body = self.decode(value)
            first, count = value.dict.get("First"), value.dict.get("N")
            if not body or not isinstance(first, int) or not isinstance(count, int):
                continue
            header, pos = [], 0
            try:
                for _ in range(min(count, MAX_OBJECTS) * 2):
                    n, pos = parse(body[:first], pos)
                    if not isinstance(n, int):
                        break
                    header.append(n)
                for i in range(0, len(header) - 1, 2):
                    if header[i] not in self.objects and len(self.objects) < MAX_OBJECTS:
                        self.objects[header[i]] = parse(body, first + header[i + 1])[0]
            except (ValueError, RecursionError):
                continue

    def get(self, value):
        """Follows references to the real value."""
        for _ in range(32):
            if not isinstance(value, Ref):
                return value
            value = self.objects.get(value[0])
        return None

    def decode(self, stream) -> bytes | None:
        """Unpacks a stream. None if it uses a filter we don't need (pictures) or can't read."""
        stream = self.get(stream)
        if not isinstance(stream, Stream):
            return None
        filters = self.get(stream.dict.get("Filter"))
        filters = filters if isinstance(filters, list) else [filters] if filters else []
        data = stream.raw
        for f in filters:
            f = self.get(f)
            if f in ("FlateDecode", "Fl"):
                data = _inflate(data)
            elif f in ("ASCIIHexDecode", "AHx"):
                end = data.find(b">")
                data = _hex(data if end < 0 else data[:end])
            elif f in ("ASCII85Decode", "A85"):
                data = re.sub(rb"[\x00\t\n\x0c\r ]", b"", data)
                data = data[2:] if data.startswith(b"<~") else data
                end = data.find(b"~>")
                try:
                    data = base64.a85decode(data if end < 0 else data[:end])
                except ValueError:
                    return None
            else:
                return None
            if data is None:
                return None
        self.unpacked += len(data)
        if self.unpacked > MAX_TOTAL_BYTES:
            raise _Stop
        return data


def _inflate(raw: bytes) -> bytes | None:
    """zlib, with a size cap. A damaged stream gives back whatever came out before the damage."""
    d, out = zlib.decompressobj(), bytearray()
    try:
        for i in range(0, len(raw), 65536):
            out += d.decompress(raw[i:i + 65536], MAX_STREAM_BYTES + 1 - len(out))
            if len(out) > MAX_STREAM_BYTES or d.unconsumed_tail:
                raise _Stop
    except zlib.error:
        pass
    return bytes(out) if out else None


# ----------------------------------------------------------------- fonts

_GLYPHS = {
    "space": " ", "exclam": "!", "quotedbl": '"', "numbersign": "#", "dollar": "$", "percent": "%",
    "ampersand": "&", "quotesingle": "'", "parenleft": "(", "parenright": ")", "asterisk": "*",
    "plus": "+", "comma": ",", "hyphen": "-", "period": ".", "slash": "/", "colon": ":",
    "semicolon": ";", "less": "<", "equal": "=", "greater": ">", "question": "?", "at": "@",
    "bracketleft": "[", "backslash": "\\", "bracketright": "]", "asciicircum": "^",
    "underscore": "_", "grave": "`", "braceleft": "{", "bar": "|", "braceright": "}",
    "asciitilde": "~", "quoteleft": "‘", "quoteright": "’", "quotedblleft": "“",
    "quotedblright": "”", "quotesinglbase": "‚", "quotedblbase": "„",
    "endash": "–", "emdash": "—", "bullet": "•", "ellipsis": "…",
    "fi": "fi", "fl": "fl", "ff": "ff", "ffi": "ffi", "ffl": "ffl", "sterling": "£",
    "Euro": "€", "copyright": "©", "registered": "®", "trademark": "™",
    "degree": "°", "minus": "−", "nbspace": " ", "periodcentered": "·",
    "germandbls": "ß", "ae": "æ", "AE": "Æ", "oe": "œ", "OE": "Œ",
    "oslash": "ø", "Oslash": "Ø", "dotlessi": "ı", "section": "§",
    "paragraph": "¶", "dagger": "†", "daggerdbl": "‡", "cent": "¢",
    "yen": "¥", "multiply": "×", "divide": "÷", "plusminus": "±",
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9",
}
_ACCENTS = {"acute": "ACUTE", "grave": "GRAVE", "circumflex": "CIRCUMFLEX", "dieresis": "DIAERESIS",
            "tilde": "TILDE", "cedilla": "CEDILLA", "ring": "RING ABOVE", "caron": "CARON"}


def _glyph(name: str) -> str | None:
    if name in _GLYPHS:
        return _GLYPHS[name]
    if len(name) == 1:
        return name
    m = re.fullmatch(r"uni([0-9A-Fa-f]{4})|u([0-9A-Fa-f]{4,6})", name)
    if m:
        code = int(m.group(1) or m.group(2), 16)
        return chr(code) if code < 0x110000 and not 0xD800 <= code < 0xE000 else None
    m = re.fullmatch(r"([A-Za-z])(acute|grave|circumflex|dieresis|tilde|cedilla|ring|caron)", name)
    if m:
        size = "CAPITAL" if m.group(1).isupper() else "SMALL"
        try:
            return unicodedata.lookup(f"LATIN {size} LETTER {m.group(1).upper()} WITH {_ACCENTS[m.group(2)]}")
        except KeyError:
            return None
    return None


def _utf16(raw: bytes) -> str:
    try:
        raw = _hex(raw)
    except ValueError:
        return ""
    if len(raw) % 2:
        raw = b"\x00" + raw
    return raw.decode("utf-16-be", errors="ignore")


def _cmap(data: bytes) -> dict:
    """Reads a ToUnicode map: character code -> text."""
    out: dict = {}
    for block in re.finditer(rb"beginbfchar(.*?)endbfchar", data, re.S):
        for src, dst in re.findall(rb"<([^>]*)>\s*<([^>]*)>", block.group(1)):
            try:
                out[int(_HEX_JUNK.sub(b"", src) or b"0", 16)] = _utf16(dst)
            except ValueError:
                continue
    for block in re.finditer(rb"beginbfrange(.*?)endbfrange", data, re.S):
        for lo, hi, dst in re.findall(rb"<([^>]*)>\s*<([^>]*)>\s*(<[^>]*>|\[[^\]]*\])", block.group(1)):
            try:
                lo, hi = int(_HEX_JUNK.sub(b"", lo) or b"0", 16), int(_HEX_JUNK.sub(b"", hi) or b"0", 16)
            except ValueError:
                continue
            if not 0 <= hi - lo <= 65535:
                continue
            if dst.startswith(b"["):
                for i, item in enumerate(re.findall(rb"<([^>]*)>", dst)[:hi - lo + 1]):
                    out[lo + i] = _utf16(item)
            else:
                start = _utf16(dst[1:-1])
                for i in range(hi - lo + 1):
                    last = ord(start[-1]) + i if start else 0
                    out[lo + i] = start[:-1] + chr(last) if start and last < 0x110000 else ""
    return out


def _simple_table(encoding_name) -> list:
    codec = {"MacRomanEncoding": "mac_roman"}.get(encoding_name, "cp1252")
    return [bytes([i]).decode(codec, errors="ignore") for i in range(256)]


class _Font:
    def __init__(self, doc: _Doc, d) -> None:
        d = doc.get(d)
        d = d if isinstance(d, dict) else {}
        self.two_byte = d.get("Subtype") == "Type0"
        tu = doc.get(d.get("ToUnicode"))
        self.cmap = _cmap(doc.decode(tu) or b"") if isinstance(tu, Stream) else {}
        self.widths: dict = {}
        self.default_width = 500
        if self.two_byte:
            self.table = None
            kids = doc.get(d.get("DescendantFonts"))
            desc = doc.get(kids[0]) if isinstance(kids, list) and kids else None
            desc = desc if isinstance(desc, dict) else {}
            dw = doc.get(desc.get("DW"))
            self.default_width = dw if isinstance(dw, (int, float)) else 1000
            self._read_cid_widths(doc, doc.get(desc.get("W")))
        else:
            enc = doc.get(d.get("Encoding"))
            base = enc.get("BaseEncoding") if isinstance(enc, dict) else enc
            self.table = _simple_table(doc.get(base))
            diffs = doc.get(enc.get("Differences")) if isinstance(enc, dict) else None
            if isinstance(diffs, list):
                code = 0
                for item in diffs[:2048]:
                    item = doc.get(item)
                    if isinstance(item, int):
                        code = item
                    elif isinstance(item, Name):
                        if 0 <= code < 256:
                            glyph = _glyph(item)
                            if glyph is not None:
                                self.table[code] = glyph
                        code += 1
            first, widths = doc.get(d.get("FirstChar")), doc.get(d.get("Widths"))
            if isinstance(first, int) and isinstance(widths, list):
                for i, w in enumerate(widths[:256]):
                    w = doc.get(w)
                    if isinstance(w, (int, float)):
                        self.widths[first + i] = w

    def _read_cid_widths(self, doc: _Doc, w) -> None:
        if not isinstance(w, list):
            return
        i = 0
        while i < len(w) - 1 and len(self.widths) < 65536:
            start, nxt = doc.get(w[i]), doc.get(w[i + 1])
            if isinstance(start, int) and isinstance(nxt, list):
                for j, width in enumerate(nxt[:65536]):
                    if isinstance(width, (int, float)):
                        self.widths[start + j] = width
                i += 2
            elif i + 2 < len(w) and isinstance(start, int) and isinstance(nxt, int):
                width = doc.get(w[i + 2])
                if isinstance(width, (int, float)) and 0 <= nxt - start <= 65535:
                    for code in range(start, nxt + 1):
                        self.widths[code] = width
                i += 3
            else:
                break

    def codes(self, raw: bytes):
        """Yields (code, text) for each character in a string from the page."""
        if self.two_byte:
            for i in range(0, len(raw) - 1, 2):
                code = raw[i] << 8 | raw[i + 1]
                yield code, self.cmap.get(code, "")
        else:
            for code in raw:
                yield code, self.cmap[code] if code in self.cmap else self.table[code]

    def width(self, code: int) -> float:
        return self.widths.get(code, self.default_width) / 1000


# ----------------------------------------------------------------- pages

_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _mult(m, n):
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return (a * a2 + b * c2, a * b2 + b * d2, c * a2 + d * c2, c * b2 + d * d2,
            e * a2 + f * c2 + e2, e * b2 + f * d2 + f2)


def _nums(values, n):
    """The last n operands, as floats, or None if they aren't all numbers."""
    if len(values) < n or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values[-n:]):
        return None
    return [float(v) for v in values[-n:]]


def _colour_space(doc: _Doc, cs):
    """A picture's colour space as a plain name: DeviceGray, DeviceRGB, or something else."""
    cs = doc.get(cs)
    if isinstance(cs, list) and cs and doc.get(cs[0]) == "ICCBased":
        icc = doc.get(cs[1]) if len(cs) > 1 else None
        n = doc.get(icc.dict.get("N")) if isinstance(icc, Stream) else None
        return {1: "DeviceGray", 3: "DeviceRGB"}.get(n, "other")
    return {"G": "DeviceGray", "CalGray": "DeviceGray", "RGB": "DeviceRGB", "CalRGB": "DeviceRGB"}.get(cs, cs)


def _picture(doc: _Doc, stream: Stream):
    """Turns a PDF picture into (media type, bytes, (width, height)) a brain can open,
    a reason string if it can't be, or None if it isn't worth mentioning."""
    d = stream.dict
    if doc.get(d.get("ImageMask")) is True:
        return None  # a stencil shape, not a picture
    w, h = doc.get(d.get("Width")), doc.get(d.get("Height"))
    if not isinstance(w, int) or not isinstance(h, int) or min(w, h) < MIN_SIDE:
        return None
    filters = doc.get(d.get("Filter"))
    filters = [doc.get(f) for f in (filters if isinstance(filters, list) else [filters] if filters else [])]
    parms = doc.get(d.get("DecodeParms"))
    parms = doc.get(parms[-1]) if isinstance(parms, list) and parms else parms
    last = filters[-1] if filters else None
    if last in ("DCTDecode", "DCT"):
        return ("image/jpeg", stream.raw, (w, h)) if len(filters) == 1 else "in a format the plant can't open"
    if last in ("CCITTFaxDecode", "CCF", "JBIG2Decode"):
        return "black-and-white scans in a format the plant can't open yet"
    if any(f not in ("FlateDecode", "Fl") for f in filters):
        return "in a format the plant can't open"
    bpc = doc.get(d.get("BitsPerComponent")) or 8
    cs = doc.get(d.get("ColorSpace"))
    palette = b""
    if isinstance(cs, list) and cs and doc.get(cs[0]) in ("Indexed", "I") and len(cs) >= 4:
        base, hival, lookup = _colour_space(doc, cs[1]), doc.get(cs[2]), doc.get(cs[3])
        lookup = doc.decode(lookup) if isinstance(lookup, Stream) else lookup
        if not isinstance(hival, int) or not isinstance(lookup, bytes):
            return None
        if base == "DeviceRGB":
            palette = lookup[:3 * (hival + 1)]
        elif base == "DeviceGray":
            palette = b"".join(bytes([v, v, v]) for v in lookup[:hival + 1])
        else:
            return "in a colour format the plant can't open"
        colour, comps, depths = 3, 1, (1, 2, 4, 8)
    elif _colour_space(doc, cs) == "DeviceGray":
        colour, comps, depths = 0, 1, (1, 2, 4, 8)
    elif _colour_space(doc, cs) == "DeviceRGB":
        colour, comps, depths = 2, 3, (8,)
    else:
        return "in a colour format the plant can't open"
    if bpc not in depths:
        return "in a format the plant can't open"
    row = (w * comps * bpc + 7) // 8
    predictor = (doc.get(parms.get("Predictor")) or 1) if isinstance(parms, dict) else 1
    if (filters and predictor >= 10 and (doc.get(parms.get("Colors")) or 1) == comps
            and (doc.get(parms.get("BitsPerComponent")) or 8) == bpc and (doc.get(parms.get("Columns")) or 1) == w):
        idat = stream.raw  # already PNG-style rows in a zlib stream, which a PNG can use as they are
    elif predictor > 1:
        return "in a format the plant can't open"
    else:
        if row * h > MAX_STREAM_BYTES:
            return "too big to send"
        try:
            pixels = _inflate(stream.raw) if filters else stream.raw
        except _Stop:
            return "too big to send"
        if not pixels or len(pixels) < row * h:
            return None
        idat = zlib.compress(b"".join(b"\x00" + pixels[i * row:(i + 1) * row] for i in range(h)), 1)
    return "image/png", png(w, h, bpc, colour, idat, palette), (w, h)


class _Reader:
    def __init__(self, doc: _Doc, limit: int, pictures=None) -> None:
        self.doc, self.limit = doc, limit
        self.out: list = []
        self.size = 0
        self.fonts: dict = {}
        self.end = None       # where the last piece of text finished: x, y, font size
        self.gap = ""         # separator owed before the next piece of text
        self.pictures = pictures  # a pictures.Collector, or None to ignore pictures
        self.page_pictures: list = []
        self.page_text = 0

    def picture(self, stream: Stream, g: dict) -> None:
        if self.pictures is None:
            return
        found = _picture(self.doc, stream)
        if isinstance(found, str):
            self.pictures.skip(found)
            return
        if not found:
            return
        mime, data, dims = found
        mark = self.pictures.add(data, mime=mime, dims=dims)
        if not mark:
            return
        a, b, c, d = g["ctm"][:4]
        self.page_pictures.append((abs(a * d - b * c), mark))
        if self.out:
            self.out.append("\n\n" if self.gap == "\n\n" else "\n")  # keep a new page's break
        self.out.append(mark)
        self.gap, self.end = "\n", None

    def emit(self, text: str) -> None:
        if not text:
            return
        text = unmark(text)
        self.page_text += len(text.strip())
        if self.gap and self.out and (self.gap != " " or not (text[0].isspace() or self.out[-1][-1].isspace())):
            self.out.append(self.gap)
        self.gap = ""
        self.out.append(text)
        self.size += len(text)
        if self.size > self.limit:
            raise _Stop

    def page(self, page: dict, resources) -> None:
        contents = self.doc.get(page.get("Contents"))
        parts = contents if isinstance(contents, list) else [contents]
        data = b"\n".join(d for d in (self.doc.decode(p) for p in parts[:1000]) if d)
        g = {"ctm": _IDENTITY, "font": None, "fs": 0.0, "tc": 0.0, "tw": 0.0, "th": 1.0, "tl": 0.0}
        self.page_pictures, self.page_text = [], 0
        self.run(data, resources, g, set())
        if self.page_pictures and self.page_text < 30:  # a page that is one big picture is a scanned page
            self.pictures.make_page(max(self.page_pictures)[1])
        self.end = None
        if self.out:
            self.gap = "\n\n"

    def font(self, resources, name):
        fonts = self.doc.get(resources.get("Font")) if isinstance(resources, dict) else None
        ref = fonts.get(name) if isinstance(fonts, dict) else None
        key = ref if isinstance(ref, Ref) else id(ref)
        if key not in self.fonts:
            self.fonts[key] = _Font(self.doc, ref)
        return self.fonts[key]

    def show(self, raw: bytes, g: dict, tm):
        font = g["font"]
        if font is None or not isinstance(raw, bytes):
            return tm
        trm = _mult(tm, g["ctm"])
        size = abs(g["fs"]) * (hypot(trm[2], trm[3]) or 1.0) or 1.0
        if self.end is not None:
            ex, ey, esize = self.end
            if abs(trm[5] - ey) > 0.5 * max(size, esize):
                self.gap = self.gap if "\n" in self.gap else "\n"
            elif trm[4] - ex > 0.2 * size or ex - trm[4] > size:
                self.gap = self.gap or " "
        pieces = []
        for code, text in font.codes(raw):
            pieces.append(text)
            tx = font.width(code) * g["fs"] + g["tc"] + (g["tw"] if code == 32 and not font.two_byte else 0)
            tx *= g["th"]
            tm = (tm[0], tm[1], tm[2], tm[3], tm[4] + tx * tm[0], tm[5] + tx * tm[1])
        self.emit("".join(pieces))
        end = _mult(tm, g["ctm"])
        self.end = (end[4], end[5], size)
        return tm

    def run(self, data: bytes, resources, g: dict, forms: set) -> None:
        stack, ops = [], []
        tm = tlm = _IDENTITY
        pos, n = 0, len(data)
        while pos < n:
            value, pos = parse(data, pos)
            if value is _EOF:
                break
            if not isinstance(value, Kw):
                if value is not _END_ARRAY and value is not _END_DICT and len(ops) < 64:
                    ops.append(value)
                continue
            op = str(value)
            if op == "BT":
                tm = tlm = _IDENTITY
            elif op in ("Tj", "'", '"'):
                if op != "Tj":
                    if op == '"' and (nums := _nums(ops[:-1], 2)):
                        g["tw"], g["tc"] = nums
                    tm = tlm = _mult((1, 0, 0, 1, 0, -g["tl"]), tlm)
                if ops:
                    tm = self.show(ops[-1], g, tm)
            elif op == "TJ":
                items = ops[-1] if ops and isinstance(ops[-1], list) else []
                for item in items:
                    if isinstance(item, bytes):
                        tm = self.show(item, g, tm)
                    elif isinstance(item, (int, float)):
                        tx = -item / 1000 * g["fs"] * g["th"]
                        tm = (tm[0], tm[1], tm[2], tm[3], tm[4] + tx * tm[0], tm[5] + tx * tm[1])
            elif op in ("Td", "TD") and (nums := _nums(ops, 2)):
                if op == "TD":
                    g["tl"] = -nums[1]
                tm = tlm = _mult((1, 0, 0, 1, nums[0], nums[1]), tlm)
            elif op == "Tm" and (nums := _nums(ops, 6)):
                tm = tlm = tuple(nums)
            elif op == "T*":
                tm = tlm = _mult((1, 0, 0, 1, 0, -g["tl"]), tlm)
            elif op == "Tf" and len(ops) >= 2 and isinstance(ops[-2], Name) and (nums := _nums(ops, 1)):
                g["font"], g["fs"] = self.font(resources, ops[-2]), nums[0]
            elif op in ("Tc", "Tw", "TL") and (nums := _nums(ops, 1)):
                g[{"Tc": "tc", "Tw": "tw", "TL": "tl"}[op]] = nums[0]
            elif op == "Tz" and (nums := _nums(ops, 1)):
                g["th"] = nums[0] / 100
            elif op == "cm" and (nums := _nums(ops, 6)):
                g["ctm"] = _mult(tuple(nums), g["ctm"])
            elif op == "q" and len(stack) < 256:
                stack.append(dict(g))
            elif op == "Q" and stack:
                g.update(stack.pop())
            elif op == "Do" and ops and isinstance(ops[-1], Name):
                self.form(ops[-1], resources, g, forms)
            elif op == "ID":  # inline picture data: skip it
                m = _INLINE_IMAGE_END.search(data, pos)
                pos = m.end() if m else n
            ops = []

    def form(self, name, resources, g: dict, forms: set) -> None:
        objs = self.doc.get(resources.get("XObject")) if isinstance(resources, dict) else None
        ref = objs.get(name) if isinstance(objs, dict) else None
        form = self.doc.get(ref)
        if isinstance(form, Stream) and form.dict.get("Subtype") == "Image":
            self.picture(form, g)
            return
        if (not isinstance(form, Stream) or form.dict.get("Subtype") != "Form"
                or id(form) in forms or len(forms) >= MAX_FORM_DEPTH):
            return
        data = self.doc.decode(form)
        if not data:
            return
        inner = dict(g)
        matrix = self.doc.get(form.dict.get("Matrix"))
        if isinstance(matrix, list) and (nums := _nums(matrix, 6)) and len(matrix) == 6:
            inner["ctm"] = _mult(tuple(nums), g["ctm"])
        own = self.doc.get(form.dict.get("Resources"))
        self.run(data, own if isinstance(own, dict) else resources, inner, forms | {id(form)})


def _pages(doc: _Doc) -> list:
    """Every page in reading order, with the resources (fonts and so on) it uses."""
    found, seen = [], set()
    todo = [(doc.get(doc.root.get("Pages")) if doc.root else None, None, 0)]
    while todo and len(found) < 100_000:
        node, inherited, depth = todo.pop()
        if not isinstance(node, dict) or id(node) in seen or depth > 64:
            continue
        seen.add(id(node))
        resources = doc.get(node.get("Resources")) or inherited
        kids = doc.get(node.get("Kids"))
        if isinstance(kids, list):
            todo.extend((doc.get(k), resources, depth + 1) for k in reversed(kids))
        elif node.get("Type") != "Pages":
            found.append((node, resources))
    if found:
        return found
    for _, value in sorted((k, v) for k, v in doc.objects.items() if isinstance(v, dict)):
        if value.get("Type") == "Page":
            node, resources = value, None
            for _ in range(64):
                if not isinstance(node, dict):
                    break
                resources = doc.get(node.get("Resources"))
                if resources:
                    break
                node = doc.get(node.get("Parent"))
            found.append((value, resources))
    return found


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f￾￿]")


def pdf_text(raw: bytes, limit: int = 250_000, pictures=None) -> str:
    """The text of a PDF, page by page. Stops after about `limit` characters.
    With a pictures.Collector, pictures are gathered too and marked where they appear."""
    try:
        doc = _Doc(raw)
    except _Stop:
        raise ValueError("That PDF is too big to unpack. Try a shorter one.")
    except RecursionError:
        raise ValueError(NOT_PDF)
    if doc.root is None and not doc.objects:
        raise ValueError(NOT_PDF)
    reader = _Reader(doc, limit, pictures)
    try:
        for page, resources in _pages(doc):
            try:
                reader.page(page, resources)
            except (ValueError, RecursionError, TypeError, IndexError, KeyError, AttributeError):
                continue  # one broken page shouldn't spoil the rest
    except _Stop:
        pass
    return _CONTROL.sub("", "".join(reader.out))
