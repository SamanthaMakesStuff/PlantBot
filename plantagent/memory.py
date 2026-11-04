"""Everything the plant remembers lives in plain text files you can open."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
from datetime import datetime, timedelta
from pathlib import Path

from .pictures import MARKER, marker, unmark


def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")



_SEARCH_SKIP = {"the", "and", "for", "with", "that", "this", "what", "how", "about", "from", "does", "are",
                "you", "your", "can", "should", "when", "where", "which", "who", "why", "into", "its", "it's"}


def find_passages(text: str, query: str, max_chars: int = 30_000, where: bool = True):
    """The passages of a text that are most about a topic, in their original order.
    Returns (passages, how many passages matched)."""
    words = [w for w in re.findall(r"[a-z0-9']+", query.lower()) if len(w) > 2 and w not in _SEARCH_SKIP][:8]
    if not words:
        return "", 0
    stems = [re.compile(r"\b" + re.escape(w[:-1] if len(w) > 4 and w.endswith("s") else w), re.I) for w in words]
    scored = []
    for start, chunk in _chunks(text):
        hits = [len(p.findall(chunk)) for p in stems]
        found = sum(1 for h in hits if h)
        if found:
            scored.append((found * 100 + sum(hits), start, chunk))
    chosen, size = [], 0
    for _, start, chunk in sorted(scored, key=lambda c: -c[0]):
        if size + len(chunk) > max_chars:
            break
        chosen.append((start, chunk))
        size += len(chunk)
    label = (lambda start: f"[about {100 * start // max(1, len(text))}% of the way through]\n") if where else (lambda start: "")
    return "\n\n...\n\n".join(label(start) + chunk for start, chunk in sorted(chosen)), len(scored)


def _chunks(text: str, size: int = 1500):
    """Splits text into passages of about `size` characters at paragraph or line breaks. Yields (start, passage)."""
    start, buf, pos = 0, "", 0
    for piece in re.split(r"(\n\s*\n|\n)", text):
        if len(buf) + len(piece) > size and buf.strip():
            yield start, buf.strip()
            start, buf = pos, ""
        buf += piece
        pos += len(piece)
        while len(buf) > size * 2:  # a huge paragraph with no breaks
            yield start, buf[:size].strip()
            buf, start = buf[size:], start + size
    if buf.strip():
        yield start, buf.strip()

class Memory:
    def __init__(self, data_dir: Path) -> None:
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.persona_path = self.dir / "persona.json"
        self.baseline_path = self.dir / "persona_baseline.json"
        self.memory_path = self.dir / "memory.md"
        self.archive_path = self.dir / "memory_archive.md"
        self.notebook_path = self.dir / "notebook.md"  # things she has learnt; kept for good, never tidied away
        self.wiki_reads_path = self.dir / "wikipedia_read.json"  # which Wikipedia articles she has read, and when
        self.diary_path = self.dir / "diary.md"
        self.wishes_path = self.dir / "wishes.md"
        self.chat_path = self.dir / "chat_log.jsonl"
        self.activity_path = self.dir / "activity.jsonl"
        self.wish_status_path = self.dir / "wishes_status.json"
        self.library_dir = self.dir / "library"
        self._log_lock = threading.Lock()
        self._activity_count = 0
        if self.activity_path.exists():
            with self.activity_path.open(encoding="utf-8") as f:
                self._activity_count = sum(1 for _ in f)

    # persona
    def has_persona(self) -> bool:
        return self.persona_path.exists()

    def load_persona(self) -> dict:
        return json.loads(self.persona_path.read_text(encoding="utf-8"))

    def save_persona(self, persona: dict) -> None:
        self.persona_path.write_text(json.dumps(persona, indent=2, ensure_ascii=False), encoding="utf-8")

    def load_baseline(self) -> dict:
        try:
            return json.loads(self.baseline_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save_baseline(self, persona: dict) -> None:
        self.baseline_path.write_text(json.dumps(persona, indent=2, ensure_ascii=False), encoding="utf-8")

    # simple append-only text files
    def _append(self, path: Path, line: str) -> None:
        with path.open("a", encoding="utf-8") as f:
            f.write(line.rstrip() + "\n")

    def remember(self, text: str) -> None:
        self._append(self.memory_path, f"- [{now_stamp()}] {text.strip()}")

    def add_learning(self, text: str) -> None:
        self._append(self.notebook_path, f"- [{now_stamp()}] {' '.join(text.split())[:600]}")

    def read_notebook_tail(self, max_chars: int = 2000) -> str:
        text = self._read(self.notebook_path)
        return text[-max_chars:].split("\n", 1)[-1] if len(text) > max_chars else text

    def notebook_items(self) -> list:
        return self._items(self.notebook_path)

    def notebook_about(self, subject: str, max_chars: int = 1500) -> str:
        """Her notebook entries that mention a subject, newest first."""
        subject = re.sub(r"\s*\(.*?\)\s*", " ", subject).strip().lower()
        if len(subject) < 3:
            return ""
        found, size = [], 0
        for item in reversed(self.notebook_items()):
            if subject in item["text"].lower():
                if size + len(item["text"]) > max_chars:
                    break
                found.append(f"- {item['text']}")
                size += len(item["text"])
        return "\n".join(found)

    # Wikipedia reading: kept so she knows when she's been here before, and can see her own patterns
    def _wiki_reads(self) -> dict:
        try:
            return json.loads(self.wiki_reads_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def note_wiki_read(self, title: str) -> list:
        """Records that she read an article now. Returns when she had read it before (oldest first)."""
        with self._log_lock:
            reads = self._wiki_reads()
            before = list(reads.get(title, []))
            reads[title] = (before + [now_stamp()])[-50:]
            self.wiki_reads_path.write_text(json.dumps(reads, ensure_ascii=False), encoding="utf-8")
            return before

    def wiki_lately(self, days: int = 3) -> list:
        """[(title, times read)] for the last few days, most-read first."""
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
        counts = [(t, sum(1 for s in stamps if s >= since)) for t, stamps in self._wiki_reads().items()]
        return sorted([c for c in counts if c[1]], key=lambda c: (-c[1], c[0]))

    def add_diary(self, text: str) -> None:
        self._append(self.diary_path, f"## {now_stamp()}\n{text.strip()}\n")

    def add_wish(self, text: str) -> None:
        self._append(self.wishes_path, f"- [{now_stamp()}] {text.strip()}")

    def _read(self, path: Path) -> str:
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def read_memory(self, max_chars: int = 10_000) -> str:
        text = self._read(self.memory_path)
        return text[-max_chars:] if len(text) > max_chars else text

    def memory_lines(self) -> list:
        return [l for l in self._read(self.memory_path).splitlines() if l.strip()]

    def read_diary_tail(self, max_chars: int = 1200) -> str:
        text = self._read(self.diary_path)
        return text[-max_chars:] if len(text) > max_chars else text

    def read_wishes(self) -> str:
        return self._read(self.wishes_path)

    def replace_memory(self, new_text: str) -> None:
        backups = self.dir / "backups"
        backups.mkdir(exist_ok=True)
        if self.memory_path.exists():
            old = self._read(self.memory_path).strip()
            shutil.copy(self.memory_path, backups / f"memory-{datetime.now():%Y%m%d-%H%M%S}.md")
            if old:  # nothing is ever lost: the night's memories go into the archive she can look back through
                self._append(self.archive_path, f"## Memories as they were on {now_stamp()}\n{old}\n")
        self.memory_path.write_text(new_text.rstrip() + "\n", encoding="utf-8")

    def recall(self, query: str, max_chars: int = 6000):
        """Looks back through her memory archive and her whole diary for a topic.
        Returns (lines found, how many matched), newest first, each line once."""
        words = [w for w in re.findall(r"[a-z0-9']+", query.lower()) if len(w) > 2 and w not in _SEARCH_SKIP][:8]
        if not words:
            return "", 0
        stems = [re.compile(r"\b" + re.escape(w[:-1] if len(w) > 4 and w.endswith("s") else w), re.I) for w in words]
        found, seen = [], set()
        for source, path in (("memory", self.archive_path), ("notebook", self.notebook_path), ("diary", self.diary_path)):
            heading = ""
            for line in self._read(path).splitlines():
                if line.startswith("## "):
                    heading = line[3:].replace("Memories as they were on ", "")
                    continue
                text = re.sub(r"^- (\[[^\]]*\] )?", "", line.strip())
                key = text.lower()
                if not text or key in seen:
                    continue
                hits = sum(1 for p in stems if p.search(text))
                if hits:
                    seen.add(key)
                    found.append((hits, heading, f"[{source}, {heading[:16] or 'undated'}] {text}"))
        best, size = [], 0
        for _, heading, line in sorted(found, key=lambda f: (f[0], f[1]), reverse=True):  # best matches, newest first
            if size + len(line) > max_chars:
                break
            best.append((heading, line))
            size += len(line) + 1
        best.sort(key=lambda b: b[0], reverse=True)
        return "\n".join(line for _, line in best), len(found)

    # activity feed: a visible record of everything the plant gets up to
    def log(self, kind: str, text: str) -> int:
        with self._log_lock:
            self._activity_count += 1
            row = {"id": self._activity_count, "t": now_stamp(), "kind": kind, "text": str(text).strip()[:600]}
            with self.activity_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            return row["id"]

    def activity(self, since: int = 0, limit: int = 200) -> list:
        if not self.activity_path.exists():
            return []
        rows = []
        for line in self.activity_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("id", 0) > since:
                rows.append(row)
        return rows[-limit:]

    # structured views of the text files (for the web page)
    _ITEM_RE = re.compile(r"^- \[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\]\s*(.*)$")

    def _items(self, path: Path) -> list:
        items = []
        for line in self._read(path).splitlines():
            if not line.strip():
                continue
            m = self._ITEM_RE.match(line)
            t, text = (m.group(1), m.group(2)) if m else ("", line.lstrip("- ").strip())
            items.append({"id": hashlib.sha1(line.encode("utf-8")).hexdigest()[:10], "t": t, "text": text})
        return items

    def memory_items(self) -> list:
        return self._items(self.memory_path)

    def diary_entries(self) -> list:
        entries, current = [], None
        for line in self._read(self.diary_path).splitlines():
            if line.startswith("## "):
                current = {"t": line[3:].strip(), "text": ""}
                entries.append(current)
            elif current is not None:
                current["text"] = (current["text"] + "\n" + line).strip()
        return entries

    def wish_statuses(self) -> dict:
        try:
            return json.loads(self.wish_status_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def set_wish_status(self, wish_id: str, status: str) -> None:
        statuses = self.wish_statuses()
        if status == "open":
            statuses.pop(wish_id, None)
        else:
            statuses[wish_id] = status
        self.wish_status_path.write_text(json.dumps(statuses), encoding="utf-8")

    def wishes_items(self) -> list:
        statuses = self.wish_statuses()
        items = self._items(self.wishes_path)
        for w in items:
            w["status"] = statuses.get(w["id"], "open")
        return items

    def chat_messages(self, n: int = 60) -> list:
        if not self.chat_path.exists():
            return []
        rows = []
        for line in self.chat_path.read_text(encoding="utf-8").splitlines()[-n:]:
            try:
                row = json.loads(line)
                rows.append({"t": row.get("t", ""), "role": row["role"], "content": row["content"]})
            except Exception:
                continue
        return rows

    # chat log
    def append_chat(self, role: str, content: str, t: str = "") -> None:
        with self.chat_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"t": t or now_stamp(), "role": role, "content": content}, ensure_ascii=False) + "\n")

    def recent_chat(self, n: int = 12) -> list:
        if not self.chat_path.exists():
            return []
        rows = []
        for line in self.chat_path.read_text(encoding="utf-8").splitlines()[-n:]:
            try:
                row = json.loads(line)
                rows.append({"role": row["role"], "content": row["content"], "t": row.get("t", "")})
            except Exception:
                continue
        while rows and rows[0]["role"] != "user":
            rows.pop(0)
        return rows

    # library: documents your human has shared (plain .md files, plus a small index and her notes)
    LIBRARY_MAX_CHARS = 2_000_000  # most whole books
    DOC_PART_CHARS = 360_000  # about a novel, and under 100,000 tokens, so most books are read in one go

    def _library_index(self) -> dict:
        try:
            return json.loads((self.library_dir / "index.json").read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_index(self, index: dict) -> None:
        (self.library_dir / "index.json").write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")

    def parts_of(self, chars: int) -> int:
        return max(1, -(-chars // self.DOC_PART_CHARS))

    def save_document(self, name: str, text: str, pictures: list | None = None, skipped: dict | None = None,
                      kind: str = "document") -> dict:
        """Stores a shared document and returns its entry: id, title, chars, words, parts, added, and
        picture counts. pictures are {"kind", "mime", "data"} for markers ⟦1⟧, ⟦2⟧... in the text."""
        with self._log_lock:
            self.library_dir.mkdir(exist_ok=True)
            title = re.sub(r"[_\-]+", " ", Path(name).stem).strip() or "document"
            base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "document"
            slug, n = base, 2
            index = self._library_index()
            while slug in index or (self.library_dir / f"{slug}.md").exists():
                slug, n = f"{base}-{n}", n + 1
            text = text[: self.LIBRARY_MAX_CHARS]
            (self.library_dir / f"{slug}.md").write_text(text, encoding="utf-8")
            parts = self.parts_of(len(text))
            kept = set(int(n) for n in MARKER.findall(text))
            pictures = [(n, p) for n, p in enumerate(pictures or [], 1) if n in kept]
            entry = {"id": slug, "title": title[:80], "chars": len(text), "words": len(MARKER.sub(" ", text).split()),
                     "parts": parts, "added": now_stamp(), "summary": "", "notes_todo": list(range(1, parts + 1)),
                     "pictures": sum(1 for _, p in pictures if p["kind"] == "picture"),
                     "scanned_pages": sum(1 for _, p in pictures if p["kind"] == "page"),
                     "pictures_todo": len(pictures), "pictures_skipped": sum((skipped or {}).values()), "kind": kind}
            if pictures:
                folder = self._pictures_dir(slug)
                folder.mkdir(exist_ok=True)
                items = {}
                for n, p in pictures:
                    name = f"{n}.{p['mime'].split('/')[1]}"
                    (folder / name).write_bytes(p["data"])
                    items[str(n)] = {"kind": p["kind"], "mime": p["mime"], "file": name}
                (folder / "index.json").write_text(json.dumps({"items": items, "todo": [n for n, _ in pictures]}),
                                                   encoding="utf-8")
            index[slug] = entry
            self._write_index(index)
            return entry

    def library_items(self) -> list:
        items = []
        for e in self._library_index().values():
            if (self.library_dir / f"{e['id']}.md").exists():
                e.setdefault("words", e.get("chars", 0) // 6)  # documents from before word counts were kept
                e["parts"] = self.parts_of(e.get("chars", 0))
                e.setdefault("summary", "")
                e.setdefault("notes_todo", [])
                for key in ("pictures", "scanned_pages", "pictures_todo", "pictures_skipped"):
                    e.setdefault(key, 0)
                e.setdefault("kind", "document")
                items.append(e)
        return sorted(items, key=lambda e: e["added"], reverse=True)

    def read_document(self, doc_id: str, part: int = 1, max_chars: int | None = None):
        """Returns (title, text, part, parts) or None. doc_id must be an id from the index.

        Long documents are split into parts; part is clamped to the ones that exist.
        """
        entry = self._library_index().get(str(doc_id).strip().lower())
        if not entry:
            return None
        path = self.library_dir / f"{entry['id']}.md"
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8")
        size = max_chars or self.DOC_PART_CHARS
        parts = max(1, -(-len(text) // size))
        part = min(max(1, part), parts)
        return entry["title"], self._show_pending(entry["id"], text[(part - 1) * size:part * size]), part, parts

    # pictures inside documents: kept until she has looked at them, then her descriptions replace the markers
    def _pictures_dir(self, doc_id: str) -> Path:
        return self.library_dir / f"{doc_id}.pictures"

    def _pictures_index(self, doc_id: str) -> dict:
        try:
            return json.loads((self._pictures_dir(doc_id) / "index.json").read_text(encoding="utf-8"))
        except Exception:
            return {"items": {}, "todo": []}

    def _show_pending(self, doc_id: str, text: str) -> str:
        if "\u27e6" not in text:
            return text
        items = self._pictures_index(doc_id)["items"]

        def show(m):
            kind = items.get(m.group(1), {}).get("kind")
            if kind == "page":
                return "[a scanned page you haven't read yet; you'll read it on your own in the background]"
            return "[a picture you haven't looked at yet]" if kind else ""
        return MARKER.sub(show, text)

    def pictures_todo(self, max_pictures: int = 20, max_pages: int = 5, max_bytes: int = 20_000_000):
        """The next batch to look at: (document id, "picture" or "page", [numbers]), oldest document first."""
        for e in sorted(self.library_items(), key=lambda e: e["added"]):
            if not e["pictures_todo"]:
                continue
            found = self._pictures_index(e["id"])
            todo = [n for n in found["todo"] if str(n) in found["items"]]
            if not todo:
                continue
            kind = found["items"][str(todo[0])]["kind"]
            batch, size = [], 0
            for n in todo:
                item = found["items"][str(n)]
                path = self._pictures_dir(e["id"]) / item["file"]
                bytes_ = path.stat().st_size if path.exists() else 0
                if item["kind"] != kind or len(batch) >= (max_pages if kind == "page" else max_pictures) or (
                        batch and size + bytes_ > max_bytes):
                    break
                batch.append(n)
                size += bytes_
            return e["id"], kind, batch
        return None

    def picture(self, doc_id: str, n: int):
        """(media type, bytes) of one picture still waiting to be looked at, or None."""
        item = self._pictures_index(doc_id)["items"].get(str(n))
        path = self._pictures_dir(doc_id) / item["file"] if item else None
        return (item["mime"], path.read_bytes()) if path and path.exists() else None

    def save_pictures(self, doc_id: str, numbers: list, seen: dict) -> int:
        """Puts what she saw (number -> description, or a page's words) into the document where each picture was.
        Anything she didn't manage is marked as unreadable, so it isn't tried forever. Returns how many are left."""
        with self._log_lock:
            index = self._library_index()
            entry = index.get(doc_id)
            path = self.library_dir / f"{doc_id}.md"
            if not entry or not path.exists():
                return 0
            found = self._pictures_index(doc_id)
            text = path.read_text(encoding="utf-8")
            for n in numbers:
                item = found["items"].pop(str(n), None)
                if not item:
                    continue
                said = re.sub(r"\[\[|\]\]", "", unmark(seen.get(n, ""))).strip()
                if item["kind"] == "page":
                    words = said[:20_000] or "[A scanned page that couldn't be read]"
                    text = text.replace(marker(n), f"\n{words}\n")
                else:
                    words = " ".join(said.split())[:600]
                    text = text.replace(marker(n), f"[Picture: {words}]" if words else "[A picture that couldn't be made out]")
                (self._pictures_dir(doc_id) / item["file"]).unlink(missing_ok=True)
            found["todo"] = [n for n in found["todo"] if str(n) in found["items"]]
            text = re.sub(r"\n{3,}", "\n\n", text).strip()[: self.LIBRARY_MAX_CHARS]
            path.write_text(text, encoding="utf-8")
            entry.update(chars=len(text), words=len(MARKER.sub(" ", text).split()), parts=self.parts_of(len(text)),
                         pictures_todo=len(found["todo"]))
            if found["todo"]:
                (self._pictures_dir(doc_id) / "index.json").write_text(json.dumps(found), encoding="utf-8")
            else:
                shutil.rmtree(self._pictures_dir(doc_id), ignore_errors=True)
                if entry.get("kind") == "picture":  # a picture she was sent: her description is all there is to note
                    said = " ".join(re.sub(r"\[\[|\]\]", "", unmark(seen.get(1, ""))).split())[:240]
                    entry["notes_todo"], entry["summary"] = [], entry.get("summary") or said
                else:
                    entry["notes_todo"] = list(range(1, entry["parts"] + 1))  # now she can read it all and make notes
            self._write_index(index)
            return len(found["todo"])

    # reading notes: she writes these herself, in the background, after a document is shared
    def _notes_path(self, doc_id: str) -> Path:
        return self.library_dir / f"{doc_id}.notes.json"

    def notes_todo(self):
        """The next (id, part) that still needs reading notes, or None. Every document gets its first
        notes (and so its summary) before any long book moves on to its later parts."""
        todo = [(e["notes_todo"][0], e["added"], e["id"]) for e in self.library_items()
                if e["notes_todo"] and not e["pictures_todo"]]  # pictures first, so her notes include them
        if not todo:
            return None
        part, _, doc_id = min(todo)
        return doc_id, part

    def queue_reading(self, doc_id: str, parts) -> None:
        """Adds parts of a long book she hasn't read yet to her background reading, so she carries on reading it on
        her own. Parts she already has notes on aren't read again (she can still open them herself any time)."""
        with self._log_lock:
            index = self._library_index()
            entry = index.get(str(doc_id).strip().lower())
            if not entry:
                return
            last = self.parts_of(entry.get("chars", 0))
            try:
                read = set(json.loads(self._notes_path(entry["id"]).read_text(encoding="utf-8")))
            except Exception:
                read = set()
            todo = set(entry.get("notes_todo", [])) | {p for p in parts if 1 <= p <= last and str(p) not in read}
            entry["notes_todo"] = sorted(todo)
            self._write_index(index)

    def save_notes(self, doc_id: str, part: int, notes: str, summary: str = "") -> None:
        with self._log_lock:
            index = self._library_index()
            entry = index.get(doc_id)
            if not entry:
                return
            path = self._notes_path(doc_id)
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                saved = {}
            # notes come from reading material, so they must never carry an action tag back into her prompt
            notes, summary = (re.sub(r"\[\[|\]\]", "", t) for t in (notes, summary))
            if notes:
                saved[str(part)] = notes[:6000]
                path.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
            entry["notes_todo"] = [p for p in entry.get("notes_todo", []) if p != part]
            if summary and not entry.get("summary"):
                entry["summary"] = summary[:240]
            self._write_index(index)

    def read_notes(self, doc_id: str) -> str:
        """All her notes on a document, part by part, or "" if she hasn't made any."""
        try:
            saved = json.loads(self._notes_path(str(doc_id).strip().lower()).read_text(encoding="utf-8"))
        except Exception:
            return ""
        later = saved.pop("later", "")
        parts = sorted(saved.items(), key=lambda kv: int(kv[0]))
        text = parts[0][1] if len(parts) == 1 else "\n\n".join(f"Part {k}:\n{v}" for k, v in parts)
        if later:
            text = (text + "\n\n" if text else "") + "Added later:\n" + later
        return text

    def add_note(self, doc_id: str, note: str):
        """Adds one of her own notes to a document, e.g. after re-reading it. Returns the title, or None."""
        doc_id = str(doc_id).strip().lower()
        note = re.sub(r"\[\[|\]\]", "", " ".join(note.split()))[:600]
        with self._log_lock:
            entry = self._library_index().get(doc_id)
            if not entry or not note:
                return None
            path = self._notes_path(doc_id)
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                saved = {}
            lines = [l for l in saved.get("later", "").splitlines() if l][-59:]  # keep the newest 60
            saved["later"] = "\n".join(lines + [f"- {note}"])
            path.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
            return entry["title"]

    def search_document(self, doc_id: str, query: str, max_chars: int = 30_000):
        """Finds the passages about a topic. Returns (title, passages, how many passages matched) or None."""
        entry = self._library_index().get(str(doc_id).strip().lower())
        path = self.library_dir / f"{entry['id']}.md" if entry else None
        if not entry or not path.exists():
            return None
        passages, found = find_passages(path.read_text(encoding="utf-8"), query, max_chars)
        return entry["title"], passages, found

    def discard_document(self, doc_id: str) -> None:
        with self._log_lock:
            index = self._library_index()
            if index.pop(doc_id, None) is not None:
                (self.library_dir / f"{doc_id}.md").unlink(missing_ok=True)
                self._notes_path(doc_id).unlink(missing_ok=True)
                shutil.rmtree(self._pictures_dir(doc_id), ignore_errors=True)
                self._write_index(index)
