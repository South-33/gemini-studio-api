import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from agy_cli import AgyCliRunner, AgyModelSelectionError, parse_model_ids


class AgyCliTests(unittest.TestCase):
    def test_parse_model_ids_preserves_cli_contract(self):
        lines = [
            "Fetching available models...",
            "gemini-3.8-flash-high\tGemini 3.8 Flash (High)",
            "claude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)",
            "gpt-oss-120b-medium\tGPT-OSS 120B (Medium)",
            "claude-sonnet-4-6\tduplicate",
        ]
        self.assertEqual(
            parse_model_ids(lines),
            ["gemini-3.8-flash-high", "claude-sonnet-4-6", "gpt-oss-120b-medium"],
        )

    def test_exact_model_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = AgyCliRunner(agy_path="/bin/true", runtime_dir=tmp)
            runner._models = ["claude-sonnet-4-6"]
            self.assertEqual(runner.resolve_model("CLAUDE-SONNET-4-6"), "claude-sonnet-4-6")
            with self.assertRaises(AgyModelSelectionError):
                runner.resolve_model("neon")
            runner.close()

    def test_run_prompt_uses_exact_model_and_stream_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = AgyCliRunner(agy_path="/bin/true", runtime_dir=tmp)
            runner._models = ["gpt-oss-120b-medium"]
            payload = {
                "event": "result",
                "result": {
                    "status": "SUCCESS",
                    "response": "review complete",
                    "duration_seconds": 1.25,
                    "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
                },
            }
            runner._run = Mock(
                return_value=subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=json.dumps(payload) + "\n",
                    stderr="",
                )
            )

            result = runner.run_prompt("review this", model="gpt-oss-120b-medium")
            self.assertEqual(result.response, "review complete")
            self.assertEqual(result.model_id, "gpt-oss-120b-medium")
            self.assertEqual(result.effective_thinking_level, "Medium")
            args = runner._run.call_args.args[0]
            self.assertEqual(args[args.index("--model") + 1], "gpt-oss-120b-medium")
            stdin = runner._run.call_args.kwargs["input_text"]
            event = json.loads(stdin)
            self.assertEqual(event["message"]["content"], "review this")
            runner.close()


if __name__ == "__main__":
    unittest.main()
