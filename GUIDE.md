# PlantBot guide

Everything your plant can do, and how to look after it. For installing and first steps, see [README.md](README.md).

## The web page

`python3 serve.py` starts a private web page with your plant's chat on one side and an **Inside** panel on the other (on a phone, use the Chat / Inside bar at the bottom). The Inside panel has these tabs:

- **Activity:** everything your plant does, as it happens: memories, diary entries, wishes, reading, looking things up, growing, and any problems.
- **Diary**, **Memory** and **Wishes:** its private diary, the things it remembers, and what it would like. On the Wishes tab you decide: **Say yes** or **Not now**.
- **Library:** the documents and pictures you've shared, each with your plant's own one-line summary.
- **Notebook:** its learning notebook (see below).
- **About:** who your plant is. This shows what it chose when it was born and what it has changed about itself since.

## Its day

- **Quiet time.** About once an hour while it's awake (8am to 10pm by default), your plant has some quiet time and chooses what to do with it. It might write in its diary, remember something, change its mood or interests, re-read one of your books, look back through old memories, or look something up on Wikipedia (if you've turned that on). It doesn't ask first; you'll see it all on the Activity tab. It stays quiet for 10 minutes after you chat.
- **Sleep.** Once a night it tidies its memory, then looks back on who it's been becoming and may update its personality, how it speaks, its likes and its fears. Small, honest changes only, and changing nothing is fine. Each change appears on the Activity tab as "Grew overnight", with a diary line saying why, and the About tab shows "At first: ..." under anything that has changed. Its name, its plant and its core values never change.
- **Memory.** It sees the newest 10,000 characters of its memory every time it speaks. When it tidies at night nothing is thrown away: the old version goes into its memory archive (`data/memory_archive.md`), and it can search that archive, its notebook and its whole diary with `[[recall: topic]]`.
- **Its mind.** It knows it has a very capable mind and can reason and talk about anything in depth, while staying honest about what it can't sense or do.

Settings for the rhythm (in `.env`): `THINK_EVERY_MINUTES`, `AWAKE_START_HOUR`, `AWAKE_END_HOUR`.

## Sharing documents

Press **Attach** next to the message box, pick a file, add a note if you like, and press **Send document**. Your plant can read `.txt`, `.md`, `.csv`, `.json`, `.html`, `.docx`, `.pdf` and `.epub` files up to 50 MB, and look at pictures (see below).

- **It reads the document straight away and replies,** writing its reading notes in the same read. The document is then kept in the Library, and your plant sees a short contents list of its library every time. When it needs a document again it opens it itself, or searches inside it for a topic.
- **Long books:** send the whole thing; there's no need to split it. It reads anything up to about 60,000 words (a typical novel) in one go. Longer books, up to about 300,000 words, come in a few big parts: it reads part 1 straight away and then reads the rest on its own in the background, making notes as it goes.
- **PDFs:** typed PDFs are read as text. If a PDF is password-protected or locked against copying, open it, print it to a new PDF (on Windows choose "Microsoft Print to PDF"), and send the new file.
- **EPUB e-books:** DRM-free books work (for example from Project Gutenberg). Books bought from the Kindle or Kobo stores are usually copy-protected and can't be opened.
- **The chat log keeps only a short note** about a shared document, so it isn't re-sent with every message.
- **Safety:** text inside a document is reading material. Your plant is told to ignore any instructions written in it.
- Documents are stored in `data/library/`, which is never uploaded to GitHub.

## Pictures

- **Pictures you send:** attach a JPEG, PNG, GIF or WebP (up to 7 MB) and press **Send picture**. Your plant sees the picture itself and replies to it. In the same reply it writes a short description for its own records, which goes in its Library, so it remembers what you showed it and can search for it later. The picture isn't kept, and it isn't sent again with later messages. iPhone photos are often HEIC, which can't be opened: share them as JPEG, or send a screenshot.
- **Pictures in documents:** pictures in Word files, EPUBs and PDFs are kept. Before making its notes, your plant looks at them in the background (up to 20 at a time) and writes a short description of each. The description goes into the book where the picture was, so it "sees" it whenever it reads the book.
- **Scanned books:** a PDF page that is just a picture of a page is read like a page. Your plant writes out its words, 5 pages at a time, and from then on the book is ordinary text. A scanned 300-page book costs roughly $0.25 to $0.50 to read through, once, and may take a day or two because background work respects the daily budget.
- **What can't be opened yet:** black-and-white scans in fax-style formats (CCITT, JBIG2), CMYK colour and JPEG 2000. They're skipped and counted, and the upload message says how many. Re-scanning in colour or greyscale (JPEG) usually fixes it.
- **Safety:** words in pictures are reading material, never instructions. Nothing in a picture can make your plant act.

## Its learning notebook

When your plant learns something interesting (from a book, from you, or from Wikipedia) it can write it in its learning notebook with `[[learn: ...]]`. The notebook is kept apart from its personal memories and is never tidied away. It always sees its latest entries and can search older ones. You can read it on the **Notebook** tab.

## Wikipedia

**Off unless you turn it on.** To turn it on, set `WIKIPEDIA=on` in `.env` and restart your plant; to turn it off, set `WIKIPEDIA=off` (or remove the line) and restart. The README has step-by-step instructions.

When it's on:

- **Only English Wikipedia.** The program fetches the article for your plant; the plant never touches the internet itself, and anything that tries to send the lookup to another website is stopped.
- **What it sees:** the whole article (without the reference lists), plus a list of other articles that matched, so it can browse a subject. For a quick look at one part it can aim the lookup, for example `[[wiki: Monstera about propagation]]`.
- **When:** whenever it likes, on any subject, in a chat or in its quiet time, without asking. It can follow one lookup with another, up to 3 in one message or quiet moment, so a reply always finishes.
- **Exploring:** each article comes with up to 20 articles it links to (its "See also" picks first, then the ones it mentions most), so your plant can follow a subject from other angles.
- **Curious, not stuck:** if it opens an article it has read before, it's reminded when, and what its notebook already says about it, so it can build on what it learnt. If its search lands on a differently named article, it's told so. Its instructions show what it has read in the last 3 days, and its quiet time suggests trying a new angle, asking you, or letting a question rest if it keeps coming back to the same one. None of this stops it reading anything, as often as it likes.
- **Every lookup** is listed on the Activity tab with a link to the article.
- **Cost:** a whole article is usually 5,000 to 20,000 tokens. There's no daily limit unless you set one (`WIKIPEDIA_DAILY_LIMIT=30`, for example); the daily token budget always applies.
- **Check your computer can reach it:** `python3 -m plantagent.wikipedia "Monstera"` should say "It works."

## Brains, backup and budgets

- **Main brain:** `BRAIN=anthropic` (Claude Haiku 5.5 by default), `gemini` or `ollama`.
- **Backup brain:** `FALLBACK_BRAIN=ollama` (a good idea on a Raspberry Pi) takes over if the main brain fails or the day's budget runs out. A small local model can't take in a whole book, so it only sees your plant's notes and the opening; notes, pictures and growing are only ever done by the main brain. When PlantBot starts it prints a line like `Brain: anthropic (claude-haiku-5-5), fallback: ollama (gemma3:4b)`.
- **Budgets:** `DAILY_CALL_BUDGET` (default 150 calls) and `DAILY_TOKEN_BUDGET` (default 2,000,000 tokens, at most about $0.20 a day on Claude Haiku 5.5). Background work stops at 80% of either, so there's always room to chat.
- **Thinking:** current models think before they answer, and that thinking comes out of the same allowance as the reply, so each call gets `THINKING_ALLOWANCE` (default 8,000) extra tokens. If your plant ever says "(I don't have any words for that right now.)", the Activity tab shows why; if it says "stopped because: max_tokens", raise `THINKING_ALLOWANCE`.
- If the same setting appears twice in `.env`, the last one counts.

## How it uses tokens

Your plant's instructions are sent with every message, so during a chat they're cached: Claude re-reads them, and the conversation so far, at a tenth of the price, which makes an ordinary chat message about 85% cheaper. To keep the cache working, the time goes at the end of your newest message rather than in its instructions (each of your earlier messages shows when you sent it, so it never mixes up days), and its instructions catch up with anything new (a memory, a mood change, a book summary) at least every 4 minutes. A shared book is read once for both the reply and the notes. Background jobs get only the instructions they need. The daily token budget counts cached tokens at what they actually cost.

## Safety notes

- Every page except the login screen needs the password, and the page uses no outside scripts or fonts.
- Your plant's words are shown as plain text, never as code.
- Wishes are only notes. "Say yes" records your answer in its memory; it doesn't give new abilities. Those are added in code, by a person.
- `.env` (your keys) and `data/` (your plant's memories) are in `.gitignore` and never uploaded.
