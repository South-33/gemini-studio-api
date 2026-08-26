import os
import subprocess
import sys
import time
import unittest


@unittest.skipUnless(os.name == "nt", "Windows Job Objects are Windows-only")
class LauncherTests(unittest.TestCase):
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
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(child_pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )


if __name__ == "__main__":
    unittest.main()
