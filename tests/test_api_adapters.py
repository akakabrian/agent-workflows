"""Tests for the HTTP API adapters (OpenAI-compatible, Anthropic, Gemini).

Network is fully stubbed: urllib.request.urlopen is patched so no request ever
leaves the process. Covers happy paths, native structured output, the
schema-validation retry, cost estimation, HTTP errors, missing keys, and the
shared post_json retry/backoff helper.
"""
from __future__ import annotations

import asyncio
import io
import json
import unittest
import urllib.error
from unittest import mock

from agent_workflows.adapters import _common
from agent_workflows.adapters.api import (
    AnthropicAPIAdapter,
    GeminiAPIAdapter,
    OpenAICompatibleAdapter,
)
from agent_workflows.models import WorkflowCallRequest

_URLOPEN = "agent_workflows.adapters._common.urllib.request.urlopen"


class _FakeResponse:
    """Minimal context-manager stand-in for an http.client.HTTPResponse."""

    def __init__(self, status: int, body: dict) -> None:
        self.status = status
        self._body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._body


def _ok(body: dict):
    return _FakeResponse(200, body)


def _run(adapter, **kwargs):
    request = WorkflowCallRequest(prompt=kwargs.pop("prompt", "hello"), **kwargs)
    return asyncio.run(adapter.run(request))


# A simple schema reused across structured-output tests.
_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
}


class OpenAICompatibleTests(unittest.TestCase):
    def _completion(self, content: str, *, prompt_tokens=10, completion_tokens=5):
        return {
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        }

    @mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"})
    def test_happy_path_text_and_cost(self):
        adapter = OpenAICompatibleAdapter()
        with mock.patch(_URLOPEN, return_value=_ok(self._completion("Hi there"))) as urlopen:
            result = _run(adapter, provider="openai", model="gpt-4o-mini")
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "Hi there")
        self.assertEqual(result.input_tokens, 10)
        self.assertEqual(result.output_tokens, 5)
        # gpt-4o-mini = (0.15 in, 0.60 out) per 1M tokens.
        self.assertAlmostEqual(result.estimated_cost_usd, (10 / 1e6) * 0.15 + (5 / 1e6) * 0.60, places=9)
        # Request carried the bearer token and the right endpoint.
        sent = urlopen.call_args.args[0]
        self.assertTrue(sent.full_url.endswith("/chat/completions"))
        self.assertEqual(sent.get_header("Authorization"), "Bearer sk-test")

    @mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"})
    def test_schema_retry_then_success(self):
        adapter = OpenAICompatibleAdapter()
        responses = [
            _ok(self._completion("not json at all")),
            _ok(self._completion(json.dumps({"answer": "42"}))),
        ]
        with mock.patch(_URLOPEN, side_effect=responses) as urlopen:
            result = _run(adapter, provider="openai", model="gpt-4o-mini", schema=_SCHEMA)
        self.assertEqual(urlopen.call_count, 2)  # retried once after invalid JSON
        self.assertTrue(result.ok)
        self.assertEqual(json.loads(result.text), {"answer": "42"})
        self.assertEqual(result.input_tokens, 20)  # tokens summed across attempts
        # _SCHEMA is fully required -> strict json_schema mode, with extra
        # properties forbidden by the strict transform.
        body = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertEqual(body["response_format"]["json_schema"]["strict"], True)
        self.assertFalse(body["response_format"]["json_schema"]["schema"]["additionalProperties"])

    @mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"})
    def test_optional_field_schema_uses_json_object(self):
        adapter = OpenAICompatibleAdapter()
        loose = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}, "required": ["a"]}
        with mock.patch(_URLOPEN, return_value=_ok(self._completion(json.dumps({"a": "x"})))) as urlopen:
            _run(adapter, provider="openai", model="gpt-5.4-mini", schema=loose)
        body = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(body["response_format"], {"type": "json_object"})

    @mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"})
    def test_strict_unsupported_falls_back_to_json_object(self):
        adapter = OpenAICompatibleAdapter()
        reject = urllib.error.HTTPError(
            "u", 400, "Bad", None, io.BytesIO(json.dumps({"error": {"message": "response_format json_schema not supported"}}).encode())
        )
        ok = _ok(self._completion(json.dumps({"answer": "ok"})))
        with mock.patch(_URLOPEN, side_effect=[reject, ok]) as urlopen:
            result = _run(adapter, provider="openai", model="gpt-5.4-mini", schema=_SCHEMA)
        self.assertEqual(urlopen.call_count, 2)  # strict rejected -> retried in object mode
        self.assertTrue(result.ok)
        second = json.loads(urlopen.call_args_list[1].args[0].data)
        self.assertEqual(second["response_format"], {"type": "json_object"})

    @mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"})
    def test_http_error_is_provider_failed(self):
        adapter = OpenAICompatibleAdapter()
        err = urllib.error.HTTPError("u", 400, "Bad", None, io.BytesIO(json.dumps({"error": {"message": "bad model"}}).encode()))
        with mock.patch(_URLOPEN, side_effect=err):
            result = _run(adapter, provider="openai", model="gpt-4o-mini")
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "provider_failed")
        self.assertIn("bad model", result.error)

    def test_missing_key(self):
        adapter = OpenAICompatibleAdapter()
        with mock.patch.dict("os.environ", {}, clear=True):
            result = _run(adapter, provider="openai", model="gpt-4o-mini")
        self.assertFalse(result.ok)
        self.assertIn("OPENAI_API_KEY", result.error)


class AnthropicTests(unittest.TestCase):
    @mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant"})
    def test_text_response(self):
        adapter = AnthropicAPIAdapter()
        body = {"content": [{"type": "text", "text": "Hello"}], "usage": {"input_tokens": 8, "output_tokens": 3}}
        with mock.patch(_URLOPEN, return_value=_ok(body)) as urlopen:
            result = _run(adapter, provider="anthropic", model="claude-3-5-sonnet-latest")
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "Hello")
        self.assertEqual(result.input_tokens, 8)
        sent = urlopen.call_args.args[0]
        self.assertEqual(sent.get_header("X-api-key"), "sk-ant")
        self.assertEqual(sent.get_header("Anthropic-version"), "2023-06-01")

    @mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant"})
    def test_tool_use_structured_output(self):
        adapter = AnthropicAPIAdapter()
        body = {
            "content": [{"type": "tool_use", "name": "emit_result", "input": {"answer": "yes"}}],
            "usage": {"input_tokens": 12, "output_tokens": 4},
        }
        with mock.patch(_URLOPEN, return_value=_ok(body)) as urlopen:
            result = _run(adapter, provider="anthropic", model="claude-3-5-sonnet-latest", schema=_SCHEMA)
        self.assertTrue(result.ok)
        self.assertEqual(json.loads(result.text), {"answer": "yes"})
        # Forced tool-use was requested.
        req_body = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(req_body["tool_choice"], {"type": "tool", "name": "emit_result"})
        self.assertEqual(req_body["tools"][0]["input_schema"], _SCHEMA)


class GeminiTests(unittest.TestCase):
    @mock.patch.dict("os.environ", {"GEMINI_API_KEY": "g-key"})
    def test_text_and_usage(self):
        adapter = GeminiAPIAdapter()
        body = {
            "candidates": [{"content": {"parts": [{"text": "Aloha"}]}}],
            "usageMetadata": {"promptTokenCount": 6, "candidatesTokenCount": 2},
        }
        with mock.patch(_URLOPEN, return_value=_ok(body)) as urlopen:
            result = _run(adapter, provider="gemini", model="gemini-2.0-flash")
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "Aloha")
        self.assertEqual(result.input_tokens, 6)
        sent = urlopen.call_args.args[0]
        self.assertTrue(sent.full_url.endswith(":generateContent"))
        self.assertEqual(sent.get_header("X-goog-api-key"), "g-key")


class CommonHelperTests(unittest.TestCase):
    def test_estimate_cost_known_and_unknown(self):
        self.assertIsNone(_common.estimate_cost("mystery-model", 100, 100))
        self.assertIsNone(_common.estimate_cost("gpt-4o-mini", None, 5))
        cost = _common.estimate_cost("gpt-4o", 1_000_000, 1_000_000)
        self.assertAlmostEqual(cost, 12.50, places=6)  # 2.50 in + 10.00 out

    def test_estimate_cost_longest_prefix_wins(self):
        # "gpt-4o-mini" must win over "gpt-4o" for a mini model name.
        mini = _common.estimate_cost("gpt-4o-mini-2024", 1_000_000, 0)
        self.assertAlmostEqual(mini, 0.15, places=6)

    def test_post_json_retries_then_succeeds(self):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req)
            if len(calls) == 1:
                raise urllib.error.HTTPError("u", 503, "busy", None, io.BytesIO(b"{}"))
            return _ok({"ok": True})

        slept = []
        with mock.patch(_URLOPEN, side_effect=fake_urlopen):
            res = _common.post_json("https://x/y", {"a": 1}, {}, timeout=5, sleep=slept.append)
        self.assertEqual(res.status, 200)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(slept), 1)  # backed off once

    def test_post_json_non_retryable_returns_status(self):
        err = urllib.error.HTTPError("u", 401, "no", None, io.BytesIO(b'{"error":"nope"}'))
        with mock.patch(_URLOPEN, side_effect=err):
            res = _common.post_json("https://x/y", {}, {}, timeout=5, sleep=lambda _: None)
        self.assertEqual(res.status, 401)


if __name__ == "__main__":
    unittest.main()
