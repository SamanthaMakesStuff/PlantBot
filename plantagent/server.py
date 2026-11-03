"""A small private web server: chat with the plant and watch its life.

Standard library only. Everything except the login page needs the password.
"""
from __future__ import annotations

import base64
import binascii
import hmac
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .brains import BrainError, BudgetExceeded
from .documents import check_picture, extract_document, is_picture
from .pictures import marker

WEB_DIR = Path(__file__).with_name("web")
STATIC = {"style.css": "text/css; charset=utf-8", "app.js": "application/javascript; charset=utf-8"}
MAX_BODY = 20_000
MAX_UPLOAD_BODY = 68_000_000  # a 50 MB file grows by a third when sent as base64
MAX_CHAT_CHARS = 2000
COOKIE = "session"
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


class App:
    """Everything the request handlers share."""

    def __init__(self, agent, memory, router, password: str) -> None:
        self.agent, self.mem, self.router = agent, memory, router
        self.password = password
        self.lock = threading.RLock()  # anything that talks to the brain takes this
        self.sessions: set = set()
        self.fails: list = []
        self.last_chat = 0.0
        self.scheduler = None

    def check_password(self, attempt: str):
        """True/False, or None if too many wrong attempts recently."""
        now = time.time()
        self.fails = [t for t in self.fails if now - t < 300]
        if len(self.fails) >= 8:
            return None
        ok = hmac.compare_digest(attempt.encode("utf-8"), self.password.encode("utf-8"))
        if not ok:
            self.fails.append(now)
            time.sleep(0.4)
        return ok

    def about(self) -> dict:
        """Who she is: what she chose at birth, and what she has changed about herself since."""
        p, born = self.agent.persona, self.mem.load_baseline()
        keep = ("name", "variety", "born", "personality", "voice", "why_this_plant", "likes", "fears", "first_words")
        changeable = ("mood", "interests", "goals", "voice_notes")
        grown = ("personality", "voice", "likes", "fears")  # what her nightly reflection can update
        return {**{k: p.get(k, "") for k in keep},
                "now": {k: p.get(k, "") for k in changeable},
                "at_birth": {k: born.get(k, "") for k in changeable + grown}}

    def state(self) -> dict:
        p = self.agent.persona
        u = self.router.usage.snapshot()
        sched = self.scheduler.status() if self.scheduler else {"night": False, "next_thought_in_minutes": None}
        return {
            "name": p["name"], "variety": p["variety"], "born": p["born"], "mood": p.get("mood", ""),
            "interests": p.get("interests", ""), "goals": p.get("goals", ""),
            "calls": u["calls"], "tokens": u["tokens"],
            "call_budget": self.router.call_budget, "token_budget": self.router.token_budget,
            "brain": getattr(self.router.primary, "name", "?"),
            "last_brain_error": self.router.last_error,
            **sched,
        }


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PlantWeb"

        def log_message(self, *args):  # keep the terminal quiet
            pass

        # ---------------------------------------------------------- helpers
        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj, extra: dict | None = None) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", extra)

        def _file(self, name: str, ctype: str) -> None:
            try:
                self._send(200, (WEB_DIR / name).read_bytes(), ctype)
            except OSError:
                self._json(404, {"message": "Not found."})

        def _token(self) -> str:
            for part in self.headers.get("Cookie", "").split(";"):
                key, _, value = part.strip().partition("=")
                if key == COOKIE:
                    return value
            return ""

        def _authed(self) -> bool:
            token = self._token()
            return bool(token) and token in app.sessions

        def _read_json(self, limit: int = MAX_BODY) -> dict:
            if "application/json" not in self.headers.get("Content-Type", ""):
                raise ValueError("Send JSON.")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length > limit:
                raise ValueError("Request too large.")
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object.")
            return data

        def _brain_call(self, fn):
            """Runs something that uses the brain, turning failures into friendly JSON."""
            try:
                with app.lock:
                    return fn()
            except BudgetExceeded:
                self._json(429, {"message": "Resting. Today's call budget is used up and no fallback brain is set. "
                                            "Add FALLBACK_BRAIN=ollama to .env, raise DAILY_CALL_BUDGET, or try tomorrow."})
            except BrainError as e:
                app.mem.log("error", f"Brain problem: {e}")
                self._json(502, {"message": f"Couldn't reach the brain: {e}"})
            return None

        # ------------------------------------------------------------- GET
        def do_GET(self) -> None:
            url = urlparse(self.path)
            path, query = url.path, parse_qs(url.query)
            if path in ("/", "/index.html"):
                return self._file("index.html", "text/html; charset=utf-8")
            if path.startswith("/static/"):
                name = path[len("/static/"):]
                if name in STATIC:
                    return self._file(name, STATIC[name])
                return self._json(404, {"message": "Not found."})
            if not path.startswith("/api/"):
                return self._json(404, {"message": "Not found."})
            if not self._authed():
                return self._json(401, {"message": "Please log in."})
            if path == "/api/state":
                return self._json(200, app.state())
            if path == "/api/chat":
                return self._json(200, {"messages": app.mem.chat_messages(80)})
            if path == "/api/diary":
                return self._json(200, {"entries": app.mem.diary_entries()})
            if path == "/api/memory":
                return self._json(200, {"items": app.mem.memory_items()})
            if path == "/api/notebook":
                return self._json(200, {"items": app.mem.notebook_items()})
            if path == "/api/wishes":
                return self._json(200, {"items": app.mem.wishes_items()})
            if path == "/api/library":
                return self._json(200, {"items": app.mem.library_items()})
            if path == "/api/about":
                return self._json(200, app.about())
            if path == "/api/activity":
                try:
                    since = int(query.get("since", ["0"])[0])
                except ValueError:
                    since = 0
                events = app.mem.activity(since, 200)
                return self._json(200, {"events": events, "last_id": events[-1]["id"] if events else since})
            return self._json(404, {"message": "Not found."})

        # ------------------------------------------------------------ POST
        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if path == "/api/upload" and not self._authed():  # don't accept a big body from a stranger
                return self._json(401, {"message": "Please log in."})
            try:
                data = self._read_json(MAX_UPLOAD_BODY if path == "/api/upload" else MAX_BODY)
            except (ValueError, json.JSONDecodeError) as e:
                return self._json(400, {"message": str(e) or "Bad request."})

            if path == "/api/login":
                result = app.check_password(str(data.get("password", "")))
                if result is None:
                    return self._json(429, {"message": "Too many wrong passwords. Wait a few minutes."})
                if not result:
                    return self._json(401, {"message": "That password isn't right."})
                token = secrets.token_urlsafe(32)
                app.sessions.add(token)
                cookie = f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000"
                return self._json(200, {"ok": True}, {"Set-Cookie": cookie})

            if not self._authed():
                return self._json(401, {"message": "Please log in."})

            if path == "/api/logout":
                app.sessions.discard(self._token())
                return self._json(200, {"ok": True}, {"Set-Cookie": f"{COOKIE}=; Max-Age=0; Path=/"})

            if path == "/api/chat":
                text = str(data.get("text", "")).strip()
                if not text:
                    return self._json(400, {"message": "Write something first."})
                if len(text) > MAX_CHAT_CHARS:
                    return self._json(400, {"message": f"That's too long (limit {MAX_CHAT_CHARS} characters)."})

                def go():
                    reply, notes = app.agent.chat(text)
                    app.last_chat = time.time()
                    return reply, notes
                out = self._brain_call(go)
                if out is not None:
                    return self._json(200, {"reply": out[0], "notes": out[1]})
                return None

            if path == "/api/upload":
                name = str(data.get("name", "")).strip()[:120]
                note = str(data.get("text", "")).strip()[:MAX_CHAT_CHARS] or (
                    "Here's a picture for you." if is_picture(name) else "Here is a document for you to read.")
                try:
                    raw = base64.b64decode(str(data.get("data", "")), validate=True)
                    if is_picture(name):
                        mime, text, pictures = check_picture(name, raw), marker(1), None
                    else:
                        mime = ""
                        text, pictures = extract_document(name, raw)
                except (ValueError, binascii.Error) as e:
                    msg = str(e) if isinstance(e, ValueError) and not isinstance(e, binascii.Error) else "That file didn't upload properly. Try again."
                    return self._json(400, {"message": msg})

                def go_upload():
                    if mime:
                        entry = app.mem.save_document(name, text, [{"kind": "picture", "mime": mime, "data": raw}],
                                                      kind="picture")
                        sent = {"title": entry["title"], "id": entry["id"],
                                "picture": {"mime": mime, "data": base64.b64encode(raw).decode()}}
                    else:
                        entry = app.mem.save_document(name, text, pictures.pictures, pictures.skipped)
                        shown = app.mem.read_document(entry["id"], 1)[1]  # pictures she hasn't seen yet are explained
                        sent = {"title": entry["title"], "text": shown, "id": entry["id"], "part": 1,
                                "parts": entry["parts"], "make_notes": not entry["pictures_todo"]}
                    parts = entry["parts"]
                    try:
                        reply, notes = app.agent.chat(note, sent)
                    except Exception:
                        app.mem.discard_document(entry["id"])  # she never saw it, so don't keep it
                        raise
                    extra = "".join(f", {entry[k]} {label}" for k, label in (
                        ("pictures", "pictures"), ("scanned_pages", "scanned pages"),
                        ("pictures_skipped", "pictures it can't open")) if entry[k])
                    size = f"about {entry['words']:,} words{extra}" if entry["words"] else extra[2:]
                    if mime:
                        app.mem.log("document", f"Was sent a picture: {entry['title']}")
                    else:
                        app.mem.log("document", f"Was given a document: {entry['title']} ({size})")
                    app.last_chat = time.time()
                    return reply, notes, {**entry, "shortened": parts > 1, "parts": parts}
                out = self._brain_call(go_upload)
                if out is not None:
                    return self._json(200, {"reply": out[0], "notes": out[1], "document": out[2]})
                return None

            if path == "/api/think":
                out = self._brain_call(lambda: app.agent.think())
                if out is not None:
                    return self._json(200, {"text": out[0], "notes": out[1]})
                return None

            if path == "/api/sleep":
                out = self._brain_call(lambda: app.agent.sleep())
                if out is not None:
                    return self._json(200, {"message": out})
                return None

            if path == "/api/wishes":
                wid, status = str(data.get("id", "")), data.get("status")
                if status not in ("yes", "not_now", "open"):
                    return self._json(400, {"message": "Status must be yes, not_now or open."})
                wish = next((w for w in app.mem.wishes_items() if w["id"] == wid), None)
                if not wish:
                    return self._json(404, {"message": "That wish isn't in the list any more."})
                with app.lock:
                    if status != wish["status"]:
                        app.mem.set_wish_status(wid, status)
                        if status == "yes":
                            app.mem.remember(
                                f"My human said yes to my wish: {wish['text']}. I only have that ability once they tell me it is ready.")
                        elif status == "not_now":
                            app.mem.remember(f"My human said not now to my wish: {wish['text']}")
                        app.mem.log("wish_" + status, wish["text"])
                return self._json(200, {"items": app.mem.wishes_items()})

            return self._json(404, {"message": "Not found."})

    return Handler
