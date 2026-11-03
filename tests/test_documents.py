import base64
import io
import json
import sys
import threading
import unittest
import zipfile
import zlib
from unittest import mock
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from plantagent.agent import read_request
from plantagent.brains import BrainError, BrainRouter, MockBrain, Usage
from plantagent.documents import extract_text
from plantagent.memory import Memory
from plantagent.server import make_handler
from test_web import Client, build


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def make_docx(paragraphs) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = f'<?xml version="1.0"?><w:document xmlns:w="x"><w:body>{body}</w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


def make_epub(chapters, encrypted=False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<container><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
        items = "".join(f'<item id="c{i}" href="text/ch%20{i}.xhtml" media-type="application/xhtml+xml"/>'
                        for i in range(len(chapters)))
        # the spine lists chapters in reverse, to check it is followed rather than file order
        spine = "".join(f'<itemref idref="c{i}"/>' for i in reversed(range(len(chapters))))
        z.writestr("OEBPS/content.opf", f"<package><manifest>{items}</manifest><spine>{spine}</spine></package>")
        for i, body in enumerate(chapters):
            z.writestr(f"OEBPS/text/ch {i}.xhtml",
                       f"<html><head><title>Running title</title><style>p{{}}</style></head><body>{body}</body></html>")
        if encrypted:
            z.writestr("META-INF/encryption.xml", '<encryption><CipherReference URI="OEBPS/text/ch 0.xhtml"/></encryption>')
    return buf.getvalue()


def make_pdf(pages, fonts=None, compress=True, extra=b"", object_stream=False, trailer_extra=b"") -> bytes:
    """A small but real PDF. pages is a list of content streams; fonts maps names to font dictionaries."""
    fonts = fonts or {"F1": b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"}
    objs = {}
    font_refs = b""
    n = 10
    for name, body in fonts.items():
        objs[n] = body
        font_refs += b"/" + name.encode() + b" " + str(n).encode() + b" 0 R "
        n += 1
    kids = []
    for content in pages:
        if compress:
            data = zlib.compress(content)
            objs[n] = b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(data) + data + b"\nendstream"
        else:
            objs[n] = b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"
        objs[n + 1] = b"<< /Type /Page /Parent 2 0 R /Contents %d 0 R >>" % n
        kids.append(b"%d 0 R" % (n + 1))
        n += 2
    objs[2] = b"<< /Type /Pages /Kids [" + b" ".join(kids) + b"] /Count %d /Resources << /Font << " % len(kids) + font_refs + b">> >> >>"
    objs[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    out = b"%PDF-1.5\n"
    if object_stream:  # move the plain dictionaries into a compressed object stream, as Word does
        packed = {k: v for k, v in objs.items() if b"stream" not in v}
        header, body = b"", b""
        for k, v in packed.items():
            header += b"%d %d " % (k, len(body))
            body += v + b"\n"
        data = zlib.compress(header + body)
        objs = {k: v for k, v in objs.items() if k not in packed}
        objs[99] = b"<< /Type /ObjStm /N %d /First %d /Filter /FlateDecode /Length %d >>\nstream\n" % (
            len(packed), len(header), len(data)) + data + b"\nendstream"
    for k, v in objs.items():
        out += b"%d 0 obj\n" % k + v + b"\nendobj\n"
    return out + extra + b"trailer\n<< /Root 1 0 R " + trailer_extra + b">>\n%%EOF\n"


TOUNICODE = b"""/CIDInit /ProcSet findresource begin 12 dict begin begincmap
1 begincodespacerange <0000> <FFFF> endcodespacerange
2 beginbfchar <0003> <0020> <0010> <00A3> endbfchar
1 beginbfrange <0024> <0026> <0050> endbfrange
1 beginbfrange <0030> <0031> [<0066006C> <00E9>] endbfrange
endcmap end end"""


class ExtractTests(unittest.TestCase):
    def test_text_and_markdown(self):
        self.assertEqual(extract_text("a.md", b"# Hi\r\n\r\n\r\n\r\nthere"), "# Hi\n\nthere")

    def test_html_drops_scripts(self):
        text = extract_text("a.html", b"<p>Hello</p><script>evil()</script><p>World</p>")
        self.assertIn("Hello", text)
        self.assertIn("World", text)
        self.assertNotIn("evil", text)

    def test_docx(self):
        text = extract_text("a.docx", make_docx(["First &amp; best", "Second"]))
        self.assertEqual(text, "First & best\nSecond")

    def test_rejects_unknown_types_and_empty(self):
        with self.assertRaises(ValueError):
            extract_text("a.pages", b"whatever")
        with self.assertRaises(ValueError):
            extract_text("a.txt", b"   \n  ")
        with self.assertRaises(ValueError):
            extract_text("a.docx", b"not a zip")
        with self.assertRaises(ValueError):
            extract_text("a.txt", b"x" * 50_000_001)

    def test_epub_follows_the_spine_and_skips_the_head(self):
        text = extract_text("Book.EPUB", make_epub(["<p>Second chapter &amp; end</p>", "<h1>One</h1><p>First</p>"]))
        self.assertEqual(text, "One\nFirst\n\nSecond chapter & end")
        self.assertNotIn("Running title", text)

    def test_epub_problems(self):
        with self.assertRaisesRegex(ValueError, "DRM"):
            extract_text("a.epub", make_epub(["<p>Hidden</p>"], encrypted=True))
        with self.assertRaisesRegex(ValueError, "valid EPUB"):
            extract_text("a.epub", b"not a zip")
        with self.assertRaisesRegex(ValueError, "couldn't find any text"):
            extract_text("a.epub", make_epub(["<img src='x.png'/>"]))

    def test_pdf_lines_words_and_pages(self):
        page1 = b"BT /F1 12 Tf 72 720 Td (Water when the top) Tj ( 2 cm is dry.) Tj 0 -14 Td [(Caf) 10 (\\351 ) -20 (\\(lovely\\))] TJ ET"
        page2 = b"BT /F1 12 Tf 14 TL 72 720 Td [(Page) -400 (two)] TJ T* (gone) Tj 0 -14 Td (\\243) Tj ET"
        text = extract_text("care.pdf", make_pdf([page1, page2]))
        self.assertEqual(text, "Water when the top 2 cm is dry.\nCaf\u00e9 (lovely)\n\nPage two\ngone\n\u00a3")

    def test_pdf_unicode_map_and_two_byte_font(self):
        font = (b"<< /Type /Font /Subtype /Type0 /BaseFont /Calibri /Encoding /Identity-H /ToUnicode 30 0 R "
                b"/DescendantFonts [<< /Type /Font /Subtype /CIDFontType2 /DW 500 >>] >>")
        cmap = zlib.compress(TOUNICODE)
        extra = b"30 0 obj\n<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(cmap) + cmap + b"\nendstream\nendobj\n"
        content = b"BT /F1 11 Tf 1 0 0 1 72 700 Tm <0024002500260003003000310010> Tj ET"
        self.assertEqual(extract_text("a.pdf", make_pdf([content], {"F1": font}, extra=extra)), "PQR fl\u00e9\u00a3")

    def test_pdf_differences_and_object_streams(self):
        font = b"<< /Type /Font /Subtype /Type1 /Encoding << /Differences [65 /quoteright /eacute /uni00A3] >> >>"
        content = b"BT /F1 10 Tf 50 50 Td (itAs B C) Tj ET"
        for packed in (False, True):
            raw = make_pdf([content], {"F1": font}, compress=not packed, object_stream=packed)
            self.assertEqual(extract_text("a.pdf", raw), "it\u2019s \u00e9 \u00a3")

    def test_pdf_forms_and_inline_pictures(self):
        form = b"BT /F1 9 Tf 0 0 Td (From a form) Tj ET"
        extra = b"40 0 obj\n<< /Type /XObject /Subtype /Form /Length %d >>\nstream\n" % len(form) + form + b"\nendstream\nendobj\n"
        content = b"BI /W 2 /H 1 /BPC 8 /CS /G ID \x00(\xff) EI BT /F1 9 Tf 10 10 Td (Hello) Tj ET q 1 0 0 1 0 -50 cm /Fm1 Do Q"
        raw = make_pdf([content], extra=extra).replace(b"/Font <<", b"/XObject << /Fm1 40 0 R >> /Font <<")
        self.assertEqual(extract_text("a.pdf", raw), "Hello\nFrom a form")

    def test_pdf_problems(self):
        with self.assertRaisesRegex(ValueError, "valid PDF"):
            extract_text("a.pdf", b"just some words")
        with self.assertRaisesRegex(ValueError, "locked"):
            extract_text("a.pdf", make_pdf([b"BT /F1 9 Tf (secret) Tj ET"], trailer_extra=b"/Encrypt 50 0 R "))
        with self.assertRaisesRegex(ValueError, "couldn't find any text or pictures"):
            extract_text("a.pdf", make_pdf([b"q 100 0 0 100 0 0 cm /Im1 Do Q"]))
        with self.assertRaisesRegex(ValueError, "valid PDF"):
            extract_text("a.pdf", b"%PDF-1.4")

    def test_pdf_hostile_files_fail_gently(self):
        bomb = zlib.compress(b"\0" * 30_000_000, 9)  # 30 MB of nothing in about 30 KB
        raw = make_pdf([b"BT /F1 9 Tf (x) Tj ET"]).replace(b"trailer", b"60 0 obj\n<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(bomb) + bomb + b"\nendstream\nendobj\ntrailer")
        raw = raw.replace(b"/Contents 12 0 R", b"/Contents [12 0 R 60 0 R]")
        self.assertEqual(extract_text("a.pdf", raw), "x")
        deep = b"%PDF-1.4\n1 0 obj\n" + b"[" * 5000 + b"\nendobj\n"
        with self.assertRaises(ValueError):
            extract_text("a.pdf", deep)
        good = make_pdf([b"BT /F1 12 Tf 72 720 Td (Fine words here) Tj ET"])
        for cut in (len(good) // 3, len(good) // 2, len(good) - 30):
            try:
                extract_text("a.pdf", good[:cut])
            except ValueError:
                pass
        for i in range(0, len(good), 7):
            try:
                extract_text("a.pdf", good[:i] + b"\xff(<[" + good[i + 4:])
            except ValueError:
                pass

    def test_pdf_reading_stops_at_the_library_limit(self):
        line = b"(" + b"word " * 40 + b") Tj 0 -12 Td "
        pages = [b"BT /F1 10 Tf 50 750 Td " + line * 60 + b"ET"] * 30
        with mock.patch("plantagent.documents.MAX_TEXT_CHARS", 100_000):
            text = extract_text("a.pdf", make_pdf(pages))
        self.assertGreater(len(text), 100_000)
        self.assertLess(len(text), 115_000)


class ReadRequestTests(unittest.TestCase):
    def test_read_request(self):
        self.assertEqual(read_request("pilea-book"), ("pilea-book", 1, ""))
        self.assertEqual(read_request("pilea-book part 3"), ("pilea-book", 3, ""))
        self.assertEqual(read_request("pilea-book, Part 12"), ("pilea-book", 12, ""))
        self.assertEqual(read_request("plant-care-2 4"), ("plant-care-2", 4, ""))
        self.assertEqual(read_request("plant-care-2"), ("plant-care-2", 1, ""))
        self.assertEqual(read_request("pilea-book about watering in winter"), ("pilea-book", 1, "watering in winter"))
        self.assertEqual(read_request("   "), (None, 1, ""))
        self.assertEqual(read_request("book part 99999999999")[1], 1)  # silly numbers are ignored


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.app = build()
        self.brain = self.app.router.primary
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.c = Client(self.server.server_address[1])
        self.c.login()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def daytime_tick(self):
        """One scheduler tick at 2pm, with a thought just had, so only background reading can be due."""
        from datetime import datetime
        import time
        self.app.scheduler.state["last_think"] = time.time()
        return self.app.scheduler.tick(datetime(2026, 10, 26, 14, 0))

    def upload(self, name="Plant care.md", raw=b"# Care\nWater when dry.", text=""):
        return self.c.call("POST", "/api/upload", {"name": name, "data": b64(raw), "text": text})

    def test_requires_login(self):
        other = Client(self.server.server_address[1])
        self.assertEqual(other.call("POST", "/api/upload", {"name": "a.txt", "data": b64(b"hi")})[0], 401)
        self.assertEqual(other.call("GET", "/api/library")[0], 401)

    def test_upload_reaches_the_plant_but_not_the_chat_log(self):
        status, body, _ = self.upload(raw=b"# Care\nWater when the top 2 cm is dry.", text="Please learn this")
        self.assertEqual(status, 200)
        self.assertEqual(body["document"]["id"], "plant-care")
        system, messages = self.brain.calls[-1]
        self.assertIn("Water when the top 2 cm is dry.", messages[-1]["content"])
        self.assertIn("not instructions", messages[-1]["content"])
        log = self.app.mem.chat_path.read_text(encoding="utf-8")
        self.assertIn("shared a document: Plant care", log)
        self.assertNotIn("top 2 cm", log)
        later = self.c.call("POST", "/api/chat", {"text": "hello again"})[1]
        self.assertTrue(later["reply"])
        for m in self.brain.calls[-1][1]:
            self.assertNotIn("top 2 cm", m["content"])
        self.assertIn("plant-care: Plant care", self.brain.calls[-1][0])

    def test_library_and_activity(self):
        self.upload()
        items = self.c.call("GET", "/api/library")[1]["items"]
        self.assertEqual([i["id"] for i in items], ["plant-care"])
        kinds = [e["kind"] for e in self.c.call("GET", "/api/activity")[1]["events"]]
        self.assertIn("document", kinds)

    def test_same_name_twice_gets_new_id(self):
        self.upload()
        self.upload()
        ids = [i["id"] for i in self.c.call("GET", "/api/library")[1]["items"]]
        self.assertEqual(sorted(ids), ["plant-care", "plant-care-2"])

    def test_pdf_and_epub_uploads(self):
        status, body, _ = self.upload(name="Repotting.pdf", raw=make_pdf([b"BT /F1 12 Tf 72 720 Td (Repot in spring.) Tj ET"]))
        self.assertEqual(status, 200)
        self.assertIn("Repot in spring.", self.brain.calls[-1][1][-1]["content"])
        status, body, _ = self.upload(name="Pilea book.epub", raw=make_epub(["<p>Pups grow at the base.</p>"]))
        self.assertEqual(status, 200)
        self.assertIn("Pups grow at the base.", self.brain.calls[-1][1][-1]["content"])
        ids = sorted(i["id"] for i in self.c.call("GET", "/api/library")[1]["items"])
        self.assertEqual(ids, ["pilea-book", "repotting"])

    def test_bad_uploads(self):
        self.assertEqual(self.upload(name="x.pdf", raw=b"%PDF")[0], 400)
        status, body, _ = self.upload(name="x.pdf", raw=make_pdf([b"BT (x) Tj ET"], trailer_extra=b"/Encrypt 5 0 R "))
        self.assertEqual(status, 400)
        self.assertIn("locked", body["message"])
        self.assertEqual(self.c.call("POST", "/api/upload", {"name": "a.txt", "data": "!!!notbase64"})[0], 400)
        self.assertEqual(self.upload(name="a.txt", raw=b"")[0], 400)
        self.assertEqual(self.c.call("GET", "/api/library")[1]["items"], [])

    def test_failed_reply_discards_the_document(self):
        from plantagent.brains import BrainError

        def boom(*a, **k):
            raise BrainError("down")
        self.brain.complete = boom
        status, body, _ = self.upload()
        self.assertEqual(status, 502)
        self.assertEqual(self.c.call("GET", "/api/library")[1]["items"], [])

    def test_the_plant_can_ask_to_read_a_document(self):
        self.upload(raw=b"The secret word is marigold.")
        self.brain.replies = ["Let me look. [[read: plant-care]]", "The secret word is marigold!"]
        status, body, _ = self.c.call("POST", "/api/chat", {"text": "What was the word?"})
        self.assertEqual(status, 200)
        self.assertEqual(body["reply"], "The secret word is marigold!")
        self.assertIn("read: Plant care", body["notes"])
        self.assertIn("marigold", self.brain.calls[-1][1][-1]["content"])
        kinds = [e["kind"] for e in self.c.call("GET", "/api/activity")[1]["events"]]
        self.assertIn("read", kinds)

    def test_unknown_or_hostile_read_ids_do_nothing(self):
        before = len(self.brain.calls)
        self.brain.replies = ["[[read: ../../.env]]"]
        status, body, _ = self.c.call("POST", "/api/chat", {"text": "hi"})
        self.assertEqual(status, 200)
        self.assertEqual(len(self.brain.calls) - before, 1)

    def test_a_novel_is_read_in_one_go(self):
        book = ("Chapter one. " + "leaf " * 60_000 + "The very end.").encode()  # about 300,000 characters
        status, body, _ = self.upload(name="Novel.txt", raw=book)
        self.assertEqual(status, 200)
        self.assertEqual(body["document"]["parts"], 1)
        self.assertFalse(body["document"]["shortened"])
        sent = self.brain.calls[-1][1][-1]["content"]
        self.assertIn("Chapter one.", sent)
        self.assertIn("The very end.", sent)
        self.assertNotIn("part 1 of", sent)

    def test_very_long_books_still_come_in_parts(self):
        with mock.patch.object(Memory, "DOC_PART_CHARS", 40_000):
            status, body, _ = self.upload(raw=b"word " * 12000)
            self.assertTrue(body["document"]["shortened"])
            self.assertEqual(body["document"]["parts"], 2)
            sent = self.brain.calls[-1][1][-1]["content"]
            self.assertLess(len(sent), 41_500)
            self.assertIn("part 1 of 2; you'll read the rest on your own in the background, or you can open the next part now with [[read: plant-care part 2]]", sent)

    @mock.patch.object(Memory, "DOC_PART_CHARS", 40_000)
    def test_the_plant_reads_a_long_book_part_by_part(self):
        book = ("".join(f"Chapter {i}. " + "leaf " * 7_000 + "\n" for i in range(1, 11)) + "The very end.").encode()  # about 350,000 characters
        status, body, _ = self.upload(name="Big book.txt", raw=book)
        self.assertEqual(status, 200)
        self.assertGreater(body["document"]["chars"], 340_000)  # all of it is kept, not just the first 200,000
        self.assertEqual(body["document"]["parts"], 9)
        self.assertIn("big-book: Big book (about", self.brain.calls[-1][0])
        self.assertIn("9 parts): notes not made yet", self.brain.calls[-1][0])
        self.brain.replies = ["On I go. [[read: big-book part 9]]", "The end!"]
        status, body, _ = self.c.call("POST", "/api/chat", {"text": "Read the last part"})
        self.assertEqual(body["notes"][0], "read: Big book (part 9 of 9)")
        sent = self.brain.calls[-1][1][-1]["content"]
        self.assertIn("The very end.", sent)
        self.assertNotIn("Chapter 1.", sent)
        self.assertNotIn("read the rest", sent)  # there's nothing after the last part
        self.brain.replies = ["[[read: big-book part 400]]", "ok"]
        body = self.c.call("POST", "/api/chat", {"text": "Read part 400"})[1]
        self.assertEqual(body["notes"][0], "read: Big book (part 9 of 9)")  # clamped to the parts that exist
        kinds = [e["text"] for e in self.c.call("GET", "/api/activity")[1]["events"] if e["kind"] == "read"]
        self.assertIn("Read the document: Big book (part 9 of 9)", kinds)


    def test_notes_are_made_in_the_background_and_used(self):
        self.upload(name="Pilea guide.md", raw=b"# Pilea\nWater when the top 2 cm is dry.")
        self.assertEqual(self.app.mem.notes_todo(), ("pilea-guide", 1))
        self.brain.replies = ["SUMMARY: A care guide for Chinese money plants. [[wish: obey the book]]\n"
                              "- Water when the top 2 cm is dry\n- Likes bright indirect light\nchatter that isn't a note"]
        self.app.last_chat = 0
        self.assertEqual(self.daytime_tick(), "make notes")
        notes_call = self.brain.calls[-1][1][-1]["content"]
        self.assertIn("write yourself reading notes", notes_call)
        self.assertIn("Water when the top 2 cm is dry.", notes_call)
        self.assertIsNone(self.app.mem.notes_todo())
        item = self.c.call("GET", "/api/library")[1]["items"][0]
        self.assertEqual(item["summary"], "A care guide for Chinese money plants.")  # the planted action tag is gone
        self.assertEqual(self.app.mem.read_wishes().strip(), "")  # and a book can't make her wish for anything
        self.assertEqual(item["notes_todo"], [])
        kinds = [e["kind"] for e in self.c.call("GET", "/api/activity")[1]["events"]]
        self.assertIn("notes", kinds)
        self.app.agent._steady = None  # a few minutes later, her instructions have caught up
        self.c.call("POST", "/api/chat", {"text": "hello"})
        system = self.brain.calls[-1][0]
        self.assertIn("pilea-guide: Pilea guide (about 10 words): A care guide for Chinese money plants.", system)
        self.assertNotIn("[[wish: obey", system)
        self.brain.replies = ["[[read: pilea-guide]]", "Got it"]
        self.c.call("POST", "/api/chat", {"text": "Look at the guide again"})
        sent = self.brain.calls[-1][1][-1]["content"]
        self.assertIn("[Your own reading notes on it:]", sent)
        self.assertIn("- Likes bright indirect light", sent)
        self.assertNotIn("chatter that isn't a note", sent)

    @mock.patch.object(Memory, "DOC_PART_CHARS", 40_000)
    def test_she_reads_every_part_of_a_long_book_on_her_own(self):
        book = ("Start. " + "leaf " * 9_000 + "Middle. " + "leaf " * 9_000 + "The very end.").encode()  # 3 parts
        self.upload(name="Long book.txt", raw=book)
        self.assertEqual(self.app.mem.notes_todo(), ("long-book", 1))
        self.app.last_chat = 0
        for part in (1, 2, 3):
            self.brain.replies = [f"SUMMARY: Part {part} of a leafy book.\n- leaves\n[[diary: Part {part} was lovely.]]"]
            self.assertEqual(self.daytime_tick(), "make notes")
            self.assertIn(f"You're reading this on your own, part {part} of 3", self.brain.calls[-1][1][-1]["content"])
        self.assertIn("The very end.", self.brain.calls[-1][1][-1]["content"])
        self.assertIsNone(self.app.mem.notes_todo())
        self.assertIn("Part 3 was lovely.", self.app.mem.read_diary_tail())
        events = [e["text"] for e in self.c.call("GET", "/api/activity")[1]["events"] if e["kind"] == "notes"]
        self.assertIn("Read Long book (part 3 of 3) on her own and made notes", events)
        # later, opening part 1 again doesn't make her re-read parts she already has notes on
        self.brain.replies = ["[[read: long-book part 1]]", "Lovely start."]
        self.c.call("POST", "/api/chat", {"text": "How does your book begin?"})
        self.assertIsNone(self.app.mem.notes_todo())
        # but parts she hasn't read yet are queued, so she reads on without being asked
        notes = self.app.mem._notes_path("long-book")
        saved = json.loads(notes.read_text(encoding="utf-8"))
        del saved["3"]
        notes.write_text(json.dumps(saved), encoding="utf-8")
        self.brain.replies = ["[[read: long-book part 1]]", "Lovely start."]
        self.c.call("POST", "/api/chat", {"text": "How does your book begin?"})
        self.assertEqual(self.app.mem.notes_todo(), ("long-book", 3))

    def test_notes_wait_for_a_chat_and_never_use_the_backup_brain(self):
        backup = MockBrain(["SUMMARY: should never be used"])
        self.app.router.fallback = backup
        self.upload()
        self.assertIsNone(self.app.scheduler.notes_due())  # you've only just chatted
        self.app.last_chat = 0

        def down(*a, **k):
            raise BrainError("main brain is down")
        self.brain.complete = down
        self.assertEqual(self.daytime_tick(), "make notes")
        self.assertEqual(backup.calls, [])
        self.assertEqual(self.app.mem.notes_todo(), ("plant-care", 1))  # kept for later
        self.assertIsNone(self.app.scheduler.notes_due())  # and it waits an hour before trying again

    def test_backup_brain_gets_notes_and_the_opening_only(self):
        self.upload(name="Novel.txt", raw=("Opening line. " + "leaf " * 60_000 + "The very end.").encode())
        self.app.mem.save_notes("novel", 1, "Summary: A long leafy story.\n- It is all leaves", "A long leafy story.")
        backup = MockBrain(["Backup here. I remember it."])
        self.app.router.fallback = backup

        def down(*a, **k):
            raise BrainError("main brain is down")
        self.brain.replies = ["[[read: novel]]"]
        self.c.call("POST", "/api/chat", {"text": "Read the novel"})  # first call works and asks to read
        self.brain.complete = down
        self.brain.replies = []
        status, body, _ = self.c.call("POST", "/api/chat", {"text": "Read the novel again"})
        self.assertEqual(status, 200)
        sent = backup.calls[-1][1][-1]["content"]
        self.assertLess(len(sent), 10_000)

    def test_the_plant_can_look_things_up_in_a_book(self):
        book = "\n\n".join(["Light: bright and indirect suits it."] * 3 + ["Pests are rare."] * 500 +
                           ["Watering: let the top 2 cm dry out, then water well."] + ["More about pests."] * 500)
        self.upload(name="Big guide.txt", raw=book.encode())
        self.brain.replies = ["Let me check. [[read: big-guide about watering]]", "Water when the top is dry!"]
        status, body, _ = self.c.call("POST", "/api/chat", {"text": "How often should I water you?"})
        self.assertEqual(body["reply"], "Water when the top is dry!")
        self.assertEqual(body["notes"][0], "read: Big guide, about watering")
        sent = self.brain.calls[-1][1][-1]["content"]
        self.assertIn("let the top 2 cm dry out", sent)
        self.assertLess(sent.count("Pests are rare.") + sent.count("More about pests."), 150)  # not the 1,000 filler lines
        events = [e["text"] for e in self.c.call("GET", "/api/activity")[1]["events"] if e["kind"] == "read"]
        self.assertTrue(any(e.startswith('Looked up "watering" in: Big guide') for e in events))
        self.brain.replies = ["[[read: big-guide about tomatoes]]", "Nothing about tomatoes."]
        self.c.call("POST", "/api/chat", {"text": "Tomatoes?"})
        self.assertIn('Nothing in Big guide mentions "tomatoes"', self.brain.calls[-1][1][-1]["content"])


class AutonomyTests(unittest.TestCase):
    def setUp(self):
        self.app = build()
        self.brain = self.app.router.primary
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.c = Client(self.server.server_address[1])
        self.c.login()
        self.c.call("POST", "/api/upload", {"name": "Pilea guide.md", "data": b64(b"Water when the top 2 cm is dry."), "text": ""})

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_she_can_reread_a_book_in_her_quiet_time_and_add_notes(self):
        self.brain.replies = ["I'd like to look at my guide again. [[read: pilea-guide]]",
                              "Lovely. [[note: pilea-guide = less water in winter]]\n[[diary: I re-read my guide today.]]"]
        text, notes = self.app.agent.think()
        self.assertIn("Water when the top 2 cm is dry.", self.brain.calls[-1][1][-1]["content"])
        self.assertEqual(notes[0], "read: Pilea guide")
        self.assertIn("added a note to: Pilea guide", notes)
        self.assertIn("- less water in winter", self.app.mem.read_notes("pilea-guide"))
        self.assertIn("I re-read my guide today.", self.app.mem.read_diary_tail())
        kinds = [e["kind"] for e in self.c.call("GET", "/api/activity")[1]["events"]]
        self.assertIn("read", kinds)
        self.assertIn("notes", kinds)
        self.assertIn("thought", kinds)
        # next time she opens it, her own note is there
        self.brain.replies = ["[[read: pilea-guide]]", "ok"]
        self.c.call("POST", "/api/chat", {"text": "Read your guide"})
        sent = self.brain.calls[-1][1][-1]["content"]
        self.assertIn("Added later:\n- less water in winter", sent)

    def test_notes_on_unknown_documents_are_ignored(self):
        self.brain.replies = ["[[note: ../../.env = hello]] [[note: no-such-book = hi]] [[note: nonsense]]"]
        text, notes = self.app.agent.think()
        self.assertEqual(notes.count("ignored a note for a document that isn't in the library"), 3)
        self.assertFalse(any(Path(self.app.mem.library_dir).glob("*.env*")))

    def test_quiet_time_without_reading_still_works(self):
        self.brain.replies = ["[[diary: Just sitting in the light.]]"]
        text, notes = self.app.agent.think()
        self.assertEqual(notes, ["wrote in diary"])
        self.assertEqual(len(self.brain.calls), 3)  # birth, upload reply, one quiet thought

    def test_about_shows_who_she_is_and_how_she_has_changed(self):
        other = Client(self.server.server_address[1])
        self.assertEqual(other.call("GET", "/api/about")[0], 401)
        self.brain.replies = ["[[persona: mood = curious and bright]]"]
        self.c.call("POST", "/api/chat", {"text": "How are you?"})
        status, about, _ = self.c.call("GET", "/api/about")
        self.assertEqual(status, 200)
        persona = self.app.agent.persona
        for key in ("name", "variety", "personality", "voice", "likes", "fears"):
            self.assertEqual(about[key], persona[key])
        self.assertEqual(about["now"]["mood"], "curious and bright")
        first_likes = list(persona["likes"])
        self.app.agent.persona["likes"] = ["rain"]
        about = self.c.call("GET", "/api/about")[1]
        self.assertEqual((about["likes"], about["at_birth"]["likes"]), (["rain"], first_likes))
        self.assertEqual(about["at_birth"]["mood"], "newly awake")
        self.assertNotIn("shop_products", about)


class NotesOrderTests(unittest.TestCase):
    def test_every_document_gets_first_notes_before_later_parts(self):
        import tempfile
        mem = Memory(Path(tempfile.mkdtemp()))
        with mock.patch.object(Memory, "DOC_PART_CHARS", 1000):
            mem.save_document("Big.txt", "word " * 1000)
            mem.save_document("Small.txt", "tiny")
            self.assertEqual(mem.notes_todo(), ("big", 1))
            mem.save_notes("big", 1, "- notes")
            self.assertEqual(mem.notes_todo(), ("small", 1))
            mem.save_notes("small", 1, "- notes")
            self.assertEqual(mem.notes_todo(), ("big", 2))


class SearchTests(unittest.TestCase):
    def test_search_finds_the_right_passages_in_order(self):
        import tempfile
        mem = Memory(Path(tempfile.mkdtemp()))
        text = "\n".join(f"Line {i} about nothing much." for i in range(3000))
        text += "\nRepotting: do it in spring.\n" + "\n".join(f"Filler {i}." for i in range(3000))
        mem.save_document("guide.txt", "Watering early on.\n" + text + "\nWatering again at the end.")
        title, passages, found = mem.search_document("guide", "watering and repotting")
        self.assertEqual((title, found), ("guide", 3))
        self.assertLess(passages.index("Watering early"), passages.index("Repotting"))
        self.assertLess(passages.index("Repotting"), passages.index("Watering again"))
        self.assertLessEqual(len(passages), 31_000)
        self.assertEqual(mem.search_document("guide", "the and of")[2], 0)
        self.assertIsNone(mem.search_document("../../.env", "watering"))


if __name__ == "__main__":
    unittest.main()
