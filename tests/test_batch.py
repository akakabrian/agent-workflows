"""Tests for the standalone batch clients (Anthropic + OpenAI).

All HTTP is stubbed via a small URL/method router patched onto urlopen, so no
request leaves the process. Records are written under a temp home.
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from agent_workflows.adapters import batch as B

_URLOPEN = "agent_workflows.adapters._common.urllib.request.urlopen"


class _Resp:
    def __init__(self, status, body):
        self.status = status
        self._body = body if isinstance(body, str) else json.dumps(body)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body.encode("utf-8")


def _router(routes):
    """routes: list of (method, url_substring, response). First match wins."""

    def fake(req, timeout=None):
        method = req.get_method()
        url = req.full_url
        for m, sub, resp in routes:
            if m == method and sub in url:
                return resp
        raise AssertionError(f"unexpected request {method} {url}")

    return fake


def _prompts_file(root: Path) -> Path:
    path = root / "prompts.jsonl"
    path.write_text(
        "\n".join([json.dumps({"prompt": "one", "custom_id": "a"}), json.dumps({"prompt": "two", "custom_id": "b"})]),
        encoding="utf-8",
    )
    return path


class AnthropicBatchTests(unittest.TestCase):
    @mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant"})
    def test_submit_writes_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            routes = [("POST", "/v1/messages/batches", _Resp(200, {"id": "msgbatch_1", "processing_status": "in_progress"}))]
            with mock.patch(_URLOPEN, side_effect=_router(routes)):
                out = B.submit_batch("anthropic", _prompts_file(root), model=None, home=root)
            self.assertEqual(out["batch_id"], "msgbatch_1")
            self.assertEqual(out["count"], 2)
            record = B.load_record(root, "msgbatch_1")
            self.assertEqual(record["provider"], "anthropic")
            self.assertEqual(record["prompts"]["a"], "one")

    @mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant"})
    def test_fetch_parses_results_and_discounts_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            B.save_record(root, {"batch_id": "msgbatch_1", "provider": "anthropic", "model": "claude-sonnet-4-6",
                                 "count": 1, "prompts": {"a": "one"}})
            results_jsonl = json.dumps({
                "custom_id": "a",
                "result": {"type": "succeeded", "message": {
                    "model": "claude-sonnet-4-6",
                    "content": [{"type": "text", "text": "hi"}],
                    "usage": {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
                }},
            })
            routes = [
                ("GET", "/v1/messages/batches/msgbatch_1", _Resp(200, {"processing_status": "ended", "results_url": "https://r/msgbatch_1"})),
                ("GET", "https://r/msgbatch_1", _Resp(200, results_jsonl)),
            ]
            with mock.patch(_URLOPEN, side_effect=_router(routes)):
                out = B.fetch_batch("msgbatch_1", home=root)
            self.assertTrue(out["done"])
            row = out["results"][0]
            self.assertEqual(row["text"], "hi")
            self.assertEqual(row["prompt"], "one")
            # sonnet-4 = (3 in, 15 out)/1M; 1M+1M tokens -> $18, halved by batch -> $9.
            self.assertAlmostEqual(row["cost_usd"], 9.0, places=6)
            self.assertAlmostEqual(out["total_cost_usd"], 9.0, places=6)

    @mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant"})
    def test_fetch_pending_returns_not_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            B.save_record(root, {"batch_id": "msgbatch_2", "provider": "anthropic", "model": "claude-sonnet-4-6", "count": 1, "prompts": {}})
            routes = [("GET", "/v1/messages/batches/msgbatch_2", _Resp(200, {"processing_status": "in_progress"}))]
            with mock.patch(_URLOPEN, side_effect=_router(routes)):
                out = B.fetch_batch("msgbatch_2", home=root)
            self.assertFalse(out["done"])
            self.assertEqual(out["results"], [])


class OpenAIBatchTests(unittest.TestCase):
    @mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-oa"})
    def test_submit_uploads_then_creates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            routes = [
                ("POST", "/files", _Resp(200, {"id": "file_1"})),
                ("POST", "/batches", _Resp(200, {"id": "batch_1", "status": "validating"})),
            ]
            with mock.patch(_URLOPEN, side_effect=_router(routes)):
                out = B.submit_batch("openai", _prompts_file(root), model="gpt-5.4-mini", home=root)
            self.assertEqual(out["batch_id"], "batch_1")
            self.assertEqual(B.load_record(root, "batch_1")["provider"], "openai")

    @mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-oa"})
    def test_fetch_parses_output_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            B.save_record(root, {"batch_id": "batch_1", "provider": "openai", "model": "gpt-5.4-mini", "count": 1, "prompts": {"a": "one"}})
            output = json.dumps({
                "custom_id": "a",
                "response": {"status_code": 200, "body": {
                    "model": "gpt-5.4-mini",
                    "choices": [{"message": {"content": "answer"}}],
                    "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0},
                }},
            })
            routes = [
                ("GET", "/batches/batch_1", _Resp(200, {"status": "completed", "output_file_id": "out_1"})),
                ("GET", "/files/out_1/content", _Resp(200, output)),
            ]
            with mock.patch(_URLOPEN, side_effect=_router(routes)):
                out = B.fetch_batch("batch_1", home=root)
            row = out["results"][0]
            self.assertEqual(row["text"], "answer")
            # gpt-5.4-mini input $0.75/1M; 1M in, 0 out -> $0.75, halved -> $0.375.
            self.assertAlmostEqual(row["cost_usd"], 0.375, places=6)


class BatchMiscTests(unittest.TestCase):
    def test_load_prompts_accepts_bare_strings_and_assigns_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.jsonl"
            path.write_text('"hello"\n{"prompt": "world"}\n', encoding="utf-8")
            items = B.load_prompts(path)
            self.assertEqual(items[0], {"prompt": "hello", "custom_id": "req-0"})
            self.assertEqual(items[1]["prompt"], "world")

    def test_unsupported_provider_raises(self):
        with self.assertRaises(B.AdapterError):
            B.build_batch_client("gemini")

    def test_missing_key_raises_on_submit(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict("os.environ", {}, clear=True):
            root = Path(tmp)
            with self.assertRaises(B.AdapterError):
                B.submit_batch("anthropic", _prompts_file(root), model=None, home=root)

    def test_list_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            B.save_record(root, {"batch_id": "x1", "provider": "openai", "model": "m", "count": 3})
            ids = {r["batch_id"] for r in B.list_records(root)}
            self.assertIn("x1", ids)

    def test_http_error_surfaces(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "k"}):
            root = Path(tmp)
            err = urllib.error.HTTPError("u", 400, "bad", None, io.BytesIO(b'{"error":"nope"}'))
            with mock.patch(_URLOPEN, side_effect=err):
                with self.assertRaises(B.AdapterError):
                    B.submit_batch("anthropic", _prompts_file(root), model=None, home=root)


if __name__ == "__main__":
    unittest.main()
