"""Tests for the reliability/observability features: provider-agnostic caching,
per-provider concurrency config, cross-run usage rollups, and the price
override/refresh path. HTTP is stubbed; no network.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from agent_workflows.adapters import _common
from agent_workflows.adapters._config import provider_concurrency
from agent_workflows.runtime import WorkflowRuntime, run_script, run_status, usage_rollup

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


def _completion(content):
    return _Resp(200, {"choices": [{"message": {"content": content}}], "usage": {"prompt_tokens": 3, "completion_tokens": 2}})


def _script(root: Path, body: str) -> Path:
    path = root / "wf.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


class CacheAcrossProvidersTests(unittest.TestCase):
    @mock.patch.dict("os.environ", {"OPENAI_API_KEY": "sk"})
    def test_api_response_is_cached_and_replayed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / ".workflows"
            script = _script(root, """
                from workflows import agent, meta
                meta(name="cache-test")
                async def main(args):
                    r = await agent("ping", label="p")
                    return {"text": r.text}
            """)
            with mock.patch(_URLOPEN, return_value=_completion("pong")) as urlopen:
                first = run_script(script, home=home, provider="openai", model="gpt-5.4-mini")
                second = run_script(script, home=home, provider="openai", model="gpt-5.4-mini")
            # The live call happened once; the second run replayed from cache.
            self.assertEqual(urlopen.call_count, 1)
            calls = run_status(home, second["run_id"])["calls"]
            self.assertEqual(calls[0]["cache_status"], "hit")
            self.assertEqual(first["status"], "done")


class ConcurrencyConfigTests(unittest.TestCase):
    def test_resolution_precedence(self):
        with mock.patch.dict("os.environ", {"OWF_PROVIDERS_FILE": "/nonexistent.json"}, clear=True):
            self.assertEqual(provider_concurrency("openai"), 8)  # default
        with mock.patch.dict("os.environ", {"OWF_PROVIDERS_FILE": "/nonexistent.json", "OWF_MAX_CONCURRENCY": "2"}, clear=True):
            self.assertEqual(provider_concurrency("openai"), 2)  # global override
        with mock.patch.dict(
            "os.environ",
            {"OWF_PROVIDERS_FILE": "/nonexistent.json", "OWF_MAX_CONCURRENCY": "2", "OWF_PROVIDER_OPENAI_CONCURRENCY": "5"},
            clear=True,
        ):
            self.assertEqual(provider_concurrency("openai"), 5)  # per-provider wins

    def test_runtime_semaphore_uses_configured_limit(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            "os.environ", {"OWF_PROVIDERS_FILE": "/nonexistent.json", "OWF_PROVIDER_OPENAI_CONCURRENCY": "3"}, clear=True
        ):
            runtime = WorkflowRuntime(home=Path(tmp))

            async def grab():
                return runtime.provider_semaphore("openai")._value

            self.assertEqual(asyncio.run(grab()), 3)


class UsageRollupTests(unittest.TestCase):
    def test_rollup_aggregates_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / ".workflows"
            script = _script(root, """
                from workflows import agent, meta
                meta(name="usage")
                async def main(args):
                    await agent("a", label="a")
                    return {}
            """)
            run_script(script, home=home, provider="fake")
            run_script(script, home=home, provider="fake")
            data = usage_rollup(home)
            self.assertEqual(data["runs"], 2)
            self.assertGreaterEqual(data["totals"]["calls"], 2)
            self.assertIn("fake", {row["provider"] for row in data["by_provider"]})


class PriceOverrideTests(unittest.TestCase):
    def test_override_file_beats_static_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prices.json"
            path.write_text(json.dumps({"prices": {"gpt-4o": [1.0, 2.0]}}), encoding="utf-8")
            with mock.patch.dict("os.environ", {"OWF_PRICES_FILE": str(path)}, clear=True):
                # static gpt-4o is (2.50, 10.00); override makes it (1, 2) -> $3 for 1M+1M.
                self.assertAlmostEqual(_common.estimate_cost("gpt-4o", 1_000_000, 1_000_000), 3.0, places=6)

    def test_refresh_writes_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prices.json"
            with mock.patch.dict("os.environ", {"OWF_PRICES_FILE": str(path)}, clear=True):
                with mock.patch(_URLOPEN, return_value=_Resp(200, {"gpt-x": [5.0, 6.0]})):
                    count = _common.refresh_prices("https://prices.example/table.json")
                self.assertEqual(count, 1)
                self.assertTrue(path.is_file())
                self.assertEqual(_common.price_table()["gpt-x"], (5.0, 6.0))


if __name__ == "__main__":
    unittest.main()
