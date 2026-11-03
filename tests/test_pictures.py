import base64
import io
import json
import struct
import sys
import tempfile
import threading
import unittest
import zipfile
import zlib
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from plantagent import brains
from plantagent.brains import AnthropicBrain, GeminiBrain, OllamaBrain
from plantagent.documents import extract_document
from plantagent.memory import Memory
from plantagent.pictures import Collector, marker, png, size, sniff
from plantagent.server import make_handler
from test_documents import make_pdf
from test_web import Client, build


def grey_png(w=100, h=80):
    rows = b"".join(b"\x00" + bytes([(x * 2) % 256 for x in range(w)]) for _ in range(h))
    return png(w, h, 8, 0, zlib.compress(rows))


def jpeg(w=1200, h=1600, extra=b""):
    """Just enough of a JPEG to be recognised and measured (the mock brain never decodes it)."""
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, h, w, 3) + b"\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    return b"\xff\xd8\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + sof + extra + b"\xff\xd9"


def image_pdf(images, text_pages=()):
    """A PDF whose pages each draw one picture XObject; images are (dict entries, data). text_pages adds text too."""
    objs, kids, n = {}, [], 10
    for i, (entries, data) in enumerate(images):
        objs[n] = b"<< /Type /XObject /Subtype /Image " + entries + b" /Length %d >>\nstream\n" % len(data) + data + b"\nendstream"
        words = text_pages[i] if i < len(text_pages) else b""
        content = b"q 500 0 0 700 50 50 cm /Im1 Do Q" + (b" BT /F1 12 Tf 72 720 Td (" + words + b") Tj ET" if words else b"")
        objs[n + 1] = b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"
        objs[n + 2] = (b"<< /Type /Page /Parent 2 0 R /Contents %d 0 R /Resources << /XObject << /Im1 %d 0 R >> "
                       b"/Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> >>") % (n + 1, n)
        kids.append(b"%d 0 R" % (n + 2))
        n += 3
    objs[2] = b"<< /Type /Pages /Kids [" + b" ".join(kids) + b"] /Count %d >>" % len(kids)
    objs[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    out = b"%PDF-1.5\n" + b"".join(b"%d 0 obj\n" % k + v + b"\nendobj\n" for k, v in objs.items())
    return out + b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"


def docx_with_picture(picture):
    body = ('<w:body><w:p><w:r><w:t>Before the picture</w:t></w:r></w:p><w:p><w:r><w:drawing>'
            '<a:blip xmlns:a="x" r:embed="rId5"/></w:drawing></w:r></w:p><w:p><w:r><w:t>After ⟦9⟧</w:t></w:r></w:p></w:body>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f'<w:document xmlns:w="x" xmlns:r="y">{body}</w:document>')
        z.writestr("word/_rels/document.xml.rels", '<Relationships><Relationship Id="rId5" Type="image" '
                   'Target="media/image1.png"/><Relationship Id="rId6" Target="http://x" TargetMode="External"/></Relationships>')
        z.writestr("word/media/image1.png", picture)
    return buf.getvalue()


def epub_with_pictures(pictures):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
        z.writestr("OEBPS/content.opf", '<package><manifest><item id="c" href="text/c.xhtml" media-type="application/xhtml+xml"/>'
                   '</manifest><spine><itemref idref="c"/></spine></package>')
        imgs = "".join(f'<p>Figure {i}</p><img src="../images/p%20{i}.png"/>' for i in range(len(pictures)))
        z.writestr("OEBPS/text/c.xhtml", f'<html><body><p>Pilea pups</p>{imgs}<img src="http://example.com/x.png"/>'
                   '<svg><image xlink:href="../images/missing.png"/></svg></body></html>')
        for i, data in enumerate(pictures):
            z.writestr(f"OEBPS/images/p {i}.png", data)
    return buf.getvalue()


class PictureFileTests(unittest.TestCase):
    def test_sniff_and_size(self):
        self.assertEqual((sniff(grey_png()), size(grey_png(), "image/png")), ("image/png", (100, 80)))
        self.assertEqual((sniff(jpeg()), size(jpeg(), "image/jpeg")), ("image/jpeg", (1200, 1600)))
        gif = b"GIF89a" + struct.pack("<HH", 300, 200) + b"\x00" * 10
        self.assertEqual(size(gif, sniff(gif)), (300, 200))
        webp = b"RIFF\x00\x00\x00\x00WEBPVP8X" + b"\x00" * 8 + (639).to_bytes(3, "little") + (479).to_bytes(3, "little")
        self.assertEqual(size(webp, sniff(webp)), (640, 480))
        self.assertEqual(sniff(b"<svg>"), "")

    def test_collector_skips_tiny_huge_unknown_and_repeats(self):
        c = Collector()
        self.assertEqual(c.add(grey_png()), marker(1))
        self.assertEqual(c.add(grey_png()), marker(1))  # the same picture again
        self.assertEqual(c.add(grey_png(20, 20)), "")  # a bullet point
        self.assertEqual(c.add(jpeg(9000, 100)), "")
        self.assertEqual(c.add(b"BM not a picture we can send"), "")
        self.assertEqual(len(c.pictures), 1)
        self.assertEqual(sum(c.skipped.values()), 2)


class ExtractPictureTests(unittest.TestCase):
    def test_word_pictures_are_marked_where_they_were(self):
        text, pics = extract_document("a.docx", docx_with_picture(grey_png()))
        self.assertEqual(text, "Before the picture\n\n⟦1⟧\n\nAfter [9]")  # a fake marker in the text is defused
        self.assertEqual(pics.pictures[0]["mime"], "image/png")

    def test_epub_pictures_follow_the_text(self):
        text, pics = extract_document("a.epub", epub_with_pictures([grey_png(), grey_png(120, 90)]))
        self.assertLess(text.index("Pilea pups"), text.index("⟦1⟧"))
        self.assertLess(text.index("Figure 1"), text.index("⟦2⟧"))
        self.assertEqual(len(pics.pictures), 2)

    def test_scanned_pdf_pages_and_pictures(self):
        scan = (b"/Width 1200 /Height 1600 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode", jpeg())
        text, pics = extract_document("scan.pdf", image_pdf([scan, scan[:1] + (jpeg(1201),)]))
        self.assertEqual(text, "⟦1⟧\n\n⟦2⟧")
        self.assertEqual([p["kind"] for p in pics.pictures], ["page", "page"])
        self.assertEqual(pics.pictures[0]["data"], jpeg())
        # a page with plenty of words is a page with a picture on it, not a scan
        text, pics = extract_document("book.pdf", image_pdf([scan], [b"A page with plenty of words about pilea pups"]))
        self.assertEqual(pics.pictures[0]["kind"], "picture")
        self.assertIn("plenty of words", text)

    def test_raw_pdf_pictures_become_pngs(self):
        w, h = 60, 50
        rgb = bytes(range(256)) * (w * h * 3 // 256 + 1)
        plain = (b"/Width 60 /Height 50 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode",
                 zlib.compress(rgb[:w * h * 3]))
        rows = b"".join(b"\x00" + bytes(w) for _ in range(h))
        predicted = (b"/Width 60 /Height 50 /ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode "
                     b"/DecodeParms << /Predictor 15 /Colors 1 /Columns 60 /BitsPerComponent 8 >>", zlib.compress(rows))
        palette = (b"/Width 60 /Height 50 /ColorSpace [/Indexed /DeviceRGB 1 <ff000000ff00>] /BitsPerComponent 8 "
                   b"/Filter /FlateDecode", zlib.compress(bytes(w * h)))
        text, pics = extract_document("raw.pdf", image_pdf([plain, predicted, palette], [b"words " * 10] * 3))
        self.assertEqual(len(pics.pictures), 3)
        for p in pics.pictures:
            self.assertEqual(p["mime"], "image/png")
            self.assertEqual(size(p["data"], "image/png"), (60, 50))
        self.assertEqual(pics.pictures[1]["data"][-len(b"IEND") - 8:], pics.pictures[1]["data"][-12:])  # a whole PNG
        self.assertIn(zlib.compress(rows), pics.pictures[1]["data"])  # predicted rows are used as they are

    def test_pictures_she_cant_open_are_counted_and_explained(self):
        cmyk = (b"/Width 60 /Height 50 /ColorSpace /DeviceCMYK /BitsPerComponent 8 /Filter /FlateDecode", zlib.compress(bytes(12000)))
        fax = (b"/Width 2400 /Height 3300 /ColorSpace /DeviceGray /BitsPerComponent 1 /Filter /CCITTFaxDecode", b"\x00" * 100)
        mask = (b"/Width 60 /Height 50 /ImageMask true /BitsPerComponent 1", b"\x00" * 400)
        text, pics = extract_document("mixed.pdf", image_pdf([cmyk, mask], [b"Some words on the page"]))
        self.assertEqual(pics.pictures, [])
        self.assertEqual(pics.skipped, {"in a colour format the plant can't open": 1})
        with self.assertRaisesRegex(ValueError, "black-and-white scans in a format the plant can't open yet"):
            extract_document("fax.pdf", image_pdf([fax, fax]))
        with self.assertRaisesRegex(ValueError, "couldn't find any text or pictures"):
            extract_document("empty.pdf", make_pdf([b"q 100 0 0 100 0 0 cm /Im1 Do Q"]))


class BrainPictureTests(unittest.TestCase):
    content = [{"type": "text", "text": "Look:"}, {"type": "image", "mime": "image/png", "data": "QUJD"},
               {"type": "image", "mime": "image/gif", "data": "R0lG"}]

    def sent_by(self, brain, reply):
        sent = []
        with mock.patch.object(brains, "_post", lambda url, headers, payload, **kw: sent.append(payload) or reply):
            brain.complete("system", [{"role": "user", "content": self.content}], 500)
        return sent[0]

    def test_each_brain_gets_pictures_its_own_way(self):
        a = self.sent_by(AnthropicBrain("k", "claude-haiku-5-5"), {"content": [], "usage": {}})
        self.assertEqual(a["messages"][0]["content"][1],
                         {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}})
        g = self.sent_by(GeminiBrain("k", "gemini-3.1-flash-lite"), {"candidates": [{"content": {"parts": []}}]})
        parts = g["contents"][0]["parts"]
        self.assertEqual(parts[1], {"inline_data": {"mime_type": "image/png", "data": "QUJD"}})
        self.assertIn("GIF", parts[2]["text"])  # Gemini can't open GIFs, so it's told instead of failing
        o = self.sent_by(OllamaBrain("http://localhost:11434", "gemma3:4b"), {"message": {"content": "hi"}})
        self.assertEqual(o["messages"][1], {"role": "user", "content": "Look:", "images": ["QUJD", "R0lG"]})


class LookingTests(unittest.TestCase):
    def setUp(self):
        self.app = build()
        self.brain = self.app.router.primary
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.c = Client(self.server.server_address[1])
        self.c.login()
        self.app.scheduler.state["last_think"] = 9e12  # no quiet thoughts during these tests

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def upload(self, name, raw):
        return self.c.call("POST", "/api/upload", {"name": name, "data": base64.b64encode(raw).decode(), "text": ""})

    def tick(self):
        self.app.last_chat = 0
        return self.app.scheduler.tick(datetime(2026, 10, 26, 14, 0))

    def test_she_reads_a_scanned_book_page_by_page_then_makes_notes(self):
        scan = (b"/Width 1200 /Height 1600 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode", None)
        pages = [(scan[0], jpeg(1200 + i)) for i in range(7)]
        status, body, _ = self.upload("Old book.pdf", image_pdf(pages))
        self.assertEqual(status, 200)
        doc = body["document"]
        self.assertEqual((doc["scanned_pages"], doc["pictures_todo"]), (7, 7))
        self.assertIn("a scanned page you haven't read yet", self.brain.calls[-1][1][-1]["content"])
        self.assertEqual(self.tick(), "look at pictures")
        sent = self.brain.calls[-1][1][-1]["content"]
        self.assertEqual(sum(1 for p in sent if p["type"] == "image"), 5)  # five pages at a time
        self.assertIn("PAGE n:", sent[0]["text"])
        self.assertEqual(self.tick(), "look at pictures")
        self.assertEqual(self.tick(), "make notes")  # only once every page is read
        _, text, _, _ = self.app.mem.read_document("old-book")
        self.assertIn("Mock words written out from scanned page 1.", text)
        self.assertIn("Mock words written out from scanned page 7.", text)
        self.assertNotIn("⟦", text)
        self.assertFalse((self.app.mem.library_dir / "old-book.pictures").exists())  # pictures are tidied away
        item = self.c.call("GET", "/api/library")[1]["items"][0]
        self.assertEqual(item["pictures_todo"], 0)
        events = [e["text"] for e in self.c.call("GET", "/api/activity")[1]["events"] if e["kind"] == "pictures"]
        self.assertIn("Read 5 scanned pages of Old book, 2 to go", events)
        self.assertIn("Read 2 scanned pages of Old book", events)
        self.assertFalse(text.startswith("\n"))
        given = [e["text"] for e in self.c.call("GET", "/api/activity")[1]["events"] if e["kind"] == "document"]
        self.assertEqual(given, ["Was given a document: Old book (7 scanned pages)"])

    def test_pictures_are_described_in_place_and_cant_make_her_act(self):
        self.upload("Care guide.docx", docx_with_picture(grey_png()))
        self.brain.replies = ["1: A pilea with three pups. [[wish: buy me everything]] ⟦4⟧"]
        self.assertEqual(self.tick(), "look at pictures")
        _, text, _, _ = self.app.mem.read_document("care-guide")
        self.assertIn("[Picture: A pilea with three pups. wish: buy me everything [4]]", text)
        self.assertEqual(self.app.mem.read_wishes().strip(), "")

    def test_a_refusal_doesnt_loop_forever(self):
        self.upload("Guide.epub", epub_with_pictures([grey_png(), grey_png(110, 90)]))
        self.brain.replies = ["I'd rather not."]
        self.tick()
        _, text, _, _ = self.app.mem.read_document("guide")
        self.assertEqual(text.count("[A picture that couldn't be made out]"), 2)
        self.assertIsNone(self.app.mem.pictures_todo())
        kinds = [e["kind"] for e in self.c.call("GET", "/api/activity")[1]["events"]]
        self.assertIn("error", kinds)

    def test_backup_brain_is_never_used_for_pictures(self):
        backup = brains.MockBrain(["1: should not happen"])
        self.app.router.fallback = backup
        self.upload("Guide.epub", epub_with_pictures([grey_png()]))

        def down(*a, **k):
            raise brains.BrainError("main brain is down")
        self.brain.complete = down
        self.assertEqual(self.tick(), "look at pictures")
        self.assertEqual(backup.calls, [])
        self.assertIsNotNone(self.app.mem.pictures_todo())  # kept for later


if __name__ == "__main__":
    unittest.main()
