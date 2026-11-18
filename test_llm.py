"""Tests for llm.py — model selection, effort, fallback. No network.

Run: python test_llm.py
"""
import types
import unittest
from unittest import mock

import anthropic

import llm


def _status_error(code: int) -> anthropic.APIStatusError:
    # A stand-in response: the SDK's HTTP library has changed between versions,
    # and llm.py only reads status_code.
    resp = types.SimpleNamespace(status_code=code, headers={}, request=None)
    return anthropic.APIStatusError("rejected", response=resp, body=None)


class FakeMessages:
    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def client_with(*outcomes):
    return types.SimpleNamespace(messages=FakeMessages(outcomes))


MSG = [{"role": "user", "content": "hi"}]


class CreateTest(unittest.TestCase):
    def test_primary_call_carries_model_effort_and_thinking_headroom(self):
        c = client_with("ok")
        self.assertEqual(llm.create(c, max_tokens=1024, system="s", messages=MSG), "ok")
        (call,) = c.messages.calls
        self.assertEqual(call["model"], llm.MODEL)
        self.assertEqual(call["extra_body"], {"output_config": {"effort": llm.EFFORT}})
        self.assertEqual(call["max_tokens"], 1024 + llm.THINKING_HEADROOM)
        self.assertEqual((call["system"], call["messages"]), ("s", MSG))

    def test_defaults(self):
        self.assertEqual((llm.MODEL, llm.EFFORT, llm.FALLBACK_MODEL),
                         ("claude-fable-5-2", "medium", "claude-sonnet-4-6"))

    def test_rejected_model_falls_back_once_without_effort(self):
        for code in (400, 403, 404):
            c = client_with(_status_error(code), "fallback-ok")
            with self.assertLogs("llm", level="WARNING") as logs:
                self.assertEqual(llm.create(c, max_tokens=4096, system="s", messages=MSG), "fallback-ok")
            first, second = c.messages.calls
            self.assertEqual(second["model"], llm.FALLBACK_MODEL)
            self.assertNotIn("extra_body", second)          # the old model takes no effort setting
            self.assertEqual(second["max_tokens"], 4096)    # and needs no thinking headroom
            self.assertIn(llm.FALLBACK_MODEL, logs.output[0])

    def test_transient_errors_are_not_masked_by_a_model_switch(self):
        for code in (429, 500, 529):
            c = client_with(_status_error(code), "should-not-be-used")
            with self.assertRaises(anthropic.APIStatusError):
                llm.create(c, max_tokens=1024, system="s", messages=MSG)
            self.assertEqual(len(c.messages.calls), 1)

    def test_fallback_failure_surfaces(self):
        c = client_with(_status_error(404), _status_error(404))
        with self.assertLogs("llm", level="WARNING"), self.assertRaises(anthropic.APIStatusError):
            llm.create(c, max_tokens=1024, system="s", messages=MSG)

    def test_no_fallback_loop_when_fallback_is_the_primary(self):
        c = client_with(_status_error(404), "unused")
        with mock.patch.object(llm, "FALLBACK_MODEL", llm.MODEL), self.assertRaises(anthropic.APIStatusError):
            llm.create(c, max_tokens=1024, system="s", messages=MSG)
        self.assertEqual(len(c.messages.calls), 1)


class TextOfTest(unittest.TestCase):
    def test_skips_thinking_blocks(self):
        blocks = [types.SimpleNamespace(type="thinking", thinking="..."),
                  types.SimpleNamespace(type="text", text='{"a": 1}')]
        self.assertEqual(llm.text_of(types.SimpleNamespace(content=blocks)), '{"a": 1}')
        self.assertEqual(llm.text_of(types.SimpleNamespace(content=blocks[:1])), "")


class WiringTest(unittest.TestCase):
    def test_bot_scripts_call_through_llm(self):
        import pathlib
        for name in ("main.py", "anthropic_news.py", "spacex_pro.py", "anthropic_pro.py"):
            src = pathlib.Path(__file__).with_name(name).read_text(encoding="utf-8")
            self.assertNotIn("client.messages.create(", src, name)   # one place decides the model
            self.assertIn("llm.create(", src, name)
            self.assertNotIn('"claude-sonnet-4-6"', src, name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
