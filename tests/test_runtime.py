from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path
from contextlib import redirect_stdout
import io

from agent_workflows.cli import main as cli_main
from agent_workflows.runtime import draft_manifest, resume_run, run_script, run_status, validate_script


class WorkflowRuntimeTests(unittest.TestCase):
    def _write_script(self, root: Path, body: str) -> Path:
        script = root / "workflow.py"
        script.write_text(textwrap.dedent(body), encoding="utf-8")
        return script

    def test_validate_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = self._write_script(
                root,
                """
                from workflows import meta

                meta(name="sample", description="sample workflow")

                async def main(args):
                    return {"ok": True}
                """,
            )
            payload = validate_script(script)
            self.assertEqual(payload["workflow"]["name"], "sample")

            manifest = draft_manifest(script, args={"hello": "world"}, provider="fake", model="fake")
            self.assertEqual(manifest["args"]["hello"], "world")
            self.assertEqual(manifest["workflow"]["name"], "sample")

    def test_run_resume_and_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / ".workflows"
            script = self._write_script(
                root,
                """
                from workflows import agent, meta, phase

                BUG_SCHEMA = {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string"},
                        "findings": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["repo", "findings"],
                }

                meta(name="bug-scan", description="scan repos")

                async def main(args):
                    phase("scan")
                    result = await agent("Inspect the repo.", schema=BUG_SCHEMA, label="scan")
                    return {"args": args, "result": result.value}
                """,
            )

            first = run_script(script, args={"repo": "api"}, home=home, provider="fake", model="fake")
            self.assertEqual(first["status"], "done")
            self.assertEqual(first["output"]["result"]["repo"], "")

            status = run_status(home, first["run_id"])
            self.assertEqual(status["run"]["status"], "done")
            self.assertEqual(len(status["calls"]), 1)
            self.assertEqual(status["calls"][0]["cache_status"], "miss")

            second = resume_run(first["run_id"], home=home)
            self.assertEqual(second["status"], "done")
            self.assertNotEqual(second["run_id"], first["run_id"])

            second_status = run_status(home, second["run_id"])
            self.assertEqual(second_status["calls"][0]["cache_status"], "hit")
            self.assertEqual(second_status["calls"][0]["status"], "done")

    def test_mutating_calls_bypass_cache_on_resume(self) -> None:
        # AC8: a write_scope / worktree call must never be reused from the
        # prompt-only cache on resume; the prior output does not prove the
        # filesystem side effects still hold.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / ".workflows"
            script = self._write_script(
                root,
                """
                from workflows import agent, meta

                meta(name="mutating")

                async def main(args):
                    result = await agent(
                        "Implement the fix in src/auth.py",
                        write_scope=["src/auth.py"],
                        isolation="worktree",
                    )
                    return {"cache": result.cache_status}
                """,
            )
            first = run_script(script, home=home, provider="fake", model="fake")
            self.assertEqual(run_status(home, first["run_id"])["calls"][0]["cache_status"], "bypassed")

            second = resume_run(first["run_id"], home=home)
            resumed_call = run_status(home, second["run_id"])["calls"][0]
            self.assertNotEqual(resumed_call["cache_status"], "hit")
            self.assertEqual(resumed_call["cache_status"], "bypassed")

    def test_child_workflow_composition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / ".workflows"
            self._write_script(
                root,
                """
                from workflows import agent, meta

                meta(name="child")

                async def main(args):
                    result = await agent("hello from child")
                    return {"text": result.text}
                """,
            ).rename(root / "child.py")
            parent = root / "parent.py"
            parent.write_text(
                textwrap.dedent(
                    """
                    from workflows import workflow, meta

                    meta(name="parent")

                    async def main(args):
                        child = await workflow("child.py", {"x": 1})
                        return {"child_status": child["status"], "child_output": child["output"]}
                    """
                ),
                encoding="utf-8",
            )
            result = run_script(parent, home=home, provider="fake", model="fake")
            self.assertEqual(result["status"], "done")
            self.assertEqual(result["output"]["child_status"], "done")
            self.assertEqual(result["output"]["child_output"]["text"], "hello from child")

    def test_cli_entrypoints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / ".workflows"
            script = self._write_script(
                root,
                """
                async def main(args):
                    return {"hello": args.get("name", "world")}
                """,
            )
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli_main(["init", "--home", str(home)]), 0)
                self.assertEqual(cli_main(["validate", str(script)]), 0)
                self.assertEqual(
                    cli_main(["run", str(script), "--home", str(home), "--json", "--args-json", '{"name":"Brian"}']),
                    0,
                )


if __name__ == "__main__":
    unittest.main()
