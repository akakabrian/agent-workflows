"""Tests for the MCP stdio server (agent_workflows.mcp_server).

All workflow runs use the fake provider so no network or model CLI is required.
The transport is exercised end-to-end through serve() with in-memory streams.
"""
from __future__ import annotations

import io
import json
import tempfile
import textwrap
import unittest
from pathlib import Path

from agent_workflows import mcp_server


def _write_script(root: Path) -> Path:
    script = root / "workflow.py"
    script.write_text(
        textwrap.dedent(
            """
            from workflows import agent, meta

            meta(name="mcp-test")

            async def main(args):
                result = await agent("Say hello.", label="greet")
                return {"text": result.text, "args": args}
            """
        ),
        encoding="utf-8",
    )
    return script


def _call(name: str, arguments: dict) -> dict:
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    return mcp_server.handle_message(message)


def _content_text(response: dict) -> str:
    return response["result"]["content"][0]["text"]


class HandshakeTests(unittest.TestCase):
    def test_initialize_echoes_protocol_and_advertises_tools(self) -> None:
        response = mcp_server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}
        )
        result = response["result"]
        self.assertEqual(result["protocolVersion"], "2025-03-26")
        self.assertIn("tools", result["capabilities"])
        self.assertEqual(result["serverInfo"]["name"], "open-agent-workflows")

    def test_initialize_defaults_protocol_when_absent(self) -> None:
        response = mcp_server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(response["result"]["protocolVersion"], mcp_server.DEFAULT_PROTOCOL_VERSION)

    def test_notification_gets_no_response(self) -> None:
        response = mcp_server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertIsNone(response)

    def test_unknown_method_is_method_not_found(self) -> None:
        response = mcp_server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "does/not/exist"})
        self.assertEqual(response["error"]["code"], mcp_server.METHOD_NOT_FOUND)

    def test_tools_list_includes_expected_tools(self) -> None:
        response = mcp_server.handle_message({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        names = {tool["name"] for tool in response["result"]["tools"]}
        self.assertEqual(
            names,
            {
                "owf_run_workflow",
                "owf_status",
                "owf_output",
                "owf_report",
                "owf_list_runs",
                "owf_read_artifact",
                "owf_new_workflow",
            },
        )
        for tool in response["result"]["tools"]:
            self.assertIn("inputSchema", tool)
            self.assertEqual(tool["inputSchema"]["type"], "object")


class ToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = self.root / ".workflows"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self) -> str:
        script = _write_script(self.root)
        response = _call(
            "owf_run_workflow",
            {"path": str(script), "home": str(self.home), "provider": "fake", "args": {"topic": "x"}},
        )
        self.assertFalse(response["result"]["isError"])
        payload = json.loads(_content_text(response))
        return payload["run_id"]

    def test_run_status_output_report_roundtrip(self) -> None:
        run_id = self._run()

        status = json.loads(_content_text(_call("owf_status", {"run_id": run_id, "home": str(self.home)})))
        self.assertEqual(status["run"]["id"], run_id)
        self.assertEqual(status["run"]["status"], "done")

        output = json.loads(_content_text(_call("owf_output", {"run_id": "latest", "home": str(self.home)})))
        self.assertEqual(output["run_id"], run_id)
        self.assertEqual(output["output"]["args"], {"topic": "x"})

        report = _content_text(_call("owf_report", {"run_id": run_id, "home": str(self.home), "format": "markdown"}))
        self.assertIn(f"Workflow run {run_id}", report)

        html = _content_text(_call("owf_report", {"run_id": run_id, "home": str(self.home), "format": "html"}))
        self.assertIn("<html", html.lower())

    def test_list_runs_and_limit(self) -> None:
        run_id = self._run()
        rows = json.loads(_content_text(_call("owf_list_runs", {"home": str(self.home), "limit": 5})))
        self.assertTrue(any(row["id"] == run_id for row in rows))

    def test_read_artifact_and_traversal_guard(self) -> None:
        run_id = self._run()
        # output.json exists at the top of the run directory.
        artifact = json.loads(
            _content_text(_call("owf_read_artifact", {"run_id": run_id, "path": "output.json", "home": str(self.home)}))
        )
        self.assertEqual(artifact["path"], "output.json")
        self.assertIn("text", artifact["content"])

        escape = _call("owf_read_artifact", {"run_id": run_id, "path": "../../etc/passwd", "home": str(self.home)})
        self.assertEqual(escape["error"]["code"], mcp_server.INVALID_PARAMS)

    def test_new_workflow_starter_and_example(self) -> None:
        target = self.root / "new" / "wf.py"
        result = json.loads(
            _content_text(_call("owf_new_workflow", {"output_path": str(target), "template_name": "hello"}))
        )
        self.assertTrue(Path(result["path"]).exists())
        self.assertIn("async def main", target.read_text(encoding="utf-8"))

        # Existing file without force is rejected.
        err = _call("owf_new_workflow", {"output_path": str(target), "template_name": "hello"})
        self.assertEqual(err["error"]["code"], mcp_server.INVALID_PARAMS)

        # Unknown template name is rejected with a helpful message.
        bad = _call("owf_new_workflow", {"output_path": str(self.root / "z.py"), "template_name": "nope"})
        self.assertEqual(bad["error"]["code"], mcp_server.INVALID_PARAMS)

    def test_status_no_runs_is_error(self) -> None:
        response = _call("owf_status", {"run_id": "latest", "home": str(self.home)})
        self.assertEqual(response["error"]["code"], mcp_server.INVALID_PARAMS)


class TransportTests(unittest.TestCase):
    def test_serve_processes_newline_delimited_messages(self) -> None:
        requests = "\n".join(
            [
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
                "",  # blank line should be ignored
                "{not json}",  # parse error -> error response with null id
            ]
        )
        stdin = io.StringIO(requests + "\n")
        stdout = io.StringIO()
        code = mcp_server.serve(stdin=stdin, stdout=stdout)
        self.assertEqual(code, 0)

        lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
        # initialize response, tools/list response, parse-error response. No
        # response for the notification or the blank line.
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0]["id"], 1)
        self.assertEqual(lines[1]["id"], 2)
        self.assertEqual(lines[2]["error"]["code"], mcp_server.PARSE_ERROR)


if __name__ == "__main__":
    unittest.main()
