#!/usr/bin/env python3
"""Start the web page and timed thoughts:   python3 serve.py   (add --mock to try it with no API key)"""
from __future__ import annotations

import socket
import sys
import threading
from http.server import ThreadingHTTPServer

from plantagent.agent import PlantAgent
from plantagent.brains import BrainError, build_router
from plantagent.config import Config
from plantagent.memory import Memory
from plantagent.scheduler import Scheduler
from plantagent.server import App, make_handler


def lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet is sent
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


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
        print(f"Can't start: {e}\nCheck your key in the .env file.")
        return 1
    agent = PlantAgent(router, mem, cfg.max_reply_tokens, cfg.wikipedia, cfg.wikipedia_daily_limit)
    if not mem.has_persona():
        if mock:
            agent.birth()
        else:
            print("Your plant hasn't been born yet. Run  python3 run.py  once first, then start this.")
            return 1

    password, host = cfg.web_password, cfg.web_host
    if not password:
        if mock:
            password, host = "plantbot-demo", "127.0.0.1"
        else:
            print("Set WEB_PASSWORD in your .env file (at least 8 characters). The page is private, so it needs one.")
            return 1
    if len(password) < 8:
        print("WEB_PASSWORD must be at least 8 characters.")
        return 1

    app = App(agent, mem, router, password)
    scheduler = Scheduler(app, cfg.think_every_minutes, cfg.awake_start, cfg.awake_end)
    app.scheduler = scheduler
    stop = threading.Event()
    threading.Thread(target=scheduler.run_forever, args=(stop,), daemon=True).start()

    server = ThreadingHTTPServer((host, cfg.web_port), make_handler(app))
    server.daemon_threads = True
    name = agent.persona["name"]
    print(f"{name} is awake. Open one of these in a browser:")
    print(f"  http://localhost:{cfg.web_port}")
    if host not in ("127.0.0.1", "localhost"):
        print(f"  http://{lan_ip()}:{cfg.web_port}   (from another device on your Wi-Fi)")
    if mock:
        print("Mock mode. Password: plantbot-demo")
    print(f"Brain: {router.describe()}")
    print("Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nGoodbye.")
    finally:
        stop.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
