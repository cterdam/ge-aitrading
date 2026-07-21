from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from execution.official_mcp_collector import (
    EXPLICITLY_DISALLOWED_TOOLS,
    MCP_SERVER_NAME,
    READ_ONLY_ROBINHOOD_TOOLS,
    OfficialCollectorError,
    _final_json_payload,
    collect_official_raw_snapshot,
    collect_official_shadow_snapshot,
    read_only_allowed_tools,
)


def _runner_stdout(final_message: str) -> str:
    return json.dumps({"type": "result", "is_error": False, "result": final_message})


def _fake_result(returncode: int, stdout: str = "", stderr: str = ""):
    return type("Result", (), {"returncode": returncode, "stdout": stdout, "stderr": stderr})()


class ReadOnlyMcpPolicyTests(unittest.TestCase):
    def test_unattended_policy_exposes_only_get_tools(self) -> None:
        self.assertTrue(READ_ONLY_ROBINHOOD_TOOLS)
        self.assertTrue(all(name.startswith("get_") for name in READ_ONLY_ROBINHOOD_TOOLS))
        forbidden = ("place_", "review_", "cancel_", "update_", "remove_", "add_")
        self.assertFalse(any(name.startswith(forbidden) for name in READ_ONLY_ROBINHOOD_TOOLS))

    def test_allowed_tools_are_scoped_to_the_robinhood_server(self) -> None:
        allowed = read_only_allowed_tools().split(",")
        self.assertEqual(len(READ_ONLY_ROBINHOOD_TOOLS), len(allowed))
        for entry in allowed:
            self.assertTrue(entry.startswith(f"mcp__{MCP_SERVER_NAME}__get_"), entry)

    def test_local_mutation_tools_are_explicitly_disallowed(self) -> None:
        for tool in ("Bash", "Write", "Edit", "WebFetch"):
            self.assertIn(tool, EXPLICITLY_DISALLOWED_TOOLS)


class FinalJsonPayloadTests(unittest.TestCase):
    def test_plain_json_final_message_is_parsed(self) -> None:
        payload = _final_json_payload(_runner_stdout('{"schema_version": 1}'))
        self.assertEqual({"schema_version": 1}, payload)

    def test_fenced_json_final_message_is_parsed(self) -> None:
        payload = _final_json_payload(_runner_stdout('```json\n{"schema_version": 1}\n```'))
        self.assertEqual({"schema_version": 1}, payload)

    def test_error_result_fails_closed(self) -> None:
        stdout = json.dumps({"type": "result", "is_error": True, "result": "{}"})
        with self.assertRaises(OfficialCollectorError):
            _final_json_payload(stdout)

    def test_prose_final_message_fails_closed(self) -> None:
        with self.assertRaises(OfficialCollectorError):
            _final_json_payload(_runner_stdout("I could not collect the data."))


class OfficialMcpCollectorTests(unittest.TestCase):
    def test_raw_collector_parses_json_strings_before_vault_storage(self) -> None:
        envelope = {
            "schema_version": 1,
            "source_updated_at": "2026-07-20T17:00:00Z",
            "request": json.dumps({"symbol": "SPY"}),
            "response": json.dumps({"quote": {"symbol": "SPY"}}),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "prompts").mkdir()
            (root / "prompts/robinhood_raw_collector.md").write_text(
                "collect {symbol}", encoding="utf-8"
            )

            def fake_run(command, **kwargs):
                return _fake_result(0, stdout=_runner_stdout(json.dumps(envelope)))

            with patch("execution.official_mcp_collector.claude_binary", return_value="claude"), \
                    patch("execution.official_mcp_collector.subprocess.run", side_effect=fake_run):
                receipt = collect_official_raw_snapshot(
                    "SPY", project_root=root, vault_root="logs/raw"
                )
            stored = json.loads(receipt.path.read_text(encoding="utf-8"))
            self.assertEqual({"symbol": "SPY"}, stored["request"])
            self.assertEqual("SPY", stored["response"]["quote"]["symbol"])

    def test_invalid_symbol_is_rejected_before_subprocess(self) -> None:
        with self.assertRaises(OfficialCollectorError):
            collect_official_shadow_snapshot("SPY; rm", "unused.json")

    def test_validated_output_is_written_atomically(self) -> None:
        example = Path("config/shadow_input.example.json").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out.json"

            def fake_run(command, **kwargs):
                return _fake_result(0, stdout=_runner_stdout(example))

            with patch("execution.official_mcp_collector.claude_binary", return_value="claude"), \
                    patch("execution.official_mcp_collector.subprocess.run", side_effect=fake_run) as run:
                result = collect_official_shadow_snapshot("sofi", output)
            self.assertEqual(output, result)
            self.assertEqual(1, json.loads(output.read_text())["schema_version"])
            command = run.call_args.args[0]
            self.assertIn("--allowedTools", command)
            allowed = command[command.index("--allowedTools") + 1]
            self.assertEqual(read_only_allowed_tools(), allowed)
            self.assertIn("--disallowedTools", command)
            self.assertNotIn("--dangerously-skip-permissions", command)

    def test_failed_collector_does_not_create_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out.json"
            with patch("execution.official_mcp_collector.claude_binary", return_value="claude"), \
                    patch(
                        "execution.official_mcp_collector.subprocess.run",
                        return_value=_fake_result(1),
                    ):
                with self.assertRaises(OfficialCollectorError):
                    collect_official_shadow_snapshot("SPY", output)
            self.assertFalse(output.exists())

    def test_invalid_snapshot_leaves_no_partial_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out.json"

            def fake_run(command, **kwargs):
                return _fake_result(0, stdout=_runner_stdout('{"schema_version": 999}'))

            with patch("execution.official_mcp_collector.claude_binary", return_value="claude"), \
                    patch("execution.official_mcp_collector.subprocess.run", side_effect=fake_run):
                with self.assertRaises(Exception):
                    collect_official_shadow_snapshot("SPY", output)
            self.assertFalse(output.exists())
            self.assertEqual([], list(Path(directory).glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
