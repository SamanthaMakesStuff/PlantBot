"""Timed behaviour: a quiet thought now and then by day, and one sleep each night."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta

from .brains import BrainError, BudgetExceeded


class Scheduler:
    def __init__(self, app, think_every_min: int = 60, awake_start: int = 8, awake_end: int = 22,
                 quiet_after_chat_min: int = 10, headroom: float = 0.8) -> None:
        self.app = app
        self.think_every = max(5, think_every_min)
        self.awake_start, self.awake_end = awake_start, awake_end
        self.quiet_after_chat = quiet_after_chat_min
        self.headroom = headroom  # background thoughts stop at this share of the daily call budget
        self.path = app.mem.dir / "schedule.json"
        self.retry_after = 0.0
        self.state = self._load()

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            # First start: don't think the instant the program launches.
            return {"last_think": time.time(), "last_sleep_key": ""}

    def _save(self) -> None:
        self.path.write_text(json.dumps(self.state), encoding="utf-8")

    def is_night(self, now: datetime | None = None) -> bool:
        hour = (now or datetime.now()).hour
        return hour < self.awake_start or hour >= self.awake_end

    def sleep_key(self, now: datetime) -> str:
        """One key per night, so the plant sleeps once even across midnight."""
        day = now.date()
        if now.hour < self.awake_start:
            day -= timedelta(days=1)
        return day.isoformat()

    def due(self, now: datetime | None = None, ts: float | None = None):
        """Returns 'sleep', 'think' or None. Pure decision, no side effects."""
        now = now or datetime.now()
        ts = time.time() if ts is None else ts
        if ts < self.retry_after:
            return None
        if self.is_night(now):
            return "sleep" if self.state.get("last_sleep_key") != self.sleep_key(now) else None
        if ts - self.state.get("last_think", 0) < self.think_every * 60:
            return None
        if ts - self.app.last_chat < self.quiet_after_chat * 60:
            return None  # you're chatting; don't interrupt
        if not self.room_left():
            return None  # keep the rest of today's budget for talking to you
        return "think"

    def room_left(self) -> bool:
        """Background work (thoughts, reading, notes) stops at 80% of today's calls or tokens,
        so there's always room left for talking to you."""
        router = self.app.router
        if router.over_budget():
            return False
        used = router.usage.snapshot()
        if router.call_budget and used["calls"] >= self.headroom * router.call_budget:
            return False
        if router.token_budget and used["tokens"] >= self.headroom * router.token_budget:
            return False
        return True

    def pictures_due(self, ts: float | None = None):
        """The next batch of pictures or scanned pages to look at, if now is a good time. Works day and night."""
        ts = time.time() if ts is None else ts
        if ts < self.retry_after or ts - self.app.last_chat < 60 or not self.room_left():
            return None
        return self.app.mem.pictures_todo()

    def notes_due(self, ts: float | None = None):
        """The next (document id, part) to make reading notes on, if now is a good time. Works day and night."""
        ts = time.time() if ts is None else ts
        if ts < self.retry_after or ts - self.app.last_chat < 60:
            return None  # let her finish replying to you first
        if not self.room_left():
            return None
        return self.app.mem.notes_todo()

    def tick(self, now: datetime | None = None, ts: float | None = None):
        now = now or datetime.now()
        ts = time.time() if ts is None else ts
        action = self.due(now, ts)
        look = None if action else self.pictures_due(ts)
        job = None if action or look else self.notes_due(ts)
        if look:
            action = "look at pictures"
        elif job:
            action = "make notes"
        if not action:
            return None
        with self.app.lock:
            try:
                if look:
                    self.app.agent.look_at_pictures(*look)
                elif job:
                    self.app.agent.make_notes(*job)
                elif action == "think":
                    self.app.agent.think()
                    self.state["last_think"] = ts
                else:
                    self.app.agent.sleep()
                    self.state["last_sleep_key"] = self.sleep_key(now)
            except (BrainError, BudgetExceeded) as e:
                self.app.mem.log("error", f"Couldn't {action}: {e}")
                self.retry_after = ts + 3600
                if action == "think":
                    self.state["last_think"] = ts
        self._save()
        return action

    def status(self) -> dict:
        now = datetime.now()
        night = self.is_night(now)
        nxt = None
        if not night:
            nxt = int(max(0, self.state.get("last_think", 0) + self.think_every * 60 - time.time()) // 60)
        return {"night": night, "next_thought_in_minutes": nxt}

    def run_forever(self, stop) -> None:
        while not stop.wait(30):
            try:
                self.tick()
            except Exception as e:  # never let the loop die
                self.app.mem.log("error", f"Scheduler problem: {e}")
