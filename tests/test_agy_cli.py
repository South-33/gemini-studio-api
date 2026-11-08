import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agy_cli import (
    AgyCliError,
    AgyCliRunner,
    AgyCliTimeoutError,
    AgyModelSelectionError,
    parse_available_model_slugs,
    parse_model_slugs,
)


class AgyCliTests(unittest.TestCase):
    def test_capability_check_rejects_old_cli(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = AgyCliRunner(agy_path="agy.exe", runtime_dir=temp_dir)
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="--model --input-format --output-format",
                stderr="",
            )
            with patch("agy_cli.subprocess.run", return_value=completed):
                with self.assertRaisesRegex(AgyCliError, "missing required flags"):
                    runner.check_capabilities()

    def test_environment_isolation_rejects_active_global_hook(self):
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as home_dir:
            hooks_path = Path(home_dir) / ".gemini" / "config" / "hooks.json"
            hooks_path.parent.mkdir(parents=True)
            hooks_path.write_text(json.dumps({
                "inject": {"PreInvocation": [{"type": "command", "command": "echo unsafe"}]}
            }), encoding="utf-8")
            runner = AgyCliRunner(agy_path="agy.exe", runtime_dir=temp_dir)
            with patch("agy_cli.Path.home", return_value=Path(home_dir)):
                with self.assertRaisesRegex(AgyCliError, "Active global Antigravity hooks"):
                    runner.check_environment_isolation()

    def test_environment_isolation_allows_disabled_customizations(self):
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as home_dir:
            hooks_path = Path(home_dir) / ".gemini" / "config" / "hooks.json"
            hooks_path.parent.mkdir(parents=True)
            hooks_path.write_text(json.dumps({
                "disabled": {
                    "enabled": False,
                    "PreInvocation": [{"type": "command", "command": "echo disabled"}],
                }
            }), encoding="utf-8")
            runner = AgyCliRunner(agy_path="agy.exe", runtime_dir=temp_dir)
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps([{"name": "disabled-plugin", "enabled": False}]),
                stderr="",
            )
            with patch("agy_cli.Path.home", return_value=Path(home_dir)), patch(
                "agy_cli.subprocess.run", return_value=completed
            ):
                runner.check_environment_isolation()

    def test_environment_isolation_rejects_enabled_plugin(self):
        with tempfile.TemporaryDirectory() as temp_dir, tempfile.TemporaryDirectory() as home_dir:
            runner = AgyCliRunner(agy_path="agy.exe", runtime_dir=temp_dir)
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps([{"name": "firebase", "enabled": True}]),
                stderr="",
            )
            with patch("agy_cli.Path.home", return_value=Path(home_dir)), patch(
                "agy_cli.subprocess.run", return_value=completed
            ):
                with self.assertRaisesRegex(AgyCliError, "Enabled Antigravity plugins"):
                    runner.check_environment_isolation()

    def test_model_discovery_uses_newest_version_for_stable_family(self):
        models = parse_model_slugs(
            [
                "gemini-3.7-flash-medium\tGemini 3.7 Flash (Medium)",
                "gemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)",
                "gemini-3.8-flash-high\tGemini 3.8 Flash (High)",
                "gemini-4-argon-high\tGemini 4 Argon (High)",
                "claude-sonnet-4-6\tClaude Sonnet 4.6",
            ]
        )

        self.assertEqual(models[("flash", "medium")], "gemini-3.8-flash-medium")
        self.assertEqual(models[("flash", "high")], "gemini-3.8-flash-high")
        self.assertEqual(models[("argon", "high")], "gemini-4-argon-high")

    def test_model_discovery_keeps_non_gemini_cli_models(self):
        models = parse_available_model_slugs([
            "Fetching available models...",
            "gemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)",
            "claude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)",
            "gpt-oss-120b-medium\tGPT-OSS 120B (Medium)",
        ])

        self.assertEqual(models, [
            "gemini-3.8-flash-medium",
            "claude-sonnet-4-6",
            "gpt-oss-120b-medium",
        ])

    def test_exact_cli_model_id_bypasses_family_alias_mapping(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = AgyCliRunner(agy_path="agy.exe", runtime_dir=temp_dir)
            runner._models = {("flash", "medium"): "gemini-3.8-flash-medium"}
            runner._available_model_slugs = ["gemini-3.8-flash-medium", "claude-sonnet-4-6"]

            self.assertEqual(runner.resolve_model("claude-sonnet-4-6", "high"), "claude-sonnet-4-6")

    def test_prompt_uses_stream_json_with_session_policy_and_minimal_agent(self):
        result_payload = {
            "event": "result",
            "result": {
                "conversation_id": "conv-1",
                "status": "SUCCESS",
                "response": "ok\n",
                "duration_seconds": 1.2,
                "usage": {"input_tokens": 10, "output_tokens": 2},
            },
        }
        observed = {}

        def fake_run(args, **kwargs):
            cwd = Path(kwargs["cwd"])
            observed["cwd"] = cwd
            agent_name = args[args.index("--agent") + 1]
            observed["agent"] = (cwd / ".agents" / "agents" / f"{agent_name}.md").read_text(encoding="utf-8")
            observed["input"] = kwargs["input"]
            observed["settings"] = json.loads(args[args.index("--settings") + 1])
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=json.dumps(result_payload), stderr="")

        with tempfile.TemporaryDirectory() as temp_dir:
            runner = AgyCliRunner(agy_path="agy.exe", runtime_dir=temp_dir)
            runner._models = {("flash", "medium"): "gemini-3.8-flash-medium"}
            with patch("agy_cli.subprocess.run", side_effect=fake_run) as run:
                result = runner.run_prompt("hello", family="flash", thinking_level="medium")
            self.assertEqual(observed["cwd"], runner.runtime_dir)

        self.assertEqual(result.response, "ok\n")
        self.assertEqual(result.prompt_transport, "stdin")
        args = run.call_args.args[0]
        self.assertIn("--disable-slash-commands", args)
        self.assertIn("--sandbox", args)
        self.assertIn("--input-format", args)
        self.assertEqual(args[args.index("--input-format") + 1], "stream-json")
        self.assertEqual(args[args.index("--output-format") + 1], "stream-json")
        self.assertEqual(args[args.index("--agent") + 1], "api-model")
        self.assertNotIn("--dangerously-skip-permissions", args)
        self.assertNotIn("-p", args)
        sent = json.loads(observed["input"].strip())
        self.assertEqual(sent["event"], "user")
        self.assertEqual(sent["message"]["content"], "hello")
        self.assertIn("tools: []", observed["agent"])
        self.assertEqual(observed["settings"]["toolPermission"], "request-review")
        self.assertEqual(observed["settings"]["modelProvider"], "")
        self.assertFalse(observed["settings"]["useG1Credits"])
        self.assertIn("read_url(*)", observed["settings"]["permissions"]["deny"])
        self.assertIn("write_file(*)", observed["settings"]["permissions"]["deny"])
        self.assertIn("command(*)", observed["settings"]["permissions"]["deny"])
        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(args[args.index("--log-file") + 1], os.devnull)

    def test_search_request_uses_search_only_agent_and_url_permission(self):
        tool_event = {
            "event": "step_update",
            "step_update": {
                "step_type": "tool",
                "tool_name": "search_web",
                "state": "DONE",
            },
        }
        result_payload = {
            "event": "result",
            "result": {
                "conversation_id": "conv-search",
                "status": "SUCCESS",
                "response": "ok\n",
                "duration_seconds": 1.0,
                "usage": {},
            },
        }
        observed = {}

        def fake_run(args, **kwargs):
            cwd = Path(kwargs["cwd"])
            agent_name = args[args.index("--agent") + 1]
            observed["agent_name"] = agent_name
            observed["agent"] = (cwd / ".agents" / "agents" / f"{agent_name}.md").read_text(encoding="utf-8")
            observed["settings"] = json.loads(args[args.index("--settings") + 1])
            stdout = json.dumps(tool_event) + "\n" + json.dumps(result_payload) + "\n"
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")

        with tempfile.TemporaryDirectory() as temp_dir:
            runner = AgyCliRunner(agy_path="agy.exe", runtime_dir=temp_dir)
            runner._models = {("flash", "high"): "gemini-3.8-flash-high"}
            with patch("agy_cli.subprocess.run", side_effect=fake_run):
                runner.run_prompt("research this", family="flash", thinking_level="high", use_search=True)

        self.assertEqual(observed["agent_name"], "api-search")
        self.assertIn("tools:\n  - search_web\n  - read_url_content\n", observed["agent"])
        self.assertEqual(observed["settings"]["permissions"]["allow"], ["read_url(*)"])
        self.assertNotIn("read_url(*)", observed["settings"]["permissions"]["deny"])
        self.assertIn("execute_url(*)", observed["settings"]["permissions"]["deny"])

    def test_search_request_fails_if_search_tool_was_not_used(self):
        result_payload = {
            "event": "result",
            "result": {
                "conversation_id": "conv-search-missing",
                "status": "SUCCESS",
                "response": "unverified\n",
                "duration_seconds": 1.0,
                "usage": {},
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = AgyCliRunner(agy_path="agy.exe", runtime_dir=temp_dir)
            runner._models = {("flash", "medium"): "gemini-3.8-flash-medium"}
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(result_payload) + "\n",
                stderr="",
            )
            with patch("agy_cli.subprocess.run", return_value=completed):
                with self.assertRaisesRegex(AgyCliError, "did not use search_web"):
                    runner.run_prompt("research this", family="flash", thinking_level="medium", use_search=True)

    def test_failed_search_tool_does_not_satisfy_search_requirement(self):
        tool_event = {
            "event": "step_update",
            "step_update": {
                "step_type": "tool",
                "tool_name": "search_web",
                "state": "ERROR",
            },
        }
        result_payload = {
            "event": "result",
            "result": {
                "conversation_id": "conv-search-error",
                "status": "SUCCESS",
                "response": "unverified\n",
                "duration_seconds": 1.0,
                "usage": {},
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = AgyCliRunner(agy_path="agy.exe", runtime_dir=temp_dir)
            runner._models = {("flash", "medium"): "gemini-3.8-flash-medium"}
            stdout = json.dumps(tool_event) + "\n" + json.dumps(result_payload) + "\n"
            completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")
            with patch("agy_cli.subprocess.run", return_value=completed):
                with self.assertRaisesRegex(AgyCliError, "did not use search_web"):
                    runner.run_prompt("research this", family="flash", thinking_level="medium", use_search=True)

    def test_non_search_request_fails_on_unexpected_tool_use(self):
        tool_event = {
            "event": "step_update",
            "step_update": {
                "step_type": "tool",
                "tool_name": "schedule",
                "state": "ERROR",
            },
        }
        result_payload = {
            "event": "result",
            "result": {
                "conversation_id": "conv-tool",
                "status": "SUCCESS",
                "response": "done\n",
                "duration_seconds": 1.0,
                "usage": {},
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = AgyCliRunner(agy_path="agy.exe", runtime_dir=temp_dir)
            runner._models = {("flash", "medium"): "gemini-3.8-flash-medium"}
            stdout = json.dumps(tool_event) + "\n" + json.dumps(result_payload) + "\n"
            completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")
            with patch("agy_cli.subprocess.run", return_value=completed):
                with self.assertRaisesRegex(AgyCliError, "outside the API policy"):
                    runner.run_prompt("hello", family="flash", thinking_level="medium")

    def test_runtime_workspace_cleans_legacy_request_dirs_and_keeps_static_agents(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            legacy = Path(temp_dir) / "agy-request-old"
            legacy.mkdir()
            (legacy / "agy.log").write_text("old", encoding="utf-8")
            stale_log = Path(temp_dir) / "agy-run-stale.log"
            stale_log.write_text("old", encoding="utf-8")

            runner = AgyCliRunner(agy_path="agy.exe", runtime_dir=temp_dir)

            self.assertFalse(legacy.exists())
            self.assertFalse(stale_log.exists())
            agent_dir = runner.runtime_dir / ".agents" / "agents"
            self.assertTrue((agent_dir / "api-model.md").exists())
            self.assertTrue((agent_dir / "api-search.md").exists())

    def test_large_prompt_stays_on_stdin_and_is_not_written_to_disk(self):
        result_payload = {
            "event": "result",
            "result": {
                "conversation_id": "conv-large",
                "status": "SUCCESS",
                "response": "large-ok\n",
                "duration_seconds": 2.5,
                "usage": {"input_tokens": 20, "output_tokens": 2},
            },
        }
        prompt = "large prompt body\n" * 5000
        observed = {}

        def fake_run(args, **kwargs):
            cwd = Path(kwargs["cwd"])
            observed["cwd"] = cwd
            observed["input"] = kwargs["input"]
            observed["files"] = [p.relative_to(cwd).as_posix() for p in cwd.rglob("*") if p.is_file()]
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=json.dumps(result_payload), stderr="")

        with tempfile.TemporaryDirectory() as temp_dir:
            runner = AgyCliRunner(agy_path="agy.exe", runtime_dir=temp_dir)
            runner._models = {("flash", "medium"): "gemini-3.8-flash-medium"}
            with patch("agy_cli.subprocess.run", side_effect=fake_run):
                result = runner.run_prompt(prompt, family="flash", thinking_level="medium")
            self.assertEqual(observed["cwd"], runner.runtime_dir)

        self.assertEqual(result.prompt_transport, "stdin")
        sent = json.loads(observed["input"].strip())
        self.assertEqual(sent["message"]["content"], prompt)
        self.assertNotIn("prompt.txt", observed["files"])

    def test_run_prompt_surfaces_result_error(self):
        result_payload = {
            "event": "result",
            "result": {
                "conversation_id": "conv-2",
                "status": "ERROR",
                "response": "",
                "error": "quota exhausted",
                "usage": {},
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = AgyCliRunner(agy_path="agy.exe", runtime_dir=Path(temp_dir))
            runner._models = {("flash", "high"): "gemini-3.8-flash-high"}
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout=json.dumps(result_payload) + "\n",
                stderr="",
            )
            with patch("agy_cli.subprocess.run", return_value=completed):
                with self.assertRaisesRegex(AgyCliError, "quota exhausted"):
                    runner.run_prompt("hello", family="flash", thinking_level="high")

    def test_zero_token_invalid_model_is_typed_for_safe_refresh(self):
        result_payload = {
            "event": "result",
            "result": {
                "conversation_id": "",
                "status": "ERROR",
                "response": "",
                "error": "invalid model selection: stale model",
                "usage": {"total_tokens": 0},
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = AgyCliRunner(agy_path="agy.exe", runtime_dir=temp_dir)
            runner._models = {("flash", "medium"): "gemini-3.8-flash-medium"}
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout=json.dumps(result_payload) + "\n",
                stderr="",
            )
            with patch("agy_cli.subprocess.run", return_value=completed):
                with self.assertRaises(AgyModelSelectionError):
                    runner.run_prompt("hello", family="flash", thinking_level="medium")

    def test_missing_result_event_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = AgyCliRunner(agy_path="agy.exe", runtime_dir=temp_dir)
            runner._models = {("flash", "medium"): "gemini-3.8-flash-medium"}
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout='not-json\n{"event":"step_update","step_update":{"state":"DONE"}}\n',
                stderr="",
            )
            with patch("agy_cli.subprocess.run", return_value=completed):
                with self.assertRaisesRegex(AgyCliError, "returned no result"):
                    runner.run_prompt("hello", family="flash", thinking_level="medium")

    def test_print_timeout_discards_partial_success_response(self):
        result_payload = {
            "event": "result",
            "result": {
                "conversation_id": "conv-timeout",
                "status": "SUCCESS",
                "response": "partial text that must not escape",
                "finished_reason": "print_timeout",
                "usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = AgyCliRunner(agy_path="agy.exe", runtime_dir=temp_dir)
            runner._models = {("flash", "high"): "gemini-3.8-flash-high"}
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(result_payload) + "\n",
                stderr="",
            )
            with patch("agy_cli.subprocess.run", return_value=completed):
                with self.assertRaisesRegex(AgyCliTimeoutError, "partial output was discarded"):
                    runner.run_prompt("hello", family="flash", thinking_level="high", timeout_seconds=1)

    def test_model_discovery_timeout_is_wrapped_as_cli_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = AgyCliRunner(agy_path="agy.exe", runtime_dir=temp_dir)
            with patch(
                "agy_cli.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd=["agy", "models"], timeout=30),
            ):
                with self.assertRaisesRegex(AgyCliError, "agy models timed out"):
                    runner.refresh_models()

    def test_request_process_start_error_is_wrapped(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = AgyCliRunner(agy_path="agy.exe", runtime_dir=temp_dir)
            runner._models = {("flash", "medium"): "gemini-3.8-flash-medium"}
            with patch("agy_cli.subprocess.run", side_effect=OSError("binary vanished")):
                with self.assertRaisesRegex(AgyCliError, "Could not start Antigravity CLI request"):
                    runner.run_prompt("hello", family="flash", thinking_level="medium")


if __name__ == "__main__":
    unittest.main()
