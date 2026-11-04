import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import launcher


class LauncherLogTests(unittest.TestCase):
    def test_autostart_uses_explorer_startup_shortcut_not_persistent_task_or_run_key(self):
        script = (Path(__file__).parents[1] / "ops" / "windows" / "install_autostart.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("[Environment]::GetFolderPath('Startup')", script)
        self.assertIn("CreateShortcut", script)
        self.assertIn("pythonw.exe", script)
        self.assertIn("Remove-ItemProperty -Path $runKey", script)
        self.assertNotIn("Set-ItemProperty -Path $runKey", script)
        self.assertIn("Gemini Studio API Bootstrap", script)
        self.assertIn("Unregister-ScheduledTask -TaskName $bootstrapTaskName", script)
        self.assertNotIn("New-ScheduledTaskTrigger", script)
        self.assertNotIn("StartWhenAvailable", script)

    def test_autostart_never_replaces_an_existing_launcher(self):
        script = (Path(__file__).parents[1] / "ops" / "windows" / "install_autostart.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("Invoke-RestMethod 'http://127.0.0.1:8000/health'", script)
        self.assertIn("Leaving the live launcher process tree untouched.", script)
        self.assertIn("if ($launcherProcesses.Count -gt 0)", script)
        self.assertNotIn("Stop-Process -Id $_.ProcessId", script)

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

    def test_endpoint_state_publishes_cloudflare_url_with_ngrok_control_plane(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "runtime_endpoint.json"
            with patch.object(launcher, "ENDPOINT_STATE", state_path):
                launcher.write_endpoint_state(
                    "https://fresh-route.trycloudflare.com/",
                    "cloudflare-quick",
                )

            payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["data_plane_url"], "https://fresh-route.trycloudflare.com")
            self.assertEqual(payload["control_plane_url"], launcher.CONTROL_ROOT)
            self.assertEqual(payload["transport"], "cloudflare-quick")

    def test_quick_tunnel_parser_ignores_urls_from_previous_launches(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "cloudflared.log"
            old = "old https://old-route.trycloudflare.com\n"
            log_path.write_text(old + "new https://new-route.trycloudflare.com\n", encoding="utf-8")
            process = SimpleNamespace(log_path=log_path, log_start_offset=len(old.encode("utf-8")))

            self.assertEqual(
                launcher.quick_tunnel_url(process),
                "https://new-route.trycloudflare.com",
            )

    def test_quick_tunnel_connection_marker_is_scoped_to_current_launch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "cloudflared.log"
            old = "Registered tunnel connection\n"
            log_path.write_text(old + "new launch pending\n", encoding="utf-8")
            process = SimpleNamespace(log_path=log_path, log_start_offset=len(old.encode("utf-8")))
            self.assertFalse(launcher.quick_tunnel_connected(process))

            with log_path.open("a", encoding="utf-8") as stream:
                stream.write("Registered tunnel connection connIndex=0\n")
            self.assertTrue(launcher.quick_tunnel_connected(process))

    def test_cloudflare_data_plane_can_rotate_during_active_gemini_work(self):
        self.assertTrue(
            launcher.should_restart_data_tunnel(
                process_exited=False,
                failures=launcher.PUBLIC_HEALTH_FAILURE_LIMIT,
            )
        )

    def test_cloudflare_live_process_does_not_depend_on_public_self_probe(self):
        cloudflare_exited = False
        failures = launcher.PUBLIC_HEALTH_FAILURE_LIMIT if cloudflare_exited else 0

        self.assertFalse(
            launcher.should_restart_data_tunnel(
                process_exited=cloudflare_exited,
                failures=failures,
            )
        )


@unittest.skipUnless(os.name == "nt", "Windows Job Objects are Windows-only")
class LauncherTests(unittest.TestCase):
    def test_data_tunnel_can_rotate_independently_of_browser_work(self):
        self.assertTrue(
            launcher.should_restart_data_tunnel(
                process_exited=False,
                failures=launcher.PUBLIC_HEALTH_FAILURE_LIMIT,
            )
        )
        self.assertTrue(launcher.CLOUDFLARE_STARTUP_GRACE_SECONDS >= 120)

    def test_api_monitor_defers_probe_failures_during_busy_grace(self):
        self.assertFalse(
            launcher.should_restart_api(
                process_exited=False,
                unhealthy_for_seconds=launcher.LOCAL_HEALTH_RESTART_AFTER_SECONDS,
                busy_grace_active=True,
            )
        )
        self.assertTrue(
            launcher.should_restart_api(
                process_exited=False,
                unhealthy_for_seconds=launcher.LOCAL_HEALTH_RESTART_AFTER_SECONDS,
                busy_grace_active=False,
            )
        )
        self.assertFalse(
            launcher.should_restart_api(
                process_exited=False,
                unhealthy_for_seconds=launcher.LOCAL_HEALTH_RESTART_AFTER_SECONDS - 1,
                busy_grace_active=False,
            )
        )
        self.assertTrue(
            launcher.should_restart_api(
                process_exited=True,
                unhealthy_for_seconds=0,
                busy_grace_active=True,
            )
        )
        self.assertGreaterEqual(launcher.LOCAL_HEALTH_RESTART_AFTER_SECONDS, 10 * 60)

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

    def test_launcher_mutex_is_host_wide(self):
        self.assertTrue(launcher.LAUNCHER_MUTEX_NAME.startswith("Global\\"))

    def test_launcher_mutex_allows_only_one_owner(self):
        mutex_name = rf"Global\GeminiStudioAPITest-{uuid.uuid4()}"
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

