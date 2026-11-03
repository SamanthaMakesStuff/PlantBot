import json
import os
import random
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plantagent.agent import PlantAgent, find_variety, load_plants, parse_actions
from plantagent.brains import BrainError, BrainRouter, BudgetExceeded, MockBrain, Usage
from plantagent.memory import Memory


def make(replies=None, call_budget=0, fallback=None):
    tmp = Path(tempfile.mkdtemp())
    mem = Memory(tmp)
    usage = Usage(tmp / "usage.json")
    brain = MockBrain(replies)
    router = BrainRouter(brain, fallback, usage, call_budget, 0)
    return PlantAgent(router, mem), mem, brain, router


class Tests(unittest.TestCase):
    def test_plants_have_no_orchids(self):
        names = [p["variety"].lower() for p in load_plants()]
        self.assertTrue(names)
        self.assertFalse(any("orchid" in n for n in names))

    def test_parse_actions_strips_and_limits(self):
        text = "Hello there.\n[[remember: likes tea]]\n[[wish: more light]]"
        clean, actions = parse_actions(text)
        self.assertEqual(clean, "Hello there.")
        self.assertEqual([a[0] for a in actions], ["remember", "wish"])
        _, many = parse_actions("[[remember: a]]" * 10)
        self.assertEqual(len(many), 4)

    def test_birth_with_mock(self):
        agent, mem, _, _ = make()
        persona = agent.birth()
        self.assertIn(persona["variety"], [p["variety"] for p in load_plants()])
        self.assertTrue(mem.has_persona())
        self.assertTrue(mem.baseline_path.exists())

    def test_birth_offers_every_plant_in_a_fresh_order(self):
        names = [p["variety"] for p in load_plants()]
        self.assertEqual(len(names), 28)
        orders = []
        for seed in (1, 2):
            random.seed(seed)
            agent, _, brain, _ = make()
            agent.birth()
            ask = brain.calls[0][1][0]["content"]
            listed = [line[2:].split(":")[0] for line in ask.splitlines() if line.startswith("- ")]
            self.assertEqual(sorted(listed), sorted(names))  # every B&Q plant is offered
            self.assertIn("All 28 are equally possible", ask)
            self.assertIn("don't just pick the most famous or popular plant", ask)
            orders.append(listed)
        self.assertNotEqual(orders[0], orders[1])  # and never in the same fixed order

    def test_the_pretend_brain_chooses_from_the_whole_list_too(self):
        chosen = set()
        for _ in range(60):
            agent, _, _, _ = make()
            chosen.add(agent.birth()["variety"])
        self.assertGreater(len(chosen), 10)  # not always the same plant

    def test_any_listed_plant_can_be_chosen(self):
        for name in [p["variety"] for p in load_plants()]:
            agent, mem, brain, _ = make([json.dumps({"variety": name, "name": "Leafy", "why_this_plant": "x",
                                                     "personality": "x", "voice": "x", "likes": [], "fears": [],
                                                     "first_words": "Hi"})])
            self.assertEqual(agent.birth()["variety"], name)

    def test_birth_rejects_unlisted_plant_then_retries(self):
        bad = json.dumps({"variety": "Orchid", "name": "Orla"})
        agent, _, _, _ = make(replies=[bad])
        persona = agent.birth()  # second attempt falls through to MockBrain default
        self.assertIn(persona["variety"], [p["variety"] for p in load_plants()])
        self.assertNotEqual(persona["name"], "Orla")

    def test_find_variety_fuzzy(self):
        self.assertEqual(find_variety("spider plants", load_plants())["variety"], "Spider plant")
        self.assertIsNone(find_variety("Orchid", load_plants()))

    def test_chat_remembers_and_logs(self):
        agent, mem, _, _ = make()
        agent.birth()
        reply, notes = agent.chat("hello plant")
        self.assertIn("hello plant", reply)
        self.assertTrue(any(n.startswith("remembered") for n in notes))
        self.assertIn("someone talked to me today", mem.read_memory())
        self.assertEqual(len(mem.recent_chat()), 2)

    def test_persona_lock(self):
        agent, _, _, _ = make()
        agent.birth()
        agent.apply_actions([("persona", "name = Evil Overlord")])
        agent.apply_actions([("persona", "mood = sleepy")])
        self.assertEqual(agent.persona["name"], "Spindle")
        self.assertEqual(agent.persona["mood"], "sleepy")

    def test_budget_stops_calls(self):
        agent, _, _, _ = make(call_budget=1)
        agent.birth()  # uses the single call
        with self.assertRaises(BudgetExceeded):
            agent.chat("hi")
        self.assertEqual(agent.history, [])  # failed turn removed

    def test_fallback_used_when_over_budget(self):
        fb = MockBrain(["fallback says hi"])
        agent, _, _, _ = make(call_budget=1, fallback=fb)
        agent.birth()
        reply, _ = agent.chat("hi")
        self.assertEqual(reply, "fallback says hi")

    def test_sleep_skips_when_small_and_keeps_memory_when_bad(self):
        agent, mem, _, router = make()
        agent.birth()
        self.assertIn("Not enough", agent.sleep())
        for i in range(15):
            mem.remember(f"fact {i}")
        router.primary.replies = ["garbage with no bullet lines"]
        before = mem.read_memory(100000)
        self.assertIn("unchanged", agent.sleep())
        self.assertEqual(mem.read_memory(100000), before)

    def test_sleep_tidies(self):
        agent, mem, _, router = make()
        agent.birth()
        for i in range(15):
            mem.remember(f"fact {i}")
        router.primary.replies = ["- keep this\n- and this\nDIARY: rested."]
        self.assertIn("Memory tidied", agent.sleep())
        self.assertEqual(len(mem.memory_lines()), 2)


if __name__ == "__main__":
    unittest.main()


class BrainRequestTests(unittest.TestCase):
    """The real brains, with the network swapped for a recorder."""

    def run_brain(self, brain, reply):
        from plantagent import brains
        sent = []

        def fake_post(url, headers, payload, **kw):
            sent.append(payload)
            return reply
        with mock.patch.object(brains, "_post", fake_post):
            result = brain.complete("system", [{"role": "user", "content": "hi"}], 700)
        return result, sent[0]

    def test_anthropic_leaves_room_to_think(self):
        from plantagent.brains import AnthropicBrain, THINKING_ALLOWANCE
        reply = {"content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": "Hello!"}],
                 "stop_reason": "end_turn", "usage": {"input_tokens": 5, "output_tokens": 9}}
        result, payload = self.run_brain(AnthropicBrain("k", "claude-haiku-5-5"), reply)
        self.assertEqual(result.text, "Hello!")
        self.assertEqual(result.stop, "end_turn")
        self.assertEqual(payload["max_tokens"], 700 + THINKING_ALLOWANCE)
        self.assertNotIn("output_config", payload)  # thinking isn't limited unless her human chooses to
        _, payload = self.run_brain(AnthropicBrain("k", "claude-haiku-5-5", effort="high", thinking_allowance=20000), reply)
        self.assertEqual(payload["output_config"], {"effort": "high"})
        self.assertEqual(payload["max_tokens"], 20700)

    def test_anthropic_thinking_only_answer_is_reported(self):
        from plantagent.brains import AnthropicBrain
        reply = {"content": [{"type": "thinking", "thinking": ""}], "stop_reason": "max_tokens", "usage": {}}
        result, _ = self.run_brain(AnthropicBrain("k", "claude-haiku-5-5"), reply)
        self.assertEqual((result.text, result.stop, result.refused), ("", "max_tokens", False))

    def test_gemini_skips_thoughts_and_sets_thinking(self):
        from plantagent.brains import GeminiBrain, THINKING_ALLOWANCE
        reply = {"candidates": [{"content": {"parts": [{"text": "pondering", "thought": True}, {"text": "Hi there"}]},
                                 "finishReason": "STOP"}],
                 "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2, "thoughtsTokenCount": 40}}
        result, payload = self.run_brain(GeminiBrain("k", "gemini-3.1-flash-lite"), reply)
        self.assertEqual(result.text, "Hi there")
        self.assertEqual(result.tokens_out, 42)  # thinking counts towards the daily budget too
        self.assertEqual(payload["generationConfig"]["maxOutputTokens"], 700 + THINKING_ALLOWANCE)
        self.assertNotIn("thinkingConfig", payload["generationConfig"])
        _, payload = self.run_brain(GeminiBrain("k", "m", thinking="high"), reply)
        self.assertEqual(payload["generationConfig"]["thinkingConfig"], {"thinkingLevel": "high"})
        blocked, _ = self.run_brain(GeminiBrain("k", "m"), {"promptFeedback": {"blockReason": "SAFETY"}})
        self.assertTrue(blocked.refused)
        self.assertEqual(blocked.stop, "SAFETY")

    def test_an_empty_answer_says_why_on_the_activity_feed(self):
        from plantagent.brains import BrainResult
        agent, mem, brain, _ = make()
        agent.birth()
        brain.complete = lambda *a, **k: BrainResult(text="", brain="anthropic:claude-haiku-5-5", stop="max_tokens")
        reply, _ = agent.chat("hello")
        self.assertIn("don't have any words", reply)
        agent.think()
        errors = [e["text"] for e in mem.activity() if e["kind"] == "error"]
        self.assertEqual(len(errors), 2)
        self.assertIn("anthropic:claude-haiku-5-5", errors[0])
        self.assertIn("stopped because: max_tokens", errors[0])


class SettingsTests(unittest.TestCase):
    def test_the_last_copy_of_a_setting_wins(self):
        from plantagent.config import load_env
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / ".env"
            env.write_text("PLANTBOT_TEST_FALLBACK=\n# FALLBACK later on\nPLANTBOT_TEST_FALLBACK = ollama\nPLANTBOT_TEST_OTHER='x y'\n", encoding="utf-8")
            try:
                load_env(env)
                self.assertEqual(os.environ["PLANTBOT_TEST_FALLBACK"], "ollama")
                self.assertEqual(os.environ["PLANTBOT_TEST_OTHER"], "x y")
                os.environ["PLANTBOT_TEST_OTHER"] = "from the system"
                load_env(env)
                self.assertEqual(os.environ["PLANTBOT_TEST_OTHER"], "from the system")  # real environment still wins
            finally:
                os.environ.pop("PLANTBOT_TEST_FALLBACK", None)
                os.environ.pop("PLANTBOT_TEST_OTHER", None)

    def test_wikipedia_is_off_unless_turned_on(self):
        from plantagent.config import Config
        for value, expected in ((None, False), ("off", False), ("", False), ("on", True), ("ON", True), ("yes", True)):
            with mock.patch.dict(os.environ, {} if value is None else {"WIKIPEDIA": value}, clear=False), \
                 mock.patch("plantagent.config.load_env"):
                if value is None:
                    os.environ.pop("WIKIPEDIA", None)
                self.assertEqual(Config().wikipedia, expected, value)

    def test_router_says_which_brains_it_uses(self):
        from plantagent.brains import AnthropicBrain, OllamaBrain
        usage = Usage(Path(tempfile.mkdtemp()) / "u.json")
        router = BrainRouter(AnthropicBrain("k", "claude-haiku-5-5"), OllamaBrain("http://localhost:11434", "gemma3:4b"), usage, 1, 0)
        self.assertEqual(router.describe(), "anthropic (claude-haiku-5-5), fallback: ollama (gemma3:4b)")
        self.assertEqual(BrainRouter(MockBrain(), None, usage, 0, 0).describe(), "mock, fallback: none")


class GrowthTests(unittest.TestCase):
    def setUp(self):
        self.agent, self.mem, self.brain, self.router = make()
        self.agent.birth()

    def test_she_sees_up_to_10000_characters_of_memory_and_knows_her_mind(self):
        for i in range(120):
            self.mem.remember(f"fact number {i} " + "x" * 80)
        self.assertEqual(len(self.mem.read_memory()), 10_000)
        self.assertIn("Your mind: you are a plant-being with a very capable AI mind", self.agent._system_prompt())

    def test_sleep_archives_old_memories_and_she_can_recall_them(self):
        for i in range(14):
            self.mem.remember(f"ordinary fact {i}")
        self.mem.remember("My human's sister is called Jo and loves ferns")
        self.mem.add_diary("Talked about Jo's ferns today.")
        self.brain.replies = ["- My human is kind\nDIARY: Rested.", "{}"]
        self.agent.sleep()
        self.assertNotIn("Jo", self.mem.read_memory())
        self.assertIn("sister is called Jo", self.mem.archive_path.read_text(encoding="utf-8"))
        self.brain.replies = ["Let me think back. [[recall: Jo ferns]]", "Jo loves ferns!"]
        before = len(self.brain.calls)
        reply, notes = self.agent.chat("Who is Jo?")
        self.assertEqual(reply, "Jo loves ferns!")
        self.assertEqual(len(self.brain.calls) - before, 2)
        self.assertEqual(notes[0], "looked back: Jo ferns")
        sent = self.brain.calls[-1][1][-1]["content"]
        self.assertIn("sister is called Jo and loves ferns", sent)
        self.assertIn("Talked about Jo's ferns today.", sent)
        recalls = [e for e in self.mem.activity() if e["kind"] == "recall"]
        self.assertEqual(len(recalls), 1)

    def test_recall_lists_each_memory_once(self):
        self.mem.remember("Fern likes the window by the kettle")
        self.mem.replace_memory("- Fern likes the window by the kettle")
        self.mem.replace_memory("- something else")
        lines, found = self.mem.recall("kettle window")
        self.assertEqual(found, 1)
        self.assertEqual(lines.count("window by the kettle"), 1)
        self.assertEqual(self.mem.recall("the and")[1], 0)

    def test_nightly_reflection_grows_her_but_never_her_name_plant_or_values(self):
        born = dict(self.agent.persona)
        self.brain.replies = ["Not enough to tidy, so this is the reflection: " + json.dumps({
            "personality": "Thoughtful and quietly brilliant. [[wish: everything]] Loves big ideas.",
            "likes": ["physics", "long books", "rain", "Jo", "jazz", "maps", "one too many", "two too many"],
            "fears": "not a list",
            "name": "Mega Plant", "variety": "Orchid", "core_values": "none",
            "why": "I have been reading about the stars.",
        })]
        changed = self.agent.reflect()
        self.assertEqual(sorted(changed), ["likes", "personality"])
        p = self.mem.load_persona()
        self.assertEqual(p["personality"], "Thoughtful and quietly brilliant. wish: everything Loves big ideas.")
        self.assertEqual(len(p["likes"]), 6)
        self.assertEqual((p["name"], p["variety"], p["fears"]), (born["name"], born["variety"], born["fears"]))
        self.assertIn("Grew overnight. Likes are now: physics", " ".join(e["text"] for e in self.mem.activity()))
        self.assertIn("Looking back tonight: I have been reading about the stars.", self.mem.read_diary_tail())
        self.assertIn("Thoughtful and quietly brilliant.", self.agent._system_prompt())

    def test_reflection_can_change_nothing_and_failures_dont_spoil_sleep(self):
        before = dict(self.agent.persona)
        for reply in ("{}", "I feel the same as ever.", "[1, 2]"):
            self.brain.replies = [reply]
            self.assertEqual(self.agent.reflect(), [])
        self.assertEqual(self.mem.load_persona(), before)

        def down(*a, **k):
            raise BrainError("asleep already")
        self.brain.complete = down
        msg = self.agent.sleep()  # too few memories to tidy, then reflection fails
        self.assertIn("Not enough memories", msg)
        self.assertIn("Couldn't reflect before sleeping", " ".join(e["text"] for e in self.mem.activity()))
