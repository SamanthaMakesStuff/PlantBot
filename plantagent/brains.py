"""The 'brains': Claude, Gemini, local Ollama, and a mock for testing.

Only Python's standard library is used, so there is nothing extra to install.
"""
from __future__ import annotations

import json
import random
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


class BrainError(Exception):
    pass


class BudgetExceeded(Exception):
    pass


# Newer models think before they answer, and that thinking comes out of the same token allowance as
# the reply. Without extra room, a long think can use it all up and leave her with nothing to say.
THINKING_ALLOWANCE = 8000


# A message's content is usually a string. To show pictures it can be a list of parts:
# {"type": "text", "text": ...} and {"type": "image", "mime": "image/jpeg", "data": <base64 string>}.
def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(p.get("text", "") for p in content if p.get("type") == "text")


def _images_of(content) -> list:
    return [] if isinstance(content, str) else [p for p in content if p.get("type") == "image"]


# Her instructions can come as a list of layers, from the most stable to the least (the last holds the clock).
# Claude caches everything up to the end of each stable layer, so repeat calls re-read it at a tenth of the price.
def _joined(system) -> str:
    return system if isinstance(system, str) else "\n\n".join(s for s in system if s)


@dataclass
class BrainResult:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    refused: bool = False
    brain: str = ""
    stop: str = ""  # why the brain stopped, as it reported it (e.g. end_turn, max_tokens, SAFETY)
    cache_read: int = 0   # prompt tokens re-read from Claude's cache (a tenth of the price)
    cache_write: int = 0  # prompt tokens written to it (a quarter extra)
    fallback: bool = False  # True if the backup brain answered


def _post(url: str, headers: dict, payload: dict, timeout: int = 90, retries: int = 2) -> dict:
    data = json.dumps(payload).encode("utf-8")
    last: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json", **headers},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:500]
            last = BrainError(f"HTTP {e.code}: {body}")
            if e.code in (429, 500, 502, 503, 529) and attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            raise last
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = BrainError(f"network error: {e}")
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            raise last
    raise last or BrainError("unknown error")


class AnthropicBrain:
    name = "anthropic"

    def __init__(self, api_key: str, model: str, effort: str = "", thinking_allowance: int = THINKING_ALLOWANCE) -> None:
        if not api_key:
            raise BrainError("ANTHROPIC_API_KEY is missing from your .env file")
        self.key, self.model, self.effort, self.room = api_key, model, effort, thinking_allowance

    def complete(self, system, messages: list, max_tokens: int, cache: str = "") -> BrainResult:
        def content(c):
            if isinstance(c, str):
                return c
            return [{"type": "image", "source": {"type": "base64", "media_type": p["mime"], "data": p["data"]}}
                    if p.get("type") == "image" else {"type": "text", "text": p.get("text", "")} for p in c]
        messages = [{"role": m["role"], "content": content(m["content"])} for m in messages]
        if isinstance(system, str) or not cache:
            system = _joined(system)
        else:  # cache each layer (at most three; the conversation can take the fourth marker)
            layers = [s for s in system if s]
            system = [{"type": "text", "text": text, **({"cache_control": {"type": "ephemeral"}} if i < 3 else {})}
                      for i, text in enumerate(layers)]
        payload = {"model": self.model, "max_tokens": max_tokens + self.room, "system": system, "messages": messages}
        if cache == "conversation" and len(messages) >= 2:
            # A chat: cache the conversation up to her last reply. The newest message (which also carries the time)
            # comes after the mark, so next turn's request starts with exactly what was cached and re-reads it.
            before = messages[-2]["content"]
            blocks = [{"type": "text", "text": before}] if isinstance(before, str) else list(before)
            blocks[-1] = {**blocks[-1], "cache_control": {"type": "ephemeral"}}
            messages[-2] = {"role": messages[-2]["role"], "content": blocks}
        if self.effort:
            payload["output_config"] = {"effort": self.effort}  # only if her human chose one; else the model's own
        out = _post(
            "https://api.anthropic.com/v1/messages",
            {"x-api-key": self.key, "anthropic-version": "2023-06-01"},
            payload,
            timeout=90 + max_tokens // 40,  # writing out several scanned pages takes a while
        )
        text = "".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text")
        usage = out.get("usage", {})
        read, wrote = usage.get("cache_read_input_tokens") or 0, usage.get("cache_creation_input_tokens") or 0
        return BrainResult(
            text=text.strip(),
            tokens_in=usage.get("input_tokens", 0) + round(wrote * 1.25) + round(read * 0.1),  # counted at their cost
            cache_read=read,
            cache_write=wrote,
            tokens_out=usage.get("output_tokens", 0),
            refused=(out.get("stop_reason") == "refusal"),
            brain=f"anthropic:{self.model}",
            stop=str(out.get("stop_reason") or ""),
        )


class GeminiBrain:
    name = "gemini"

    def __init__(self, api_key: str, model: str, thinking: str = "", thinking_allowance: int = THINKING_ALLOWANCE) -> None:
        if not api_key:
            raise BrainError("GEMINI_API_KEY is missing from your .env file")
        self.key, self.model, self.thinking, self.room = api_key, model, thinking, thinking_allowance

    def complete(self, system, messages: list, max_tokens: int, cache: str = "") -> BrainResult:
        def parts(c):
            if isinstance(c, str):
                return [{"text": c}]
            return [({"inline_data": {"mime_type": p["mime"], "data": p["data"]}} if p["mime"] != "image/gif"
                     else {"text": "(a GIF picture, which this brain can't open)"})
                    if p.get("type") == "image" else {"text": p.get("text", "")} for p in c]
        contents = [{"role": "model" if m["role"] == "assistant" else "user", "parts": parts(m["content"])}
                    for m in messages]
        generation = {"maxOutputTokens": max_tokens + self.room}
        if self.thinking:
            generation["thinkingConfig"] = {"thinkingLevel": self.thinking}
        out = _post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
            {"x-goog-api-key": self.key},
            {
                "systemInstruction": {"parts": [{"text": _joined(system)}]},
                "contents": contents,
                "generationConfig": generation,
            },
            timeout=90 + max_tokens // 40,
        )
        cands = out.get("candidates") or []
        if not cands:
            blocked = (out.get("promptFeedback") or {}).get("blockReason", "")
            return BrainResult(text="", refused=True, brain=f"gemini:{self.model}", stop=str(blocked or "no answer"))
        parts = cands[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        usage = out.get("usageMetadata", {})
        return BrainResult(
            text=text.strip(),
            tokens_in=usage.get("promptTokenCount", 0),
            tokens_out=usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0),
            brain=f"gemini:{self.model}",
            stop=str(cands[0].get("finishReason") or ""),
        )


class OllamaBrain:
    name = "ollama"

    def __init__(self, url: str, model: str) -> None:
        self.url, self.model = url.rstrip("/"), model

    def complete(self, system, messages: list, max_tokens: int, cache: str = "") -> BrainResult:
        out = _post(
            f"{self.url}/api/chat",
            {},
            {
                "model": self.model,
                "stream": False,
                "messages": [{"role": "system", "content": _joined(system)}] + [
                    {"role": m["role"], "content": _text_of(m["content"]),
                     **({"images": [p["data"] for p in _images_of(m["content"])]} if _images_of(m["content"]) else {})}
                    for m in messages],
                "options": {"num_predict": max_tokens},
            },
            timeout=300,
            retries=0,
        )
        return BrainResult(
            text=out.get("message", {}).get("content", "").strip(),
            tokens_in=out.get("prompt_eval_count", 0),
            tokens_out=out.get("eval_count", 0),
            brain=f"ollama:{self.model}",
        )


class MockBrain:
    """Gives scripted answers so you can try the program with no API key."""

    name = "mock"

    def __init__(self, replies=None) -> None:
        self.replies = list(replies) if replies else []
        self.calls = []

    def complete(self, system, messages: list, max_tokens: int, cache: str = "") -> BrainResult:
        system = _joined(system)
        self.calls.append((system, messages))
        last = _text_of(messages[-1]["content"])
        shown = re.findall(r"^(Picture|Page) (\d+):$", last, re.M)
        if self.replies:
            text = self.replies.pop(0)
        elif "come into being" in system:
            # A pretend birth: any of the plants it was offered, at random, like a real brain could choose.
            offered = re.findall(r"^- (.+?): ", last, re.M) or ["Spider plant"]
            variety = random.choice(offered)
            text = json.dumps({
                "variety": variety,
                "name": "Spindle",
                "why_this_plant": f"A {variety.lower()} felt right: it suits the way I like to live.",
                "personality": "Gentle, curious and a bit giddy. Easily delighted by light.",
                "voice": "Soft, playful, short sentences.",
                "likes": ["morning light", "being talked to"],
                "fears": ["being forgotten for weeks"],
                "first_words": "Oh! Hello. Is that light? I think I like light.",
            })
        elif "your own quiet time" in last:
            text = "[[diary: A quiet moment. I noticed the light moving.]]"
        elif "who you've been becoming" in last:
            text = '{"likes": ["morning light", "being talked to", "long books"], "why": "I have been reading a lot."}'
        elif "write yourself reading notes" in last:
            text = "SUMMARY: A document my human shared with me.\n- Mock note one\n- Mock note two"
        elif "tidy your memory" in last:
            text = "- Mock memory line one\nDIARY: I tidied my thoughts."
        elif shown:
            text = "\n".join(f"PAGE {n}:\nMock words written out from scanned page {n}." if kind == "Page"
                             else f"{n}: A mock picture, number {n}." for kind, n in shown)
        else:
            text = "(mock plant) I heard you say: " + last[:80] + "\n[[remember: someone talked to me today]]"
        return BrainResult(text=text, tokens_in=len(system) // 4, tokens_out=len(text) // 4, brain="mock")


class Usage:
    """Counts today's calls and tokens so a daily cap can be enforced."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.state = {"date": self._today(), "calls": 0, "tokens": 0, "cache_read": 0, "cache_write": 0}
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if saved.get("date") == self._today():
                self.state = saved
        except Exception:
            pass

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def snapshot(self) -> dict:
        if self.state["date"] != self._today():
            self.state = {"date": self._today(), "calls": 0, "tokens": 0, "cache_read": 0, "cache_write": 0}
        return dict(self.state)

    def count(self, name: str) -> int:
        """Counts one more of something rationed per day (like Wikipedia lookups). Returns today's total."""
        self.snapshot()
        self.state[name] = self.state.get(name, 0) + 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state), encoding="utf-8")
        return self.state[name]

    def add(self, calls: int, tokens: int, cache_read: int = 0, cache_write: int = 0) -> None:
        self.snapshot()
        self.state["calls"] += calls
        self.state["tokens"] += tokens
        self.state["cache_read"] = self.state.get("cache_read", 0) + cache_read
        self.state["cache_write"] = self.state.get("cache_write", 0) + cache_write
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state), encoding="utf-8")


class BrainRouter:
    """Primary brain, optional fallback, and a daily spending cap."""

    def __init__(self, primary, fallback, usage: Usage, call_budget: int, token_budget: int) -> None:
        self.primary, self.fallback, self.usage = primary, fallback, usage
        self.call_budget, self.token_budget = call_budget, token_budget
        self.last_error = ""

    def describe(self) -> str:
        """Which brains are in use, for the start-up message, e.g. "anthropic (claude-haiku-5-5), fallback: ollama (gemma3:4b)"."""
        def one(b):
            return f"{b.name} ({b.model})" if getattr(b, "model", "") else b.name
        return one(self.primary) + ", fallback: " + (one(self.fallback) if self.fallback else "none")

    def over_budget(self) -> bool:
        u = self.usage.snapshot()
        return bool(
            (self.call_budget and u["calls"] >= self.call_budget)
            or (self.token_budget and u["tokens"] >= self.token_budget)
        )

    def complete(self, system, messages: list, max_tokens: int, light: list | None = None,
                 use_fallback: bool = True, cache: str = "") -> BrainResult:
        """system: a string, or a list of layers from most to least stable (see _joined).
        light: shorter messages for the fallback brain (a small local model can't take a whole book).
        use_fallback=False: main brain only, for jobs a small model would do badly.
        cache: only for chats, where another request usually follows within the cache's 5 minutes. Writing to the
        cache costs a quarter extra and re-reading it saves nine tenths, so a one-off request (a quiet thought, a
        night's reflection, a background job) is cheaper without it.
          "instructions": cache her instructions' stable layers (a chat message carrying a document).
          "conversation": cache those and the conversation so far (an ordinary chat message).
        Never cache a whole document: writing 90,000 tokens to a cache nobody re-reads only costs extra."""
        fallback = self.fallback if use_fallback else None
        if not self.over_budget():
            try:
                result = self.primary.complete(system, messages, max_tokens, cache=cache)
                self.usage.add(1, result.tokens_in + result.tokens_out, result.cache_read, result.cache_write)
                return result
            except BrainError as e:
                self.last_error = str(e)
                if not fallback:
                    raise
        elif not fallback:
            raise BudgetExceeded("Today's call or token budget is used up.")
        result = fallback.complete(system, light or messages, max_tokens)
        result.fallback = True
        return result


def build_router(cfg, mock: bool = False) -> BrainRouter:
    usage = Usage(cfg.data_dir / "usage.json")

    def make(kind: str):
        if kind == "anthropic":
            return AnthropicBrain(cfg.anthropic_key, cfg.anthropic_model, cfg.anthropic_effort, cfg.thinking_allowance)
        if kind == "gemini":
            return GeminiBrain(cfg.gemini_key, cfg.gemini_model, cfg.gemini_thinking, cfg.thinking_allowance)
        if kind == "ollama":
            return OllamaBrain(cfg.ollama_url, cfg.ollama_model)
        raise BrainError(f"Unknown brain '{kind}'. Use anthropic, gemini or ollama.")

    if mock:
        return BrainRouter(MockBrain(), None, usage, 0, 0)
    fallback = make(cfg.fallback_brain) if cfg.fallback_brain else None
    return BrainRouter(make(cfg.brain), fallback, usage, cfg.daily_call_budget, cfg.daily_token_budget)
