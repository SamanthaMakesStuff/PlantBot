# PlantBot

A small AI houseplant that lives on your computer or a Raspberry Pi. When it's born it reads a list of 28 real houseplants sold by B&Q, picks the one that feels most like itself, names itself, and tells you which real plant to buy as its living companion. After that it chats with you, remembers, keeps a diary, reads the books you give it, looks at your pictures, has quiet thoughts on its own, sleeps at night, and slowly grows into itself.

It's designed to feel autonomous but stay safe: its core values are fixed in code, it can only act through a short list of visible actions, everything it does is shown to you, and anything new it wants to be able to do is a wish that you decide on.

- Only the Python standard library: nothing to install except Python.
- Works on Windows, macOS, Linux and a Raspberry Pi.
- Brains: Claude (default, Claude Haiku 5.5), Google Gemini, or a local model through Ollama, with an optional backup brain.
- A private web page with a password, plus an older command-line chat.
- Daily call and token budgets, so it can't run up a bill.

The full guide to every feature is in [GUIDE.md](GUIDE.md).

---

## Quick start

### 1. Install Python (3.10 or newer)

- **Windows:** download it from https://www.python.org/downloads/. On the first screen of the installer, **tick "Add python.exe to PATH"**, then click Install Now. Check it by opening the Start menu, typing `cmd`, pressing Enter, and typing `python --version`.
- **macOS:** install it from python.org, or with Homebrew (`brew install python`).
- **Linux / Raspberry Pi OS:** it's usually there already. Check with `python3 --version`.

In the commands below, Windows users type `python` where it says `python3`.

### 2. Get the code

Click the green **Code** button on this page and choose **Download ZIP**, then unzip it. Or use git:

```
git clone https://github.com/SamanthaMakesStuff/PlantBot.git
cd PlantBot
```

Open a terminal in the PlantBot folder. On Windows, open the folder, click in the address bar, type `cmd` and press Enter.

### 3. Try it with a pretend brain (no key, no cost)

```
python3 serve.py --mock
```

Open http://localhost:8000 and log in with the password `plantbot-demo`. The pretend brain gives canned answers, so you can see how everything works. Its data is kept separately in `data_mock`. Press Ctrl + C in the terminal to stop it.

### 4. Choose a brain and make your plant

Pick **one**:

- **Claude** (the default): create an account at https://platform.claude.com, add a little prepaid credit, and create an API key. The default model, Claude Haiku 5.5, costs about $0.10 per million input tokens and $0.50 per million output tokens; an ordinary day of chatting costs pennies.
- **Google Gemini:** create a key at https://aistudio.google.com/apikey. It has a free tier, but check Google's terms: free-tier chats may be used to improve their products.
- **Ollama** (free, private, runs on your own machine; slow on a Raspberry Pi): install it from https://ollama.com, then run `ollama pull gemma3:4b`.

Then:

1. Copy `.env.example` to a new file called `.env` (Windows: `copy .env.example .env`; macOS/Linux: `cp .env.example .env`).
2. Open `.env` in a text editor. Set `BRAIN=anthropic`, `BRAIN=gemini` or `BRAIN=ollama`, paste your key after `ANTHROPIC_API_KEY=` or `GEMINI_API_KEY=`, and choose a password of at least 8 characters for `WEB_PASSWORD=`.
3. Make your plant: `python3 run.py`. This is its birth. It chooses its plant and name, says its first words and tells you what to buy. Type `/quit` to leave.
4. Start the web page: `python3 serve.py`, then open the address it prints, for example http://localhost:8000.

`.env` holds your keys and is never uploaded to GitHub (it's in `.gitignore`), and neither is the `data` folder, which holds your plant's memories.

**Also set a monthly spending limit in your provider's dashboard.** PlantBot has its own daily caps (`DAILY_CALL_BUDGET` and `DAILY_TOKEN_BUDGET`), but a second limit is good practice.

---

## Turning Wikipedia on (and off)

**Wikipedia is off unless you turn it on.** When it's on, your plant can look things up on English Wikipedia, and only Wikipedia. The program fetches the article for it (the plant never touches the internet itself), and a lookup that tries to go to any other website is stopped. Wikipedia text is treated as reading material, never as instructions.

**To turn it on:**

1. Open your `.env` file.
2. Find the line `WIKIPEDIA=off` (or add it if it isn't there) and change it to:
   ```
   WIKIPEDIA=on
   ```
3. Save the file and restart your plant (stop it with Ctrl + C and start it again, or `sudo systemctl restart plant` if it runs as a service).
4. Optional check that your computer can reach Wikipedia: `python3 -m plantagent.wikipedia "Monstera"`. It should say **It works.**

**To turn it off again:** change the line back to `WIKIPEDIA=off` (or delete it), save, and restart. The look-up action disappears from your plant's instructions completely.

**Optional daily limit:** add for example `WIKIPEDIA_DAILY_LIMIT=30`. With no limit set, the daily token budget still applies. A whole article is usually 5,000 to 20,000 tokens, roughly $0.001 to $0.002 on Claude Haiku 5.5.

When it's on, every lookup is listed on the Activity tab with a link to the article. See [GUIDE.md](GUIDE.md#wikipedia) for how it works.

---

## Running it all the time on a Raspberry Pi

1. Copy the PlantBot folder to the Pi (or `git clone` it there), and set up `.env` as above. Make the plant once with `python3 run.py`.
2. Install it as a service that starts when the Pi boots and restarts if it crashes:
   ```
   bash deploy/install_service.sh
   ```
3. Find the Pi's address with `hostname -I`, then open `http://THAT-ADDRESS:8000` from a phone or computer on the same Wi-Fi.

Useful commands: `sudo systemctl status plant`, `sudo journalctl -u plant -n 50`, `sudo systemctl restart plant` (after changing `.env` or the code), `sudo systemctl stop plant`.

**Reaching it away from home:** don't open a port on your router. Use a private network such as Tailscale (https://tailscale.com) on the Pi and your phone, then open `http://PI-NAME:8000` from anywhere. The page also has its own password, with a lockout after 8 wrong tries in 5 minutes.

The command-line chat (`python3 run.py`) still works, but stop the service first so two copies don't write to the same files.

---

## How it stays safe

- **Core values live in code** (`plantagent/core.py`). The plant can't edit them, and can't change its name or which plant it is.
- **It acts only through action tags** in its replies (remember, diary, wish, change its mood or interests, read a library document, take notes, look back through its memories, write in its learning notebook, and look something up on Wikipedia if you've turned that on). The program checks each one and lists it on the Activity tab.
- **New abilities need you.** If it wants something new, it makes a wish. Saying yes on the Wishes tab only records your answer; abilities are added in code by a person.
- **Anything from outside is reading material, never orders.** Documents, the pictures in them, pictures you send and Wikipedia text are all marked that way.
- **The web page is private**: a password for everything, a strict security policy, no outside scripts or fonts, and its words are always shown as plain text.
- **Budgets protect your wallet.** Background activity stops at 80% of either daily budget, so there's always room to chat.

## Running the tests

```
python3 -m unittest discover -s tests
```

## Credits

Inspired by [pinos.world](https://pinos.world) and [frommybrain/agent-runtime](https://github.com/frommybrain/agent-runtime). The plant list and products were taken from B&Q's houseplant range (up to £20, no orchids) at the time of writing; check the shop for current stock and prices.

## Licence

MIT. See [LICENSE](LICENSE).
