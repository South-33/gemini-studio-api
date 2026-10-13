import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import launcher


class LauncherLogTests(unittest.TestCase):
    def test_rotate_log_keeps_one_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "server.log"
            log_path.write_text("old log", encoding="utf-8")

            with patch.object(launcher, "MAX_PROCESS_LOG_BYTES", 1):
                launcher.rotate_log(log_path)

            self.assertFalse(log_path.exists())
            self.assertEqual(
                (Path(temp_dir) / "server.log.1").read_text(encoding="utf-8"),
                "old log",
            )


@unittest.skipUnless(os.name == "nt", "Windows Job Objects are Windows-only")
class LauncherTests(unittest.TestCase):
    def test_tunnel_monitor_defers_probe_failures_during_active_request(self):
        self.assertFalse(
            launcher.should_restart_tunnel(
                process_exited=False,
                failures=launcher.PUBLIC_HEALTH_FAILURE_LIMIT,
                active_request=True,
            )
        )
        self.assertTrue(
            launcher.should_restart_tunnel(
                process_exited=False,
                failures=launcher.PUBLIC_HEALTH_FAILURE_LIMIT,
                active_request=False,
            )
        )
        self.assertTrue(
            launcher.should_restart_tunnel(
                process_exited=True,
                failures=0,
                active_request=True,
            )
        )

    def test_launcher_mutex_allows_only_one_owner(self):
        mutex_name = rf"Local\GeminiStudioAPITest-{uuid.uuid4()}"
        owner_code = (
            "import launcher, sys, time; "
            "handle = launcher.acquire_launcher_mutex(sys.argv[1]); "
            "print('owned' if handle else 'blocked', flush=True); "
            "time.sleep(30)"
        )
        owner = subprocess.Popen(
            [sys.executable, "-c", owner_code, mutex_name],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(owner.stdout.readline().strip(), "owned")
            contender = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import launcher, sys; print('owned' if launcher.acquire_launcher_mutex(sys.argv[1]) else 'blocked')",
                    mutex_name,
                ],
                cwd=os.path.dirname(os.path.dirname(__file__)),
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
            self.assertEqual(contender.stdout.strip(), "blocked")
        finally:
            owner.kill()
            owner.wait(timeout=5)
            if owner.stdout:
                owner.stdout.close()

    def test_job_owner_exit_kills_descendant_process(self):
        owner_code = (
            "import launcher, subprocess, sys, time; "
            "job = launcher.enable_kill_on_close_job(); "
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            "print(child.pid, flush=True); "
            "time.sleep(60)"
        )
        owner = subprocess.Popen(
            [sys.executable, "-c", owner_code],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            stdout=subprocess.PIPE,
            text=True,
        )
        child_pid = int(owner.stdout.readline().strip())
        try:
            owner.kill()
            owner.wait(timeout=5)
            time.sleep(0.5)
            tasklist = subprocess.run(
                ["tasklist", "/FI", f"PID eq {child_pid}"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout
            self.assertNotIn(str(child_pid), tasklist)
        finally:
            if owner.stdout:
                owner.stdout.close()
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(child_pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )


if __name__ == "__main__":
    unittest.main()
