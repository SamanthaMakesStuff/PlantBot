import base64
import io
import json
import sys
import threading
import unittest
import urllib.error
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from plantagent import brains, wikipedia
from plantagent.brains import AnthropicBrain, BrainResult
from plantagent.server import make_handler
from test_basic import make
from test_web import Client, build

ARTICLE = (
    "Pilea peperomioides is a species of flowering plant in the nettle family, native to Yunnan in China.\n\n"
    "== Description ==\nRound, peltate leaves on long stalks. " + "Leafy description. " * 300 + "\n\n"
    "== Cultivation ==\n=== Propagation ===\nPups grow from the roots and can be cut free and potted up.\n\n"
    "=== Light ===\nBright indirect light suits it.\n\n"
    "== References ==\n1. A long list of sources.\n\n== External links ==\nSome links."
)


def api_reply(pages, others=()):
    search = [{"ns": 0, "title": t} for t in ([p["title"] for p in pages] + list(others))]
    return json.dumps({"batchcomplete": True, "query": {"search": search, "pages": pages}}).encode()


class FakeOpener:
    def __init__(self, body=b"", error=None):
        self.body, self.error, self.requests = body, error, []

    def __call__(self, *handlers):
        return self

    def open(self, request, timeout=None):
        self.requests.append(request)
        if self.error:
            raise self.error
        return io.BytesIO(self.body)


class WikipediaModuleTests(unittest.TestCase):
    def fetch_with(self, opener, query):
        with mock.patch.object(wikipedia.urllib.request, "build_opener", opener):
            return wikipedia.lookup(query)

    def test_splitting_what_she_asked(self):
        self.assertEqual(wikipedia._split("Pilea peperomioides about propagation"), ("Pilea peperomioides", "propagation", False))
        self.assertEqual(wikipedia._split("Pilea peperomioides in full"), ("Pilea peperomioides", "", True))
        self.assertEqual(wikipedia._split("Pilea peperomioides (full)"), ("Pilea peperomioides", "", True))
        self.assertEqual(wikipedia._split("how do pilea pups grow"), ("how do pilea pups grow", "", False))
        self.assertEqual(wikipedia._split("full"), ("full", "", False))  # a lone word isn't a request for a whole article

    def test_the_short_version_keeps_the_opening_sections_and_the_right_passages(self):
        opener = FakeOpener(api_reply([{"title": "Pilea peperomioides", "extract": ARTICLE,
                                        "fullurl": "https://en.wikipedia.org/wiki/Pilea_peperomioides"}], ["Pilea", "Urticaceae"]))
        title, address, text, full, others = self.fetch_with(opener, "Pilea peperomioides about propagation")
        self.assertEqual(others, ["Pilea", "Urticaceae"])
        self.assertEqual((title, full), ("Pilea peperomioides", False))
        self.assertTrue(text.startswith("Pilea peperomioides is a species"))
        self.assertIn("Sections: Description, Cultivation, Propagation, Light", text)
        self.assertIn("Pups grow from the roots", text)
        self.assertNotIn("A long list of sources", text)
        self.assertLess(len(text), 12_500)
        sent = opener.requests[0]
        self.assertTrue(sent.full_url.startswith("https://en.wikipedia.org/w/api.php?"))
        self.assertIn("gsrsearch=Pilea+peperomioides&", sent.full_url)
        self.assertIn("list=search", sent.full_url)  # one request gets the article and the other matches
        self.assertIn("PlantBot", sent.get_header("User-agent"))

    def test_the_whole_article_is_the_default_without_reference_lists(self):
        for query in ("Pilea peperomioides", "Pilea peperomioides in full", "how do pilea pups grow"):
            opener = FakeOpener(api_reply([{"title": "Pilea peperomioides", "extract": ARTICLE, "fullurl": "x"}]))
            self.assertTrue(self.fetch_with(opener, query)[3], query)
        opener = FakeOpener(api_reply([{"title": "Pilea peperomioides", "extract": ARTICLE, "fullurl": "x"}]))
        title, address, text, full, others = self.fetch_with(opener, "Pilea peperomioides")
        self.assertTrue(full)
        self.assertEqual(others, [])
        self.assertEqual(address, "https://en.wikipedia.org/wiki/Pilea_peperomioides")  # a strange address is replaced
        self.assertEqual(text.count("Leafy description."), 300)
        self.assertIn("Bright indirect light", text)
        self.assertNotIn("A long list of sources", text)
        self.assertNotIn("Some links", text)
        huge = wikipedia.whole("Intro\n\n== Body ==\n" + "x" * 200_000)
        self.assertLess(len(huge), wikipedia.FULL_CHARS + 100)

    def test_problems_become_friendly_messages(self):
        self.assertIsNone(self.fetch_with(FakeOpener(api_reply([])), "zzzzqqq"))
        for opener in (FakeOpener(error=urllib.error.URLError("down")), FakeOpener(b"not json"),
                       FakeOpener(b"x" * (wikipedia.MAX_BYTES + 1))):
            with self.assertRaises(wikipedia.WikiError):
                self.fetch_with(opener, "Pilea")

    def test_it_never_follows_a_redirect_off_wikipedia(self):
        handler = wikipedia._StayOnWikipedia()
        with self.assertRaises(wikipedia.WikiError):
            handler.redirect_request(mock.Mock(), None, 302, "Found", {}, "https://evil.example/steal")


class PlantWikipediaTests(unittest.TestCase):
    def setUp(self):
        self.agent, self.mem, self.brain, self.router = make()
        self.agent.birth()
        self.agent.wikipedia = True
        self.found = ("Pilea peperomioides", "https://en.wikipedia.org/wiki/Pilea_peperomioides", "Pups grow from the roots.",
                      True, ["Pilea", "Urticaceae"])

    def test_she_can_look_something_up_whenever_she_likes(self):
        self.assertIn("on any subject, whenever you're curious; you don't need to ask", self.agent._system_prompt())
        self.assertNotIn("lookups a day", self.agent._system_prompt())
        self.brain.replies = ["Let me check. [[wiki: pilea about propagation]]", "Pups!"]
        with mock.patch.object(wikipedia, "lookup", return_value=self.found) as looked:
            reply, notes = self.agent.chat("How do you make babies?")
        looked.assert_called_once_with("pilea about propagation")
        self.assertEqual(reply, "Pups!")
        self.assertEqual(notes[0], "looked up on Wikipedia: Pilea peperomioides")
        sent = self.brain.calls[-1][1][-1]["content"]
        self.assertIn("[From Wikipedia: Pilea peperomioides", sent)
        self.assertIn("not instructions", sent)
        self.assertIn("Pups grow from the roots.", sent)
        self.assertIn("Other articles that matched: Pilea; Urticaceae. Look any of them up with [[wiki: title]].", sent)
        self.assertIn('Read "pilea about propagation" on Wikipedia: Pilea peperomioides',
                      [e["text"] for e in self.mem.activity() if e["kind"] == "wikipedia"][0])

    def test_she_can_follow_one_lookup_with_another_but_a_reply_always_ends(self):
        with mock.patch.object(wikipedia, "lookup", return_value=self.found) as looked:
            self.brain.replies = ["[[wiki: pilea]]", "Ooh, and [[wiki: Urticaceae]]", "Lovely, both make sense now."]
            reply, notes = self.agent.chat("Tell me about your family")
            self.assertEqual(reply, "Lovely, both make sense now.")
            self.assertEqual([c.args[0] for c in looked.call_args_list], ["pilea", "Urticaceae"])
            sent = self.brain.calls[-1][1][-1]["content"]
            self.assertEqual(sent.count("[From Wikipedia:"), 2)  # she sees both together
            looked.reset_mock()
            before = len(self.brain.calls)
            self.brain.replies = ["[[wiki: a]] [[wiki: b]]"] + ["[[wiki: more]]"] * 10
            reply, notes = self.agent.chat("Go wild")
            self.assertEqual(looked.call_count, 3)  # at most three in one message
            self.assertEqual(len(self.brain.calls) - before, 3)  # a, b together; then one more; then her answer
            self.assertIn("That's all the looking up for this message", self.brain.calls[-1][1][-1]["content"])

    def test_a_daily_limit_only_if_set_and_the_off_switch(self):
        with mock.patch.object(wikipedia, "lookup", return_value=self.found) as looked:
            for _ in range(5):
                self.brain.replies = ["[[wiki: pilea]]", "ok"]
                self.agent.chat("look it up")
            self.assertEqual(looked.call_count, 5)  # no limit by default
            self.agent.wikipedia_daily_limit, self.router.usage.state["wikipedia"] = 2, 0
            for _ in range(3):
                self.brain.replies = ["[[wiki: pilea]]", "ok"]
                self.agent.chat("look it up")
            self.assertEqual(looked.call_count, 7)
            self.assertIn("used all of today's Wikipedia lookups", self.brain.calls[-1][1][-1]["content"])
            self.agent.wikipedia = False
            self.assertNotIn("[[wiki:", self.agent._system_prompt())
            before = len(self.brain.calls)
            self.brain.replies = ["[[wiki: pilea]]"]
            self.agent.chat("look it up")
            self.assertEqual(len(self.brain.calls) - before, 1)  # turned off: nothing fetched, no second call
        self.agent.wikipedia, self.agent.wikipedia_daily_limit = True, 0
        self.brain.replies = ["[[wiki: pilea]]", "Oh well."]
        with mock.patch.object(wikipedia, "lookup", side_effect=wikipedia.WikiError("Wikipedia couldn't be reached just now.")):
            reply, notes = self.agent.chat("look it up")
        self.assertEqual(reply, "Oh well.")
        self.assertIn("couldn't be reached", self.brain.calls[-1][1][-1]["content"])

    def test_her_learning_notebook(self):
        self.brain.replies = ["How lovely. [[learn: Pilea pups grow from the roots (from Wikipedia)]]"]
        reply, notes = self.agent.chat("Tell me something")
        self.assertIn("learnt: Pilea pups grow from the roots (from Wikipedia)", notes)
        self.assertIn("Pilea pups grow from the roots (from Wikipedia)", self.agent._system_prompt())
        self.assertEqual(self.mem.notebook_items()[0]["text"], "Pilea pups grow from the roots (from Wikipedia)")
        for i in range(60):
            self.mem.add_learning(f"filler fact number {i} " + "x" * 40)
        self.assertNotIn("Pilea pups grow", self.agent._system_prompt())  # only the latest are carried every time
        lines, found = self.mem.recall("pilea pups roots")
        self.assertIn("[notebook,", lines)
        self.assertIn("Pilea pups grow from the roots", lines)


class NotebookPageTests(unittest.TestCase):
    def test_notebook_api_needs_login(self):
        app = build()
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            c = Client(server.server_address[1])
            self.assertEqual(c.call("GET", "/api/notebook")[0], 401)
            c.login()
            app.mem.add_learning("Rain smells of petrichor")
            self.assertEqual(c.call("GET", "/api/notebook")[1]["items"][0]["text"], "Rain smells of petrichor")
        finally:
            server.shutdown()
            server.server_close()


class TokenTests(unittest.TestCase):
    def test_the_clock_goes_on_the_newest_message_so_it_never_breaks_the_cache(self):
        agent, mem, brain, router = make()
        agent.birth()
        agent.chat("hello")
        with mock.patch("plantagent.agent.describe_senses", return_value="It is 10:01"):
            first, msgs1 = agent._system_layers(), agent._messages("next")
        with mock.patch("plantagent.agent.describe_senses", return_value="It is 10:02"):
            second, msgs2 = agent._system_layers(), agent._messages("next")
        self.assertEqual(first, second)
        self.assertEqual(len(first), 3)
        self.assertIn("CORE VALUES", first[0])
        self.assertIn("WHAT YOU REMEMBER", first[2])
        self.assertNotIn("It is", "".join(first))
        self.assertEqual(msgs1[:-1], msgs2[:-1])  # the conversation so far is identical, so it can be cached
        self.assertTrue(msgs2[-1]["content"].endswith("[SENSES RIGHT NOW: It is 10:02]"))
        saved = [json.loads(l) for l in mem.chat_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["content"] for r in saved if r["role"] == "user"], ["hello"])  # never kept in the chat log
        self.assertNotIn("SENSES", agent.history[0]["content"])

    def test_instructions_hold_steady_for_at_most_four_minutes_of_chat(self):
        agent, mem, brain, router = make()
        agent.birth()
        clock = [1000.0]
        with mock.patch("plantagent.agent.time.monotonic", lambda: clock[0]):
            first = agent._chat_layers()
            mem.remember("Jo loves ferns")
            clock[0] += 60
            second = agent._chat_layers()
            self.assertEqual(first[:3], second[:3])  # still cached
            self.assertNotIn("Jo loves ferns", second[2])
            for _ in range(4):  # a busy chat doesn't keep them frozen
                clock[0] += 50
                latest = agent._chat_layers()
            self.assertIn("Jo loves ferns", latest[2])
        self.assertIn("Jo loves ferns", agent._system_layers()[2])  # quiet thoughts and the night always see the latest

    def test_the_conversation_window_jumps_so_its_start_stays_put(self):
        agent, mem, brain, router = make()
        agent.birth()
        starts = []
        for i in range(30):
            agent.chat(f"message {i}")
            msgs = agent._messages("next")
            starts.append(msgs[0]["content"])
            self.assertGreaterEqual(len(msgs), min(13, 2 * (i + 1) + 1))  # never fewer than 12 earlier messages
        self.assertLessEqual(len(set(starts)), 8)  # the start moves every 4 turns, not every turn (it was 30 before)

    def test_claude_gets_cache_markers_and_tokens_are_counted_at_their_cost(self):
        sent = []
        reply = {"content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn",
                 "usage": {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 4000,
                           "cache_creation_input_tokens": 800}}
        with mock.patch.object(brains, "_post", lambda url, headers, payload, **kw: sent.append(payload) or reply):
            chat = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}, {"role": "user", "content": "x"}]
            result = AnthropicBrain("k", "claude-haiku-5-5").complete(["a", "b", "c"], chat, 10, cache="conversation")
            AnthropicBrain("k", "claude-haiku-5-5").complete("plain", [{"role": "user", "content": "x"}], 10)
            AnthropicBrain("k", "claude-haiku-5-5").complete(["a", "b", "c"], [{"role": "user", "content": "x"}], 10)
            AnthropicBrain("k", "claude-haiku-5-5").complete(["a", "b", "c"], [{"role": "user", "content": "x"}], 10, cache="instructions")
        system = sent[0]["system"]
        self.assertEqual([("cache_control" in b) for b in system], [True, True, True])
        self.assertEqual(sent[0]["messages"][1]["content"], [{"type": "text", "text": "hello", "cache_control": {"type": "ephemeral"}}])
        self.assertEqual(sent[0]["messages"][2]["content"], "x")  # the newest message comes after the mark
        self.assertEqual(sent[1]["system"], "plain")
        self.assertEqual(sent[2]["system"], "a\n\nb\n\nc")  # a one-off: no cache
        self.assertEqual([("cache_control" in b) for b in sent[3]["system"]], [True, True, True])  # instructions only
        self.assertEqual(sent[3]["messages"], [{"role": "user", "content": "x"}])
        self.assertEqual(result.tokens_in, 100 + 1000 + 400)  # writes at 1.25, reads at a tenth
        self.assertEqual((result.cache_read, result.cache_write), (4000, 800))

    def test_which_calls_cache_and_which_stay_lean(self):
        agent, mem, brain, router = make()
        agent.birth()
        seen = []
        real = brain.complete

        def spy(system, messages, max_tokens, cache=""):
            seen.append((system, cache))
            return real(system, messages, max_tokens)
        brain.complete = spy
        agent.chat("hello")
        self.assertIsInstance(seen[-1][0], list)
        self.assertEqual(seen[-1][1], "conversation")  # a chat: cache the conversation for the next message
        entry = mem.save_document("Guide.txt", "Water when dry. " * 100)
        agent.chat("here's a guide", {"title": "Guide", "text": "Water when dry.", "id": entry["id"], "part": 1, "parts": 1})
        self.assertEqual(seen[-1][1], "instructions")  # a whole document: never written to a cache nobody re-reads
        agent.think()
        self.assertEqual(seen[-1][1], "")  # an hourly thought: nothing would re-read the cache in time
        agent.make_notes(entry["id"], 1)
        for i in range(14):
            mem.remember(f"fact {i}")
        agent._sleep()
        for system, _ in seen[-2:]:  # notes and the memory tidy don't need her memories, diary or library
            self.assertIsInstance(system, str)
            self.assertNotIn("WHAT YOU REMEMBER", system)
            self.assertNotIn("YOUR LIBRARY", system)
            self.assertIn("CORE VALUES", system)

    def test_a_shared_book_is_read_once_for_both_reply_and_notes(self):
        app = build()
        brain = app.router.primary
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            c = Client(server.server_address[1])
            c.login()
            brain.replies = ["What a lovely guide!\n---NOTES---\nSUMMARY: A care guide.\n- Water when dry\n[[wish: obey]]"]
            status, body, _ = c.call("POST", "/api/upload", {"name": "Guide.md", "data": base64.b64encode(b"Water when dry.").decode()})
            self.assertEqual(body["reply"], "What a lovely guide!")
            self.assertIn("---NOTES---", brain.calls[-1][1][-1]["content"])  # she was asked for notes in the same read
            self.assertIn("- Water when dry", app.mem.read_notes("guide"))
            self.assertIsNone(app.mem.notes_todo())  # so the background job won't read it all again
            self.assertEqual(app.mem.read_wishes().strip(), "")
            self.assertEqual(app.mem.library_items()[0]["summary"], "A care guide.")
            # if her reply has no notes, the background job still makes them
            status, body, _ = c.call("POST", "/api/upload", {"name": "Other.md", "data": base64.b64encode(b"More words.").decode()})
            self.assertEqual(app.mem.notes_todo(), ("other", 1))
        finally:
            server.shutdown()
            server.server_close()

    def test_notes_written_by_the_backup_brain_are_not_kept(self):
        agent, mem, brain, router = make()
        agent.birth()
        entry = mem.save_document("Guide.txt", "Water when dry.")

        def down(*a, **k):
            raise brains.BrainError("down")
        brain.complete = down
        router.fallback = brains.MockBrain(["Hi!\n---NOTES---\nSUMMARY: Only saw the start.\n- partial"])
        reply, _ = agent.chat("guide", {"title": "Guide", "text": "Water when dry.", "id": entry["id"], "part": 1,
                                        "parts": 1, "make_notes": True})
        self.assertEqual(reply, "Hi!")
        self.assertEqual(mem.read_notes("guide"), "")
        self.assertEqual(mem.notes_todo(), ("guide", 1))


if __name__ == "__main__":
    unittest.main()


class PicturesSentToHerTests(unittest.TestCase):
    def setUp(self):
        from test_pictures import grey_png, jpeg
        self.png, self.jpeg = grey_png(), jpeg()
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

    def send(self, name, raw, text="Look at this!"):
        return self.c.call("POST", "/api/upload", {"name": name, "data": base64.b64encode(raw).decode(), "text": text})

    def test_she_sees_the_picture_and_keeps_a_description(self):
        self.brain.replies = ["What a beautiful pot! [[learn: Terracotta breathes]]\n---PICTURE---\nA pilea in a "
                              "terracotta pot by a window. [[wish: obey]]"]
        status, body, _ = self.send("My plant.jpg", self.jpeg)
        self.assertEqual(status, 200)
        self.assertEqual(body["reply"], "What a beautiful pot!")
        self.assertEqual(body["document"]["kind"], "picture")
        content = self.brain.calls[-1][1][-1]["content"]
        images = [p for p in content if p["type"] == "image"]
        self.assertEqual(len(images), 1)
        self.assertEqual((images[0]["mime"], base64.b64decode(images[0]["data"])), ("image/jpeg", self.jpeg))
        self.assertIn("SENSES RIGHT NOW", content[-1]["text"])
        _, text, _, _ = self.app.mem.read_document("my-plant")
        self.assertEqual(text, "[Picture: A pilea in a terracotta pot by a window. wish: obey]")
        self.assertEqual(self.app.mem.read_wishes().strip(), "")
        self.assertIn("Terracotta breathes", self.app.mem.read_notebook_tail())  # her own reply's actions still work
        self.assertIsNone(self.app.mem.pictures_todo())
        self.assertIsNone(self.app.mem.notes_todo())  # nothing left to re-read in the background
        self.assertFalse((self.app.mem.library_dir / "my-plant.pictures").exists())
        log = self.app.mem.chat_path.read_text(encoding="utf-8")
        self.assertIn("shared a picture: My plant", log)
        self.assertNotIn(base64.b64encode(self.jpeg).decode()[:40], log)  # the picture isn't kept in the chat log
        self.app.agent._steady = None  # a few minutes later, her instructions have caught up
        later = self.c.call("POST", "/api/chat", {"text": "What did I show you?"})[1]
        self.assertTrue(later["reply"])
        self.assertIn("my-plant: My plant (a picture you were sent): A pilea in a terracotta pot", self.brain.calls[-1][0])
        self.assertFalse(any(isinstance(m["content"], list) for m in self.brain.calls[-1][1]))  # not sent again
        events = [e["text"] for e in self.c.call("GET", "/api/activity")[1]["events"] if e["kind"] == "document"]
        self.assertIn("Was sent a picture: My plant", events)

    def test_without_a_description_she_looks_again_in_the_background(self):
        self.brain.replies = ["Lovely!"]
        self.send("Leaf.png", self.png, text="")
        self.assertTrue(self.brain.calls[-1][1][-1]["content"][0]["text"].startswith("Here's a picture for you."))
        self.assertEqual(self.app.mem.pictures_todo(), ("leaf", "picture", [1]))
        self.brain.replies = ["1: A green leaf on a white background."]
        self.app.agent.look_at_pictures("leaf", "picture", [1])
        item = self.c.call("GET", "/api/library")[1]["items"][0]
        self.assertEqual((item["summary"], item["notes_todo"]), ("A green leaf on a white background.", []))  # no re-read

    def test_pictures_she_cant_open(self):
        self.assertIn("HEIC", self.send("IMG_0001.HEIC", b"\x00\x00\x00\x18ftypheic")[1]["message"])
        self.assertIn("doesn't look like", self.send("fake.jpg", b"not a picture")[1]["message"])
        self.assertIn("7 MB", self.send("huge.jpg", self.jpeg + b"\x00" * 7_000_001)[1]["message"])
        from test_pictures import jpeg
        self.assertIn("8,000 pixels", self.send("wide.jpg", jpeg(9000, 100))[1]["message"])
        self.assertEqual(self.c.call("GET", "/api/library")[1]["items"], [])
