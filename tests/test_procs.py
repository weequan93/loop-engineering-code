import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from loop_engineering import procs


@unittest.skipUnless(os.name == "posix", "process groups are POSIX")
class ProcessGroupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_timeout_kills_background_children(self):
        marker = self.dir / "pid"
        script = f"sleep 60 & echo $! > {marker}; sleep 60"
        result = procs.run(script, shell=True, cwd=self.dir, logs=self.dir / "logs", timeout=1)
        self.assertEqual(result.outcome, "timeout")
        child = int(marker.read_text())
        time.sleep(0.2)
        self.assertFalse(procs.alive(child))

    def test_completed_command_reaps_leftover_group(self):
        marker = self.dir / "pid"
        result = procs.run(f"(sleep 60 & echo $! > {marker}); exit 0", shell=True, cwd=self.dir,
                           logs=self.dir / "logs2", timeout=10)
        self.assertEqual((result.outcome, result.exit_code), ("completed", 0))
        time.sleep(0.2)
        self.assertFalse(procs.alive(int(marker.read_text())))

    def test_zombie_only_group_is_not_a_permission_error(self):
        # Darwin returns EPERM from killpg when only zombies remain (the v1 "permission" blocker).
        child = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        time.sleep(0.5)  # exited, not reaped: a zombie-only group
        outcome = procs.signal_group(child.pid, signal.SIGTERM)
        self.assertIn(outcome, {"zombie", "sent", "gone"})
        self.assertTrue(procs.group_quiescent(child.pid))
        self.assertEqual(procs.terminate_group(child.pid), outcome if outcome != "sent" else "terminated")
        child.wait()

    def test_group_members_and_identity_without_ps(self):
        child = subprocess.Popen(["sleep", "5"], start_new_session=True)
        try:
            members = procs.group_members(child.pid)
            self.assertIsNotNone(members)
            self.assertIn(child.pid, [m.pid for m in members])
            ident = procs.identity(child.pid)
            self.assertTrue(procs.alive(child.pid, ident))
            self.assertFalse(procs.alive(child.pid, ident + "x"))
        finally:
            child.kill()
            child.wait()
        self.assertFalse(procs.alive(child.pid))

    def test_cancel_and_output_limit(self):
        result = procs.run("sleep 30", shell=True, cwd=self.dir, logs=self.dir / "c", timeout=30,
                           cancelled=lambda: True)
        self.assertEqual(result.outcome, "cancelled")
        result = procs.run("yes", shell=True, cwd=self.dir, logs=self.dir / "o", timeout=30, max_output_bytes=10000)
        self.assertEqual(result.outcome, "output_limit")

    def test_detached_spawn_is_not_our_child(self):
        # A parent that re-execs loses its reaper threads; a detached child must never become its zombie.
        out = self.dir / "ppid"
        pid = procs.spawn_detached([sys.executable, "-c", f"import os, time; time.sleep(0.3); "
                                    f"open({str(out)!r}, 'w').write(str(os.getppid()))"],
                                   cwd=self.dir, log=self.dir / "spawn.log")
        deadline = time.time() + 10
        while not out.exists() or not out.read_text():
            self.assertLess(time.time(), deadline)
            time.sleep(0.05)
        self.assertNotEqual(int(out.read_text()), os.getpid())
        with self.assertRaises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)

    def test_stdin_and_unavailable(self):
        result = procs.run(["cat"], cwd=self.dir, logs=self.dir / "s", timeout=5, stdin_text="hello")
        self.assertEqual(result.stdout.read_text(), "hello")
        result = procs.run(["/nonexistent/binary"], cwd=self.dir, logs=self.dir / "u", timeout=5)
        self.assertEqual(result.outcome, "unavailable")


class LoginPathTests(unittest.TestCase):
    def test_login_shell_path_is_used_for_background_processes(self):
        from unittest import mock
        procs._LOGIN_PATH = None
        fake = mock.Mock(stdout="motd noise\n__LOOP_PATH__/home/u/.local/bin:/usr/bin__END__")
        with mock.patch.dict(os.environ, {"SHELL": "/bin/sh", "LOOP_LOGIN_PATH": "1"}), \
                mock.patch.object(procs.subprocess, "run", return_value=fake):
            self.assertEqual(procs.user_env({"X": "1"}), {"PATH": "/home/u/.local/bin:/usr/bin", "X": "1"})
        procs._LOGIN_PATH = None


if __name__ == "__main__":
    unittest.main()
