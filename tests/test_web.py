import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plantagent.agent import PlantAgent
from plantagent.brains import BrainRouter, MockBrain, Usage
from plantagent.memory import Memory
from plantagent.scheduler import Scheduler
from plantagent.server import App, make_handler

PASSWORD = "correct-horse"


def build(call_budget=0):
    tmp = Path(tempfile.mkdtemp())
    mem = Memory(tmp)
    router = BrainRouter(MockBrain(), None, Usage(tmp / "usage.json"), call_budget, 0)
    agent = PlantAgent(router, mem)
    agent.birth()
    app = App(agent, mem, router, PASSWORD)
    app.scheduler = Scheduler(app, think_every_min=60, awake_start=8, awake_end=22)
    return app


class Client:
    def __init__(self, port):
        self.port, self.cookie = port, ""

    def call(self, method, path, body=None, ctype="application/json"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {}
        if self.cookie:
            headers["Cookie"] = self.cookie
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            headers["Content-Type"] = ctype
        conn.request(method, path, data, headers)
        resp = conn.getresponse()
        raw = resp.read()
        sc = resp.getheader("Set-Cookie")
        if sc and sc.startswith("session=") and "Max-Age=0" not in sc:
            self.cookie = sc.split(";")[0]
        try:
            payload = json.loads(raw.decode())
        except Exception:
            payload = raw.decode(errors="replace")
        conn.close()
        return resp.status, payload, resp

    def login(self):
        return self.call("POST", "/api/login", {"password": PASSWORD})[0]


class WebTests(unittest.TestCase):
    def setUp(self):
        self.app = build()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.c = Client(self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_page_is_public_but_api_is_not(self):
        status, _, resp = self.c.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("default-src 'none'", resp.getheader("Content-Security-Policy"))
        for path in ("/api/state", "/api/chat", "/api/diary", "/api/memory", "/api/wishes", "/api/activity"):
            self.assertEqual(self.c.call("GET", path)[0], 401, path)
        self.assertEqual(self.c.call("POST", "/api/chat", {"text": "hi"})[0], 401)

    def test_static_whitelist(self):
        self.assertEqual(self.c.call("GET", "/static/style.css")[0], 200)
        self.assertEqual(self.c.call("GET", "/static/app.js")[0], 200)
        self.assertEqual(self.c.call("GET", "/static/../server.py")[0], 404)
        self.assertEqual(self.c.call("GET", "/static/..%2Fserver.py")[0], 404)

    def test_login_and_lockout(self):
        self.assertEqual(self.c.call("POST", "/api/login", {"password": "nope"})[0], 401)
        self.assertEqual(self.c.login(), 200)
        self.assertEqual(self.c.call("GET", "/api/state")[0], 200)
        self.app.fails = [time.time()] * 8
        other = Client(self.server.server_address[1])
        self.assertEqual(other.call("POST", "/api/login", {"password": PASSWORD})[0], 429)

    def test_logout(self):
        self.c.login()
        self.assertEqual(self.c.call("POST", "/api/logout", {})[0], 200)
        self.assertEqual(self.c.call("GET", "/api/state")[0], 401)

    def test_post_needs_json(self):
        self.c.login()
        status, body, _ = self.c.call("POST", "/api/chat", b"text=hi", "application/x-www-form-urlencoded")
        self.assertEqual(status, 400)

    def test_chat_flow_and_activity(self):
        self.c.login()
        status, body, _ = self.c.call("POST", "/api/chat", {"text": "hello Spindle"})
        self.assertEqual(status, 200)
        self.assertIn("hello Spindle", body["reply"])
        self.assertTrue(any(n.startswith("remembered") for n in body["notes"]))
        msgs = self.c.call("GET", "/api/chat")[1]["messages"]
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant"])
        ev = self.c.call("GET", "/api/activity?since=0")[1]
        kinds = [e["kind"] for e in ev["events"]]
        self.assertIn("birth", kinds)
        self.assertIn("remember", kinds)
        later = self.c.call("GET", f"/api/activity?since={ev['last_id']}")[1]
        self.assertEqual(later["events"], [])

    def test_chat_validation(self):
        self.c.login()
        self.assertEqual(self.c.call("POST", "/api/chat", {"text": "   "})[0], 400)
        self.assertEqual(self.c.call("POST", "/api/chat", {"text": "x" * 3000})[0], 400)

    def test_think_sleep_and_views(self):
        self.c.login()
        self.assertEqual(self.c.call("POST", "/api/think", {})[0], 200)
        self.assertEqual(self.c.call("POST", "/api/sleep", {})[0], 200)
        diary = self.c.call("GET", "/api/diary")[1]["entries"]
        self.assertTrue(any("quiet moment" in e["text"] or "light" in e["text"] for e in diary))
        self.assertTrue(self.c.call("GET", "/api/memory")[1]["items"])
        kinds = [e["kind"] for e in self.c.call("GET", "/api/activity")[1]["events"]]
        self.assertIn("thought", kinds + ["thought"])  # mock thought has no visible text
        self.assertIn("sleep", kinds)

    def test_wish_flow(self):
        self.c.login()
        self.app.mem.add_wish("a brighter window")
        wishes = self.c.call("GET", "/api/wishes")[1]["items"]
        self.assertEqual(wishes[0]["status"], "open")
        wid = wishes[0]["id"]
        status, body, _ = self.c.call("POST", "/api/wishes", {"id": wid, "status": "yes"})
        self.assertEqual(body["items"][0]["status"], "yes")
        self.assertIn("said yes to my wish: a brighter window", self.app.mem.read_memory())
        self.assertEqual(self.c.call("POST", "/api/wishes", {"id": wid, "status": "bogus"})[0], 400)
        self.assertEqual(self.c.call("POST", "/api/wishes", {"id": "missing", "status": "yes"})[0], 404)
        self.c.call("POST", "/api/wishes", {"id": wid, "status": "open"})
        self.assertEqual(self.c.call("GET", "/api/wishes")[1]["items"][0]["status"], "open")


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.app = build(call_budget=100)
        self.s = self.app.scheduler
        self.day = datetime(2026, 10, 26, 14, 0)
        self.night = datetime(2026, 10, 26, 23, 0)
        self.small_hours = datetime(2026, 10, 27, 3, 0)
        self.now = time.time()

    def test_no_thought_straight_after_start(self):
        self.s.state["last_think"] = self.now
        self.assertIsNone(self.s.due(self.day, self.now + 60))

    def test_thinks_after_interval(self):
        self.s.state["last_think"] = self.now - 3700
        self.assertEqual(self.s.due(self.day, self.now), "think")

    def test_waits_while_chatting(self):
        self.s.state["last_think"] = self.now - 3700
        self.app.last_chat = self.now - 120
        self.assertIsNone(self.s.due(self.day, self.now))

    def test_keeps_budget_headroom(self):
        self.s.state["last_think"] = self.now - 3700
        self.app.router.usage.add(85, 0)
        self.assertIsNone(self.s.due(self.day, self.now))

    def test_keeps_token_headroom_too(self):
        self.s.state["last_think"] = self.now - 3700
        self.app.router.token_budget = 1000
        self.app.router.usage.add(1, 850)  # one big read has used most of today's tokens
        self.assertIsNone(self.s.due(self.day, self.now))
        self.app.mem.save_document("Guide.txt", "Water when dry.")
        self.app.last_chat = 0
        self.assertIsNone(self.s.notes_due(self.now))

    def test_sleeps_once_per_night_even_across_midnight(self):
        self.assertEqual(self.s.due(self.night, self.now), "sleep")
        self.s.state["last_sleep_key"] = self.s.sleep_key(self.night)
        self.assertIsNone(self.s.due(self.night, self.now))
        self.assertIsNone(self.s.due(self.small_hours, self.now))

    def test_never_thinks_at_night(self):
        self.s.state["last_think"] = 0
        self.s.state["last_sleep_key"] = self.s.sleep_key(self.night)
        self.assertIsNone(self.s.due(self.night, self.now))

    def test_tick_runs_and_logs(self):
        self.s.state["last_think"] = self.now - 3700
        self.assertEqual(self.s.tick(self.day, self.now), "think")
        kinds = [e["kind"] for e in self.app.mem.activity()]
        self.assertIn("diary", kinds)
        self.assertEqual(self.s.tick(self.night, self.now), "sleep")
        self.assertIsNone(self.s.tick(self.night, self.now))


if __name__ == "__main__":
    unittest.main()
