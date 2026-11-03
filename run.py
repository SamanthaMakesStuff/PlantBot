#!/usr/bin/env python3
"""Start your plant:   python run.py      (add --mock to try it with no API key)"""
from __future__ import annotations

import shutil
import sys
from datetime import datetime

from plantagent.agent import (BirthError, PlantAgent, find_variety, load_plants,
                              shopping_hint)
from plantagent.brains import BrainError, BudgetExceeded, build_router
from plantagent.config import Config
from plantagent.memory import Memory

HELP = """Commands:
  /think      let your plant have a quiet moment (writes in its diary)
  /sleep      tidy its memory
  /status     name, mood, and today's usage
  /memory     what it remembers
  /diary      its diary
  /wishes     things it has asked you for
  /buy        which real plant to buy
  /reset      start over (your old plant is saved in a backup folder)
  /help       this list
  /quit       say goodbye (everything is saved)"""


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    mock = "--mock" in sys.argv
    cfg = Config()
    if mock:
        cfg.data_dir = cfg.data_dir.parent / "data_mock"
    mem = Memory(cfg.data_dir)
    try:
        router = build_router(cfg, mock=mock)
    except BrainError as e:
        print(f"\nCan't start: {e}\nOpen the .env file and check your key (see README step 3).")
        return 1
    agent = PlantAgent(router, mem, cfg.max_reply_tokens, cfg.wikipedia, cfg.wikipedia_daily_limit)

    if not mem.has_persona():
        print("\nA new plant is about to wake up and choose what it is...\n")
        try:
            persona = agent.birth()
        except BirthError:
            print("It couldn't decide. Here are the options:")
            plants = load_plants()
            print(", ".join(p["variety"] for p in plants))
            choice = input("Type one for it: ").strip()
            if not find_variety(choice, plants):
                print("That wasn't on the list. Run again to retry.")
                return 1
            persona = agent.birth(forced_variety=find_variety(choice, plants)["variety"])
        except (BrainError, BudgetExceeded) as e:
            print(f"The brain couldn't be reached: {e}")
            return 1
        print(f"It chose to be a {persona['variety']} and named itself {persona['name']}.")
        print(f"Why: {persona['why_this_plant']}\n")
        print(shopping_hint(find_variety(persona["variety"], load_plants())))
        print(f"\n{persona['name']}: {persona['first_words']}\n")
    else:
        print(f"\n{agent.persona['name']} the {agent.persona['variety']} is awake. Type /help for commands.\n")

    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            return 0
        if not text:
            continue
        if text.startswith("/"):
            cmd = text.split()[0].lower()
            if cmd in ("/quit", "/exit"):
                print(f"{agent.persona['name']}: Goodbye for now.")
                return 0
            elif cmd == "/help":
                print(HELP)
            elif cmd == "/status":
                print(agent.status())
            elif cmd == "/memory":
                print(mem.read_memory() or "(nothing yet)")
            elif cmd == "/diary":
                print(mem.read_diary_tail(4000) or "(empty)")
            elif cmd == "/wishes":
                print(mem.read_wishes() or "(no wishes yet)")
            elif cmd == "/buy":
                print(shopping_hint(find_variety(agent.persona["variety"], load_plants())))
            elif cmd == "/think":
                try:
                    out, notes = agent.think()
                    print(f"{agent.persona['name']}: {out}" if out else "(it thought quietly)")
                    for n in notes:
                        print(f"  [{n}]")
                except (BrainError, BudgetExceeded) as e:
                    print(f"(couldn't think: {e})")
            elif cmd == "/sleep":
                try:
                    print(agent.sleep())
                except (BrainError, BudgetExceeded) as e:
                    print(f"(couldn't sleep: {e})")
            elif cmd == "/reset":
                if input("Really start over? Type YES: ").strip() == "YES":
                    backup = cfg.data_dir.parent / f"{cfg.data_dir.name}_backup_{datetime.now():%Y%m%d_%H%M%S}"
                    shutil.move(str(cfg.data_dir), str(backup))
                    print(f"Old plant saved in {backup}. Run the program again to meet a new one.")
                    return 0
            else:
                print("Unknown command. Type /help.")
            continue
        try:
            reply, notes = agent.chat(text)
        except BudgetExceeded:
            print(f"{agent.persona['name']}: *resting* (today's budget is used up; try again tomorrow, or raise DAILY_CALL_BUDGET in .env)")
            continue
        except BrainError as e:
            print(f"(couldn't reach the brain: {e})")
            continue
        print(f"{agent.persona['name']}: {reply}")
        for n in notes:
            print(f"  [{n}]")


if __name__ == "__main__":
    sys.exit(main())
