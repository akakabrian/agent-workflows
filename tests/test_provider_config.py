"""Tests for config-driven custom providers (agent_workflows.adapters._config).

Each test isolates the environment and points OWF_PROVIDERS_FILE at a temp path
so a real ~/.workflows/providers.json (if any) never leaks in.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_workflows.adapters import (
    AdapterError,
    AnthropicAPIAdapter,
    GeminiAPIAdapter,
    OpenAICompatibleAdapter,
    build_adapter,
    list_providers,
)


def _clean_env(**extra):
    """Environment with no OWF_PROVIDER_* leakage plus a non-existent file path."""
    env = {k: v for k, v in __import__("os").environ.items() if not k.startswith("OWF_PROVIDER")}
    env["OWF_PROVIDERS_FILE"] = "/nonexistent/owf-providers.json"
    env.update(extra)
    return env


class EnvProviderTests(unittest.TestCase):
    def test_env_registers_openai_compatible_provider(self):
        env = _clean_env(
            OWF_PROVIDER_GROQ_BASE_URL="https://api.groq.com/openai/v1",
            OWF_PROVIDER_GROQ_API_KEY_ENV="GROQ_API_KEY",
            OWF_PROVIDER_GROQ_MODEL="llama-3.3-70b",
        )
        with mock.patch.dict("os.environ", env, clear=True):
            adapter = build_adapter("groq")
            self.assertIsInstance(adapter, OpenAICompatibleAdapter)
            self.assertEqual(adapter.base_url, "https://api.groq.com/openai/v1")
            self.assertEqual(adapter.api_key_env, "GROQ_API_KEY")
            self.assertEqual(adapter.default_model, "llama-3.3-70b")
            self.assertIn("groq", {p["name"] for p in list_providers()})

    def test_env_kind_anthropic(self):
        env = _clean_env(
            OWF_PROVIDER_MYCLAUDE_KIND="anthropic",
            OWF_PROVIDER_MYCLAUDE_API_KEY_ENV="MY_KEY",
            OWF_PROVIDER_MYCLAUDE_MODEL="claude-opus-4-8",
        )
        with mock.patch.dict("os.environ", env, clear=True):
            adapter = build_adapter("myclaude")
            self.assertIsInstance(adapter, AnthropicAPIAdapter)
            self.assertEqual(adapter.api_key_env, "MY_KEY")
            self.assertEqual(adapter.default_model, "claude-opus-4-8")

    def test_env_kind_gemini(self):
        env = _clean_env(
            OWF_PROVIDER_VERTEX_KIND="gemini",
            OWF_PROVIDER_VERTEX_MODEL="gemini-3.5-flash",
        )
        with mock.patch.dict("os.environ", env, clear=True):
            self.assertIsInstance(build_adapter("vertex"), GeminiAPIAdapter)

    def test_builtin_takes_precedence_and_unknown_raises(self):
        with mock.patch.dict("os.environ", _clean_env(), clear=True):
            self.assertIsInstance(build_adapter("openai"), OpenAICompatibleAdapter)
            with self.assertRaises(AdapterError):
                build_adapter("does-not-exist")

    def test_incomplete_env_provider_errors_only_when_built(self):
        env = _clean_env(OWF_PROVIDER_BROKEN_BASE_URL="https://x/v1")  # no key/model
        with mock.patch.dict("os.environ", env, clear=True):
            # Building another provider still works; the broken one is lazy.
            self.assertIsInstance(build_adapter("openai"), OpenAICompatibleAdapter)
            with self.assertRaises(AdapterError):
                build_adapter("broken")


class FileProviderTests(unittest.TestCase):
    def test_file_registers_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "providers.json"
            path.write_text(
                json.dumps(
                    {
                        "providers": {
                            "local": {
                                "base_url": "http://localhost:11434/v1",
                                "api_key_env": "OLLAMA_KEY",
                                "default_model": "qwen2.5",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            env = _clean_env(OWF_PROVIDERS_FILE=str(path))
            with mock.patch.dict("os.environ", env, clear=True):
                adapter = build_adapter("local")
                self.assertIsInstance(adapter, OpenAICompatibleAdapter)
                self.assertEqual(adapter.base_url, "http://localhost:11434/v1")

    def test_env_overrides_file_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "providers.json"
            path.write_text(
                json.dumps(
                    {"providers": {"local": {"base_url": "http://a/v1", "api_key_env": "K", "default_model": "m1"}}}
                ),
                encoding="utf-8",
            )
            env = _clean_env(OWF_PROVIDERS_FILE=str(path), OWF_PROVIDER_LOCAL_MODEL="m2")
            with mock.patch.dict("os.environ", env, clear=True):
                self.assertEqual(build_adapter("local").default_model, "m2")


if __name__ == "__main__":
    unittest.main()
