"""Tests for agent_workflows.adapters (build_adapter, _real_model, _parse_codex_events)
and runtime worktree helpers (_create_worktree, _finalize_worktree).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import agent_workflows.adapters as adapters
from agent_workflows.adapters import (
    AdapterError,
    ClaudeCLIAdapter,
    CodexCLIAdapter,
    FakeAdapter,
    build_adapter,
)
from agent_workflows.adapters import cli as cli_adapter
from agent_workflows.models import AgentResult
from agent_workflows.runtime import WorkflowRuntime, _Worktree, _create_worktree, _finalize_worktree


# ---------------------------------------------------------------------------
# build_adapter
# ---------------------------------------------------------------------------

class BuildAdapterTests(unittest.TestCase):

    def test_fake_provider(self):
        adapter = build_adapter("fake")
        self.assertIsInstance(adapter, FakeAdapter)

    def test_fixture_provider(self):
        adapter = build_adapter("fixture")
        self.assertIsInstance(adapter, FakeAdapter)

    def test_none_defaults_to_fake(self):
        adapter = build_adapter(None)
        self.assertIsInstance(adapter, FakeAdapter)

    def test_claude_provider(self):
        adapter = build_adapter("claude")
        self.assertIsInstance(adapter, ClaudeCLIAdapter)

    def test_anthropic_provider(self):
        adapter = build_adapter("anthropic")
        self.assertIsInstance(adapter, ClaudeCLIAdapter)

    def test_claude_cli_provider(self):
        adapter = build_adapter("claude-cli")
        self.assertIsInstance(adapter, ClaudeCLIAdapter)

    def test_codex_provider(self):
        adapter = build_adapter("codex")
        self.assertIsInstance(adapter, CodexCLIAdapter)

    def test_codex_cli_provider(self):
        adapter = build_adapter("codex-cli")
        self.assertIsInstance(adapter, CodexCLIAdapter)

    def test_openai_provider_raises(self):
        # "openai" is NOT in the registry (CodexCLIAdapter uses "codex"/"codex-cli")
        with self.assertRaises(AdapterError):
            build_adapter("openai")

    def test_unknown_provider_raises(self):
        with self.assertRaises(AdapterError):
            build_adapter("nope")

    def test_unknown_provider_error_message(self):
        with self.assertRaises(AdapterError) as ctx:
            build_adapter("mystery-provider")
        self.assertIn("mystery-provider", str(ctx.exception))

    def test_case_insensitive_claude(self):
        adapter = build_adapter("CLAUDE")
        self.assertIsInstance(adapter, ClaudeCLIAdapter)

    def test_case_insensitive_codex(self):
        adapter = build_adapter("CODEX")
        self.assertIsInstance(adapter, CodexCLIAdapter)

    def test_case_insensitive_fake(self):
        adapter = build_adapter("FAKE")
        self.assertIsInstance(adapter, FakeAdapter)

    def test_case_insensitive_anthropic(self):
        adapter = build_adapter("Anthropic")
        self.assertIsInstance(adapter, ClaudeCLIAdapter)


# ---------------------------------------------------------------------------
# _real_model
# ---------------------------------------------------------------------------

class RealModelTests(unittest.TestCase):

    def test_none_returns_none(self):
        self.assertIsNone(cli_adapter._real_model(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(cli_adapter._real_model(""))

    def test_fake_returns_none(self):
        self.assertIsNone(cli_adapter._real_model("fake"))

    def test_default_returns_none(self):
        self.assertIsNone(cli_adapter._real_model("default"))

    def test_claude_placeholder_returns_none(self):
        self.assertIsNone(cli_adapter._real_model("claude"))

    def test_codex_placeholder_returns_none(self):
        self.assertIsNone(cli_adapter._real_model("codex"))

    def test_placeholder_case_insensitive_fake(self):
        self.assertIsNone(cli_adapter._real_model("FAKE"))

    def test_placeholder_case_insensitive_default(self):
        self.assertIsNone(cli_adapter._real_model("DEFAULT"))

    def test_placeholder_case_insensitive_claude(self):
        self.assertIsNone(cli_adapter._real_model("CLAUDE"))

    def test_placeholder_case_insensitive_codex(self):
        self.assertIsNone(cli_adapter._real_model("CODEX"))

    def test_real_model_sonnet(self):
        result = cli_adapter._real_model("sonnet")
        self.assertEqual(result, "sonnet")

    def test_real_model_gpt5(self):
        result = cli_adapter._real_model("gpt-5")
        self.assertEqual(result, "gpt-5")

    def test_real_model_preserves_exact_string(self):
        name = "claude-opus-4-5-20250514"
        result = cli_adapter._real_model(name)
        self.assertEqual(result, name)


# ---------------------------------------------------------------------------
# _parse_codex_events
# ---------------------------------------------------------------------------

_REALISTIC_JSONL = "\n".join([
    json.dumps({"type": "thread.started", "thread_id": "x"}),
    json.dumps({"type": "turn.started"}),
    json.dumps({
        "type": "item.completed",
        "item": {"id": "item_0", "type": "agent_message", "text": "hello world"},
    }),
    json.dumps({
        "type": "turn.completed",
        "usage": {
            "input_tokens": 13589,
            "cached_input_tokens": 10112,
            "output_tokens": 5,
            "reasoning_output_tokens": 0,
        },
    }),
])


class ParseCodexEventsTests(unittest.TestCase):

    def test_realistic_stream_usage(self):
        usage, error, text = cli_adapter._parse_codex_events(_REALISTIC_JSONL)
        self.assertEqual(usage["input_tokens"], 13589)
        self.assertEqual(usage["output_tokens"], 5)
        self.assertEqual(usage["cache_read_tokens"], 10112)
        self.assertIsNone(error)

    def test_realistic_stream_message_text(self):
        _, _, text = cli_adapter._parse_codex_events(_REALISTIC_JSONL)
        self.assertEqual(text, "hello world")

    def test_realistic_stream_no_error(self):
        _, error, _ = cli_adapter._parse_codex_events(_REALISTIC_JSONL)
        self.assertIsNone(error)

    def test_turn_failed_yields_error(self):
        stream = json.dumps({"type": "turn.failed", "error": {"message": "boom"}})
        usage, error, text = cli_adapter._parse_codex_events(stream)
        self.assertEqual(error, "boom")

    def test_error_event_yields_error(self):
        stream = json.dumps({"type": "error", "message": "server exploded"})
        _, error, _ = cli_adapter._parse_codex_events(stream)
        self.assertEqual(error, "server exploded")

    def test_empty_string(self):
        usage, error, text = cli_adapter._parse_codex_events("")
        self.assertEqual(usage, {})
        self.assertIsNone(error)
        self.assertEqual(text, "")

    def test_garbage_lines_ignored(self):
        garbage = "not json\n\n   \n!!!\n"
        usage, error, text = cli_adapter._parse_codex_events(garbage)
        self.assertEqual(usage, {})
        self.assertIsNone(error)
        self.assertEqual(text, "")

    def test_mixed_garbage_and_valid(self):
        stream = "not-json\n" + json.dumps({"type": "item.completed", "item": {"id": "i", "type": "agent_message", "text": "hi"}})
        _, _, text = cli_adapter._parse_codex_events(stream)
        self.assertEqual(text, "hi")

    def test_item_without_agent_message_not_extracted(self):
        stream = json.dumps({"type": "item.completed", "item": {"id": "i", "type": "tool_call", "text": "should not capture"}})
        _, _, text = cli_adapter._parse_codex_events(stream)
        self.assertEqual(text, "")

    def test_last_agent_message_wins(self):
        stream = "\n".join([
            json.dumps({"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": "first"}}),
            json.dumps({"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "second"}}),
        ])
        _, _, text = cli_adapter._parse_codex_events(stream)
        self.assertEqual(text, "second")

    def test_cache_read_tokens_via_cached_input_tokens(self):
        stream = json.dumps({
            "type": "turn.completed",
            "usage": {"input_tokens": 100, "cached_input_tokens": 50, "output_tokens": 10},
        })
        usage, _, _ = cli_adapter._parse_codex_events(stream)
        self.assertEqual(usage["cache_read_tokens"], 50)

    def test_cache_read_tokens_via_cache_read_tokens_key(self):
        stream = json.dumps({
            "type": "turn.completed",
            "usage": {"input_tokens": 100, "cache_read_tokens": 40, "output_tokens": 10},
        })
        usage, _, _ = cli_adapter._parse_codex_events(stream)
        self.assertEqual(usage["cache_read_tokens"], 40)


# ---------------------------------------------------------------------------
# Worktree helpers (_create_worktree, _finalize_worktree)
# ---------------------------------------------------------------------------

def _git(args, cwd=None):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)


class WorktreeTests(unittest.TestCase):

    def setUp(self):
        if shutil.which("git") is None:
            self.skipTest("git not found on PATH")

    def _make_git_repo(self, tmp: Path) -> Path:
        repo = tmp / "repo"
        repo.mkdir()
        _git(["-C", str(repo), "init"])
        _git(["-C", str(repo), "config", "user.email", "test@example.com"])
        _git(["-C", str(repo), "config", "user.name", "Test User"])
        # Initial commit
        readme = repo / "README.md"
        readme.write_text("initial\n", encoding="utf-8")
        _git(["-C", str(repo), "add", "README.md"])
        _git(["-C", str(repo), "commit", "-m", "init"])
        return repo

    def _make_runtime(self, repo: Path, run_id: str = "run_test") -> WorkflowRuntime:
        home = repo / ".workflows"
        output_dir = home / "runs" / run_id
        output_dir.mkdir(parents=True, exist_ok=True)
        script = repo / "wf.py"
        script.touch()
        rt = WorkflowRuntime(home=home)
        rt.run_id = run_id
        rt.script_path = script
        rt.current_output_dir = output_dir
        return rt

    def test_create_worktree_returns_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_git_repo(Path(tmp))
            rt = self._make_runtime(repo, "run_test")
            wt = _create_worktree(rt, "call_0001")
            self.assertIsNotNone(wt)
            self.assertIsInstance(wt, _Worktree)
            self.assertTrue(Path(wt.path).exists())

    def test_create_worktree_path_inside_output_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_git_repo(Path(tmp))
            rt = self._make_runtime(repo, "run_test")
            wt = _create_worktree(rt, "call_0001")
            self.assertIsNotNone(wt)
            expected_prefix = str(rt.current_output_dir / "worktrees")
            self.assertTrue(wt.path.startswith(expected_prefix))

    def test_finalize_worktree_with_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_git_repo(Path(tmp))
            rt = self._make_runtime(repo, "run_test2")
            wt = _create_worktree(rt, "call_0001")
            self.assertIsNotNone(wt)
            # Write a new file in the worktree so git detects a change
            new_file = Path(wt.path) / "new_file.txt"
            new_file.write_text("something new\n", encoding="utf-8")
            result = AgentResult(ok=True, status="done")
            _finalize_worktree(wt, result)
            self.assertIsNotNone(result.changed_files)
            self.assertGreater(len(result.changed_files), 0)
            self.assertIsNotNone(result.worktree_path)
            self.assertEqual(result.worktree_path, wt.path)

    def test_finalize_worktree_no_changes_cleans_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._make_git_repo(Path(tmp))
            rt = self._make_runtime(repo, "run_test3")
            wt = _create_worktree(rt, "call_0002")
            self.assertIsNotNone(wt)
            # Do NOT write anything — no changes
            result = AgentResult(ok=True, status="done")
            _finalize_worktree(wt, result)
            self.assertEqual(result.changed_files, [])
            self.assertIsNone(result.worktree_path)

    def test_no_git_repo_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            non_git = Path(tmp) / "not_a_repo"
            non_git.mkdir()
            home = non_git / ".workflows"
            output_dir = home / "runs" / "run_nongit"
            output_dir.mkdir(parents=True, exist_ok=True)
            script = non_git / "wf.py"
            script.touch()
            rt = WorkflowRuntime(home=home)
            rt.run_id = "run_nongit"
            rt.script_path = script
            rt.current_output_dir = output_dir
            result = _create_worktree(rt, "call_0001")
            self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
