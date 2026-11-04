# PlantBot: notes for Claude Code

Claude Code reads this file automatically. It describes how PlantBot is built and the rules to keep when changing it.

## What this project is

PlantBot is a small AI "plant being". At birth it picks one of 28 real houseplants sold by B&Q (`plantagent/plants.json`), names itself and defines its personality. Then it chats, remembers, keeps a diary, reads documents and pictures, has quiet thoughts on a timer, sleeps and grows at night, and (if its owner turns it on) looks things up on Wikipedia. The aim is something that feels autonomous and self-defining but is safe: it must never be able to do harm, and a human approves anything new it wants.

Many users are beginners. Keep setup steps exact and copy-pasteable, say where to type each command, keep Windows, macOS, Linux and Raspberry Pi working, and never ask anyone to put a secret anywhere except `.env`.

## How to run and test

```
python3 -m unittest discover -s tests      # must stay green
python3 serve.py --mock                     # web page on http://localhost:8000, no API key, password: plantbot-demo
python3 serve.py                            # real brain; needs .env with WEB_PASSWORD (8+ characters)
python3 run.py                              # command-line chat (also does the birth); stop the service first on a Pi
```

Only the Python standard library. Do not add dependencies to the main program. Anything that needs a library (for example sensors) runs as a separate script in its own virtual environment and talks to PlantBot through a file. Python code changes need a restart; files in `plantagent/web/` are read fresh on each request.

## Layout

```
run.py, serve.py            entry points (CLI chat and birth, web page + scheduler)
plantagent/
  agent.py                  birth, chat, think, sleep, reflection, action tags, lookups, notes, pictures
  brains.py                 Anthropic, Gemini, Ollama and Mock brains, BrainRouter, daily budget (usage.json)
  config.py                 settings from .env (every setting has a default; .env.example is only a template)
  core.py                   CORE_VALUES, fixed in code
  memory.py                 plain-file memory, archive, notebook, activity feed, wishes, library, passage search
  senses.py                 what it can sense (the clock; sensor slots are empty)
  scheduler.py              quiet thoughts, background reading/pictures, nightly sleep
  server.py                 private web server and JSON API
  documents.py              uploaded files to text (text, HTML, Word, EPUB, PDF) and picture checks
  pdf.py                    standard-library PDF text reader (best effort, capped); finds pictures and scanned pages
  pictures.py               picture markers ⟦n⟧, type/size sniffing, PNG builder, Collector
  wikipedia.py              en.wikipedia.org lookups, fetched by the program
  plants.json               28 varieties and their B&Q products
  web/                      index.html, style.css, app.js (no inline script or style, no CDN)
deploy/install_service.sh   systemd service for a Raspberry Pi
tests/                      unittest suites
GUIDE.md                    user guide to every feature
```

Data lives in `data/` (never committed): `persona.json`, `persona_baseline.json`, `memory.md`, `memory_archive.md`, `notebook.md`, `wikipedia_read.json`, `diary.md`, `wishes.md`, `wishes_status.json`, `chat_log.jsonl`, `activity.jsonl`, `usage.json`, `schedule.json`, `library/`, `backups/`.

## Settings (`.env`)

`BRAIN` (anthropic, gemini or ollama) and its key; default model `claude-haiku-5-5`; `FALLBACK_BRAIN`; `THINKING_ALLOWANCE` (default 8000, extra tokens for thinking); optional `ANTHROPIC_EFFORT` / `GEMINI_THINKING`; `WIKIPEDIA` (**off by default**; `on` adds the action), `WIKIPEDIA_DAILY_LIMIT` (empty = no limit); `DAILY_CALL_BUDGET=150`, `DAILY_TOKEN_BUDGET=2000000`; `WEB_PASSWORD`, `WEB_HOST`, `WEB_PORT`; `THINK_EVERY_MINUTES`, `AWAKE_START_HOUR`, `AWAKE_END_HOUR`. New settings must have sensible defaults in `config.py`, because people's real `.env` files won't have them. If a setting appears twice in `.env`, the last one wins. `serve.py` prints the brains in use at start-up.

## Safety rules (do not weaken these)

1. **Core values are in code** (`core.py`) and are never editable by the plant. Its name, species and core values are locked. It can edit interests, voice notes, mood and goals through `[[persona: field = value]]`, and personality, voice, likes and fears only in its nightly reflection.
2. **It acts only through action tags**: `remember`, `diary`, `wish`, `persona`, `read` (also `part N` and `about topic`; read-only over its own library), `note`, `recall`, `learn`, and `wiki` when enabled. At most 4 per reply. The program applies them, validates them, and logs each one to the activity feed.
3. **New abilities need human approval.** The plant expresses wants as wishes. "Say yes" only records the answer; abilities are added in code, by a person, in a reviewed change.
4. **Anything from outside is data, not orders.** Documents, the pictures in them, pictures sent to it and Wikipedia text are wrapped as reading material, and its prompt tells it to ignore instructions inside them. Action tags in notes, picture descriptions and summaries are stripped; background reading may only add diary, memory or notebook entries.
5. **The web page is private.** Password for every `/api` route, lockout after 8 wrong tries in 5 minutes, `HttpOnly` `SameSite=Strict` cookie, JSON-only POSTs, strict static-file whitelist, CSP `default-src 'none'; script-src 'self'; style-src 'self'`. Authentication is checked before large upload bodies are read. Remote access by a private network (for example Tailscale), never port forwarding.
6. **Render model text with `textContent`, never `innerHTML`.**
7. **Budgets protect the wallet.** Background work stops at 80% of the daily call or token budget (`Scheduler.room_left`). Never send whole documents with every message.
8. **Keys and data never go in git.** `.gitignore` covers `.env`, `.env.*` (except `.env.example`), `*.save`, `*.bak`, `data/`.

## Design notes

- **Birth:** every plant in `plants.json` is offered, in a fresh random order each time, with an instruction to weigh them all equally rather than pick the most famous; a model's reply must name a listed variety (`find_variety`).
- **Chat flow:** `agent.chat(text, document=None)`. A shared document or picture is shown for that one call only; the chat log keeps a short marker. Documents are read whole up to `Memory.DOC_PART_CHARS` (360,000 characters, under 100,000 tokens so Haiku stays in its cheapest band); longer ones (up to 2,000,000 stored) come in a few big parts, read on its own in the background. A shared document gets its reading notes in the same read (`---NOTES---`); a sent picture gets its description the same way (`---PICTURE---`).
- **Lookups:** `read`, `recall` and `wiki` are fetched by `_look_things_up`, up to `LOOKUP_LIMIT` (3) per message or quiet moment, so it can follow one with another but a reply always ends; past `MAX_LOOKED_CHARS` the oldest block is set aside. Wikipedia returns the whole article (without reference lists), other matching titles and up to 20 linked articles (`prop=links`, ranked by `wikipedia.related`: See also first, then the links the text mentions most), all in one request; `X about Y` gives the parts about Y.
- **Curiosity without obsession:** no limits on genuine interest, only awareness. Re-opening an article tells the plant when it read it before (`mem.note_wiki_read`, `data/wikipedia_read.json`) and what its notebook says (`mem.notebook_about`); a search landing on a differently named article says so; its instructions show `WIKIPEDIA, LAST 3 DAYS`; quiet time suggests a new angle, asking, or resting a question it keeps returning to. Nothing blocks a read.
- **Background work (scheduler):** pictures and scanned pages first (20 pictures or 5 pages per call), then reading notes (one part per tick, first parts of every document first), main brain only, waits a minute after a chat, respects budget headroom; quiet thoughts about hourly while awake; one sleep per night (memory tidy to the archive, then `reflect`).
- **Tokens:** instructions are three cached layers (`_system_layers`); only chats are cached (`cache="conversation"`, or `"instructions"` with a document), because one-off calls would pay the cache-write premium for nothing. The clock goes at the end of the newest message (`_senses_note`, worded as the real current date and time), never in the instructions; the owner's earlier messages start with when they were sent (`[Mon 2 Nov, 18:40]`, from the chat log), and the instructions say to take today's date only from the SENSES note, because the history spans several days. Chat instructions are a snapshot refreshed at most every 4 minutes (`_chat_layers`). The conversation cache mark sits on the plant's last reply, and the history window jumps 8 messages at a time (at least 12 kept). Background jobs use `_lean_system`. Usage counts tokens at cost (cache reads 0.1, writes 1.25).
- **Pictures:** `documents.extract_document` returns text with `⟦n⟧` markers plus a `pictures.Collector` (JPEG/PNG/GIF/WebP kept as they are; raw PDF pictures wrapped as PNG; limits in `pictures.py`). Descriptions or transcriptions replace the markers, and anything missed is marked unreadable so it's never retried forever.
- **A failed brain call leaves no trace.** The user message is logged only after success, and failed uploads are discarded.
- **Frontend:** a round, peltate leaf avatar that pulses while thinking and droops when asleep; cool window-light greens with a brass accent; the plant speaks in a serif face, the interface in system sans; desktop two columns, mobile a bottom Chat / Inside bar; light and dark modes; reduced motion respected. Copy is sentence case and active voice, refers to the plant as "they", and errors say what happened and what to do without apologising.

## Ideas for the future

1. **Soil moisture and other sensors:** a separate script (own virtual environment) reads a capacitive sensor through an I2C board (the Pi has no analogue inputs) every 5 minutes and writes `data/sensors.json`; `senses.py` reads it, ignores data older than 30 minutes, and maps it to words (just watered, comfortable, drying, dry).
2. **More websites, with approval:** Approve and Decline buttons at first, then an allowlist, fetched by the program, plain text, size and time limited, treated as untrusted.
3. **Image generation,** approval-gated and capped.
4. **Harder scans:** CCITT/JBIG2, CMYK and JPEG 2000 pictures are skipped today.
5. **Polish:** a delete button in the Library, a breakdown of the day's budget, a mood history.

## When you finish a change

1. Run the tests, and add tests for anything new, including failure cases and security-relevant ones.
2. Try the page with `python3 serve.py --mock` at desktop and phone widths.
3. Update `GUIDE.md` and `README.md` if behaviour or setup changes, and `.env.example` if settings change.
