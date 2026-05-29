"""End-to-end tests for the newer CLI subcommands: explain-cache, report,
artifacts, cat.  All runs use the fake provider so no network is required.
"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import textwrap
import unittest
from pathlib import Path

from agent_workflows.cli import main as cli_main
from agent_workflows.runtime import run_status


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _run_cli(argv: list[str]) -> tuple[int, str]:
    """Run cli_main, capture stdout, return (exit_code, captured_text)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli_main(argv)
    return code, buf.getvalue()


def _write_script(root: Path, body: str) -> Path:
    script = root / "workflow.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return script


def _setup_run(root: Path, home: Path) -> str:
    """Write a minimal workflow, run it, return the run_id."""
    script = _write_script(
        root,
        """
        from workflows import agent, meta

        meta(name="cli-extra-test")

        async def main(args):
            result = await agent("Say hello.", label="greet")
            return {"text": result.text}
        """,
    )
    code, out = _run_cli(["run", str(script), "--home", str(home), "--json"])
    assert code == 0, f"run failed:\n{out}"
    payload = json.loads(out)
    return payload["run_id"]


# ---------------------------------------------------------------------------
# CLI subcommand tests
# ---------------------------------------------------------------------------

class ExplainCacheTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._home = root / ".workflows"
        self._run_id = _setup_run(root, self._home)

    def tearDown(self):
        self._tmp.cleanup()

    def test_explain_cache_returns_zero(self):
        code, _ = _run_cli(["explain-cache", self._run_id, "--home", str(self._home)])
        self.assertEqual(code, 0)

    def test_explain_cache_mentions_miss(self):
        _, out = _run_cli(["explain-cache", self._run_id, "--home", str(self._home)])
        self.assertIn("miss", out)

    def test_explain_cache_json_mode(self):
        code, out = _run_cli(["explain-cache", self._run_id, "--home", str(self._home), "--json"])
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertIsInstance(rows, list)
        self.assertGreater(len(rows), 0)
        self.assertIn("cache_status", rows[0])

    def test_explain_cache_latest_alias(self):
        code, _ = _run_cli(["explain-cache", "latest", "--home", str(self._home)])
        self.assertEqual(code, 0)


class ReportTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._home = root / ".workflows"
        self._run_id = _setup_run(root, self._home)

    def tearDown(self):
        self._tmp.cleanup()

    def _output_dir(self) -> Path:
        status = run_status(self._home, self._run_id)
        return Path(status["run"]["output_dir"])

    def test_report_markdown_returns_zero(self):
        code, _ = _run_cli(["report", self._run_id, "--home", str(self._home)])
        self.assertEqual(code, 0)

    def test_report_markdown_file_exists(self):
        _run_cli(["report", self._run_id, "--home", str(self._home)])
        report_path = self._output_dir() / "report.md"
        self.assertTrue(report_path.exists(), f"report.md not found at {report_path}")

    def test_report_markdown_content(self):
        _run_cli(["report", self._run_id, "--home", str(self._home)])
        content = (self._output_dir() / "report.md").read_text(encoding="utf-8")
        self.assertIn(self._run_id, content)

    def test_report_html_returns_zero(self):
        code, _ = _run_cli(["report", self._run_id, "--home", str(self._home), "--html"])
        self.assertEqual(code, 0)

    def test_report_html_file_exists(self):
        _run_cli(["report", self._run_id, "--home", str(self._home), "--html"])
        report_path = self._output_dir() / "report.html"
        self.assertTrue(report_path.exists(), f"report.html not found at {report_path}")

    def test_report_html_starts_with_doctype(self):
        _run_cli(["report", self._run_id, "--home", str(self._home), "--html"])
        content = (self._output_dir() / "report.html").read_text(encoding="utf-8")
        self.assertTrue(
            content.startswith("<!doctype html"),
            f"HTML report does not start with <!doctype html>; got: {content[:60]!r}",
        )

    def test_report_stdout_flag(self):
        code, out = _run_cli([
            "report", self._run_id, "--home", str(self._home), "--stdout",
        ])
        self.assertEqual(code, 0)
        self.assertIn(self._run_id, out)

    def test_report_custom_out_path(self):
        out_path = Path(self._tmp.name) / "custom_report.md"
        code, _ = _run_cli([
            "report", self._run_id, "--home", str(self._home),
            "--out", str(out_path),
        ])
        self.assertEqual(code, 0)
        self.assertTrue(out_path.exists())


class ArtifactsTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._home = root / ".workflows"
        self._run_id = _setup_run(root, self._home)

    def tearDown(self):
        self._tmp.cleanup()

    def test_artifacts_returns_zero(self):
        code, _ = _run_cli(["artifacts", self._run_id, "--home", str(self._home)])
        self.assertEqual(code, 0)

    def test_artifacts_json_mode(self):
        code, out = _run_cli(["artifacts", self._run_id, "--home", str(self._home), "--json"])
        self.assertEqual(code, 0)
        rows = json.loads(out)
        self.assertIsInstance(rows, list)

    def test_artifacts_lists_prompt(self):
        _, out = _run_cli(["artifacts", self._run_id, "--home", str(self._home)])
        self.assertIn("prompt", out)


class CatTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._home = root / ".workflows"
        self._run_id = _setup_run(root, self._home)

    def tearDown(self):
        self._tmp.cleanup()

    def _first_call_id(self) -> str:
        status = run_status(self._home, self._run_id)
        return status["calls"][0]["id"]

    def test_cat_prompt_returns_zero(self):
        call_id = self._first_call_id()
        code, _ = _run_cli(["cat", call_id, "--home", str(self._home), "--prompt"])
        self.assertEqual(code, 0)

    def test_cat_prompt_prints_prompt_text(self):
        call_id = self._first_call_id()
        _, out = _run_cli(["cat", call_id, "--home", str(self._home), "--prompt"])
        self.assertIn("Say hello", out)

    def test_cat_output_returns_zero(self):
        call_id = self._first_call_id()
        code, _ = _run_cli(["cat", call_id, "--home", str(self._home)])
        self.assertEqual(code, 0)

    def test_cat_unknown_call_returns_nonzero(self):
        code, _ = _run_cli(["cat", "call_9999", "--home", str(self._home)])
        self.assertNotEqual(code, 0)


# ---------------------------------------------------------------------------
# Integrated "full flow" test: run -> explain-cache -> report -> artifacts -> cat
# ---------------------------------------------------------------------------

class FullFlowTest(unittest.TestCase):
    """Run a single workflow once and exercise all new subcommands against it."""

    def test_full_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / ".workflows"
            script = _write_script(
                root,
                """
                from workflows import agent, meta, phase

                meta(name="full-flow")

                async def main(args):
                    phase("work")
                    result = await agent("What is 2+2?", label="math")
                    return {"answer": result.text}
                """,
            )

            # Run
            code, out = _run_cli(["run", str(script), "--home", str(home), "--json"])
            self.assertEqual(code, 0)
            run_id = json.loads(out)["run_id"]

            # explain-cache
            code, out = _run_cli(["explain-cache", run_id, "--home", str(home)])
            self.assertEqual(code, 0)
            self.assertIn("miss", out)

            # report (markdown)
            code, _ = _run_cli(["report", run_id, "--home", str(home)])
            self.assertEqual(code, 0)
            status = run_status(home, run_id)
            report_md = Path(status["run"]["output_dir"]) / "report.md"
            self.assertTrue(report_md.exists())

            # report --html
            code, _ = _run_cli(["report", run_id, "--home", str(home), "--html"])
            self.assertEqual(code, 0)
            report_html = Path(status["run"]["output_dir"]) / "report.html"
            self.assertTrue(report_html.exists())
            html_content = report_html.read_text(encoding="utf-8")
            self.assertTrue(html_content.startswith("<!doctype html"))

            # artifacts
            code, _ = _run_cli(["artifacts", run_id, "--home", str(home)])
            self.assertEqual(code, 0)

            # cat --prompt
            call_id = status["calls"][0]["id"]
            code, cat_out = _run_cli(["cat", call_id, "--home", str(home), "--prompt"])
            self.assertEqual(code, 0)
            self.assertIn("2+2", cat_out)


if __name__ == "__main__":
    unittest.main()
