from __future__ import annotations

import asyncio
import hashlib
import tempfile
import textwrap
import unittest
from pathlib import Path
from contextlib import redirect_stdout
import io

from agent_workflows.cli import main as cli_main
from agent_workflows.models import AgentResult
from agent_workflows.runtime import draft_manifest, parallel, resume_run, run_script, run_status, validate_script
from agent_workflows.storage import get_artifacts, list_runs
from agent_workflows.validation import SchemaValidationError, validate_schema


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

    def test_resume_records_resumed_from_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / ".workflows"
            script = self._write_script(
                root,
                """
                from workflows import agent

                async def main(args):
                    result = await agent("resume lineage check", label="resume")
                    return {"cache_status": result.cache_status, "cache_key": result.cache_key}
                """,
            )

            first = run_script(script, home=home, provider="fake", model="fake")
            second = resume_run(first["run_id"], home=home)

            first_run = run_status(home, first["run_id"])["run"]
            second_run = run_status(home, second["run_id"])["run"]
            second_call = run_status(home, second["run_id"])["calls"][0]

            self.assertIsNone(first_run["resumed_from_run_id"])
            self.assertEqual(second_run["resumed_from_run_id"], first["run_id"])
            self.assertNotEqual(second["run_id"], first["run_id"])
            self.assertEqual(second_call["cache_status"], "hit")

    def test_call_ids_are_unique_across_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / ".workflows"
            script = self._write_script(
                root,
                """
                from workflows import agent

                async def main(args):
                    result = await agent("hello", label="first")
                    return {"text": result.text}
                """,
            )
            first = run_script(script, home=home, provider="fake", model="fake")
            second = run_script(script, home=home, provider="fake", model="fake", cache_policy="refresh")
            first_call = run_status(home, first["run_id"])["calls"][0]
            second_call = run_status(home, second["run_id"])["calls"][0]
            self.assertEqual(first_call["call_index"], 1)
            self.assertEqual(second_call["call_index"], 1)
            self.assertNotEqual(first_call["id"], second_call["id"])
            self.assertTrue(first_call["id"].startswith(first["run_id"]))
            self.assertTrue(second_call["id"].startswith(second["run_id"]))

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

    def test_cache_semantics_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / ".workflows"
            script = self._write_script(
                root,
                """
                from workflows import agent

                async def main(args):
                    mode = args.get("mode", "base")
                    schema = None
                    if mode == "schema":
                        schema = {"type": "object", "properties": {"repo": {"type": "string"}}, "required": ["repo"]}
                    elif mode == "schema_changed":
                        schema = {"type": "object", "properties": {"findings": {"type": "array", "items": {"type": "string"}}}, "required": ["findings"]}
                    result = await agent(
                        "cache matrix prompt",
                        label="cache",
                        schema=schema,
                        provider=args.get("provider"),
                        model=args.get("model"),
                        cache_policy=args.get("cache_policy", "auto"),
                        cache_namespace=args.get("namespace"),
                        write_scope=["README.md"] if mode == "write" else None,
                    )
                    return {"cache": result.cache_status}
                """,
            )

            def run(args=None, *, cache_policy="auto"):
                payload = run_script(script, args=args or {}, home=home, provider="fake", model="fake", cache_policy=cache_policy)
                return run_status(home, payload["run_id"])["calls"][0]["cache_status"]

            self.assertEqual(run(), "miss")
            first = run_script(script, home=home, provider="fake", model="fake")
            resumed = resume_run(first["run_id"], home=home)
            self.assertEqual(run_status(home, resumed["run_id"])["calls"][0]["cache_status"], "hit")
            self.assertEqual(run({"cache_policy": "disabled"}), "disabled")
            self.assertEqual(run({"cache_policy": "refresh"}), "miss")
            self.assertEqual(run({"mode": "write"}), "bypassed")
            self.assertEqual(run({"mode": "schema"}), "miss")
            self.assertEqual(run({"mode": "schema_changed"}), "miss")
            self.assertEqual(run({"model": "different-model"}), "miss")
            self.assertEqual(run({"provider": "fixture"}), "miss")
            self.assertEqual(run({"namespace": "v2"}), "miss")

    def test_refresh_updates_normal_cache_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / ".workflows"
            script = self._write_script(
                root,
                """
                from workflows import agent

                async def main(args):
                    result = await agent(
                        "cache refresh prompt",
                        label="cache-refresh",
                        cache_policy=args.get("cache_policy", "auto"),
                        cache_namespace=args.get("namespace"),
                    )
                    return {"cache_status": result.cache_status, "cache_key": result.cache_key, "text": result.text}
                """,
            )

            first = run_script(script, args={"cache_policy": "auto"}, home=home, provider="fake", model="fake")
            first_call = run_status(home, first["run_id"])["calls"][0]
            self.assertEqual(first_call["cache_status"], "miss")

            second = run_script(script, args={"cache_policy": "auto"}, home=home, provider="fake", model="fake")
            second_call = run_status(home, second["run_id"])["calls"][0]
            self.assertEqual(second_call["cache_status"], "hit")
            self.assertEqual(second_call["call_key"], first_call["call_key"])

            refresh = run_script(script, args={"cache_policy": "refresh"}, home=home, provider="fake", model="fake")
            refresh_call = run_status(home, refresh["run_id"])["calls"][0]
            self.assertEqual(refresh_call["cache_status"], "miss")
            self.assertEqual(refresh_call["call_key"], first_call["call_key"])

            after = run_script(script, args={"cache_policy": "auto"}, home=home, provider="fake", model="fake")
            after_call = run_status(home, after["run_id"])["calls"][0]
            self.assertEqual(after_call["cache_status"], "hit")
            self.assertEqual(after_call["call_key"], first_call["call_key"])

    def test_worktree_failure_is_recorded_without_provider_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / ".workflows"
            script = self._write_script(
                root,
                """
                from workflows import agent, meta

                meta(name="worktree-fail")

                async def main(args):
                    result = await agent(
                        "This must not reach an unknown provider.",
                        provider="unknown-provider",
                        isolation="worktree",
                    )
                    return {"ok": result.ok, "status": result.status, "error": result.error}
                """,
            )
            result = run_script(script, home=home, provider="fake", model="fake")
            self.assertEqual(result["status"], "done")
            self.assertFalse(result["output"]["ok"])
            self.assertEqual(result["output"]["status"], "worktree_failed")
            call = run_status(home, result["run_id"])["calls"][0]
            self.assertEqual(call["status"], "worktree_failed")
            self.assertIn("worktree isolation failed", call["error"])

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

    def test_grandchild_workflow_is_rejected_and_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / ".workflows"
            (root / "grandchild.py").write_text(
                textwrap.dedent(
                    """
                    async def main(args):
                        return {"level": "grandchild"}
                    """
                ),
                encoding="utf-8",
            )
            (root / "child.py").write_text(
                textwrap.dedent(
                    """
                    from workflows import workflow

                    async def main(args):
                        return await workflow("grandchild.py")
                    """
                ),
                encoding="utf-8",
            )
            parent = root / "parent.py"
            parent.write_text(
                textwrap.dedent(
                    """
                    from workflows import workflow

                    async def main(args):
                        return await workflow("child.py")
                    """
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(Exception, "one child level"):
                run_script(parent, home=home, provider="fake", model="fake")
            runs = list_runs(home)
            self.assertTrue(any(row["status"] == "failed" and "one child level" in (row["error_message"] or "") for row in runs))

    def test_parallel_fail_fast_cancels_pending_work(self) -> None:
        started: list[str] = []

        async def slow_success() -> AgentResult:
            started.append("slow")
            await asyncio.sleep(0.2)
            return AgentResult(ok=True, status="done", text="slow")

        async def quick_failure() -> AgentResult:
            started.append("fail")
            await asyncio.sleep(0.01)
            return AgentResult(ok=False, status="failed", error="boom")

        async def should_not_start() -> AgentResult:
            started.append("third")
            await asyncio.sleep(0.2)
            return AgentResult(ok=True, status="done", text="third")

        results = asyncio.run(
            parallel([slow_success, quick_failure, should_not_start], concurrency=2, fail_fast=True)
        )
        self.assertEqual(results[1].status, "failed")
        self.assertEqual(results[2].status, "cancelled")
        self.assertNotIn("third", started)

    def test_artifacts_have_paths_hashes_and_cat_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / ".workflows"
            script = self._write_script(
                root,
                """
                from workflows import agent

                async def main(args):
                    result = await agent("artifact check", label="artifact")
                    return {"text": result.text}
                """,
            )
            result = run_script(script, home=home, provider="fake", model="fake")
            rows = get_artifacts(home, result["run_id"])
            self.assertGreaterEqual(len(rows), 2)
            for row in rows:
                path = Path(row["path"])
                self.assertTrue(path.exists())
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                self.assertEqual(row["sha256"], digest)
                self.assertEqual(row["size_bytes"], path.stat().st_size)

    def test_schema_validation_subset(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "score": {"type": "number"},
                "count": {"type": "integer"},
                "active": {"type": "boolean"},
                "missing": {"type": "null"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "nested": {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                },
            },
            "required": ["name", "tags", "nested"],
        }
        validate_schema(
            {
                "name": "demo",
                "score": 1.5,
                "count": 2,
                "active": True,
                "missing": None,
                "tags": ["a"],
                "nested": {"ok": False},
            },
            schema,
        )
        with self.assertRaises(SchemaValidationError):
            validate_schema({"name": "demo", "tags": [1], "nested": {"ok": True}}, schema)
        with self.assertRaises(SchemaValidationError):
            validate_schema({"tags": [], "nested": {"ok": True}}, schema)

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
