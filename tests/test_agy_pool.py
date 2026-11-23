import time
import unittest

from agy_cli import AgyResult
from agy_pool import AgyPool


class FakeRunner:
    def __init__(self):
        self.models = ["gpt-oss-120b-medium", "claude-sonnet-4-6"]
        self.calls = []
        self.closed = False

    def check_capabilities(self):
        return None

    def refresh_models(self):
        return list(self.models)

    def run_prompt(self, prompt, *, model, use_search, timeout_seconds):
        self.calls.append((prompt, model, use_search, timeout_seconds))
        return AgyResult(
            response="ok",
            usage={},
            duration_seconds=0.01,
            model_id=model,
            effective_thinking_level="Medium" if model.endswith("-medium") else None,
            tools_used=(),
        )

    def close(self):
        self.closed = True


class AgyPoolTests(unittest.TestCase):
    def test_expired_queue_item_does_not_start_model(self):
        runner = FakeRunner()
        pool = AgyPool(runner=runner, max_concurrency=1, max_queue=1)
        pool._initialized = True
        pool._models = ["gpt-oss-120b-medium"]

        result = pool._run_queued(
            "hello",
            "gpt-oss-120b-medium",
            False,
            1,
            time.monotonic() - 2,
        )

        self.assertTrue(result["timed_out"])
        self.assertEqual(runner.calls, [])
        pool.close()

    def test_pool_runs_exact_model(self):
        runner = FakeRunner()
        pool = AgyPool(runner=runner, max_concurrency=1, max_queue=1)
        self.assertTrue(pool.init())
        result = pool.submit(
            "hello",
            model="gpt-oss-120b-medium",
            timeout_seconds=5,
        ).result(timeout=2)
        self.assertTrue(result["success"])
        self.assertEqual(result["agy_model"], "gpt-oss-120b-medium")
        self.assertEqual(runner.calls[0][1], "gpt-oss-120b-medium")
        pool.close()
        self.assertTrue(runner.closed)

    def test_diagnostics_are_bounded_and_small(self):
        pool = AgyPool(runner=FakeRunner(), max_concurrency=2, max_queue=3)
        self.assertTrue(pool.init())
        diag = pool.diagnostics()
        self.assertEqual(diag["max_concurrency"], 2)
        self.assertEqual(diag["max_queue"], 3)
        self.assertEqual(diag["active_requests"], 0)
        self.assertEqual(len(diag["models"]), 2)
        pool.close()


if __name__ == "__main__":
    unittest.main()
