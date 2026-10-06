"""Owned process supervision; never launches a model or external service."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from loop_engineering.processes import execute, process_alive, process_identity, same_process_identity


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="loop-process-")
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_success_and_separate_logs(self):
        result = execute([sys.executable, "-c", "import sys; print('actual stdout'); print('actual stderr', file=sys.stderr)"],
                         self.root, self.root / "logs", timeout=3)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.outcome, "completed")
        self.assertEqual(result.stdout.read_text().strip(), "actual stdout")
        self.assertEqual(result.stderr.read_text().strip(), "actual stderr")

    def test_stdin_survives_communicate_polling(self):
        text = "input" * 50000
        result = execute([sys.executable, "-c", "import sys,time; time.sleep(.15); print(len(sys.stdin.read()))"],
                         self.root, self.root / "logs", timeout=3, input_text=text)
        self.assertEqual(result.stdout.read_text().strip(), str(len(text)))

    def test_timeout_stops_owned_child(self):
        ids = []
        result = execute([sys.executable, "-c", "import time; time.sleep(10)"], self.root, self.root / "logs",
                         timeout=.1, on_started=ids.append)
        self.assertEqual(result.outcome, "timeout")
        self.assertFalse(process_alive(ids[0]))
        self.assertLess(result.elapsed_ms, 2000)

    def test_cancellation_stops_owned_child(self):
        ids = []
        result = execute([sys.executable, "-c", "import time; time.sleep(10)"], self.root, self.root / "logs",
                         timeout=3, cancelled=lambda: True, on_started=ids.append)
        self.assertEqual(result.outcome, "cancelled")
        self.assertFalse(process_alive(ids[0]))

    def test_callback_error_cleans_up_process(self):
        ids = []

        def fail(pid):
            ids.append(pid)
            raise ValueError("journal failure")

        with self.assertRaisesRegex(ValueError, "journal failure"):
            execute([sys.executable, "-c", "import time; time.sleep(10)"], self.root, self.root / "logs", timeout=3, on_started=fail)
        self.assertFalse(process_alive(ids[0]))

    def test_unavailable_executable_retains_diagnostics(self):
        result = execute(["no-such-loop-executable-123"], self.root, self.root / "logs", timeout=3)
        self.assertEqual(result.outcome, "unavailable")
        self.assertIsNone(result.exit_code)
        self.assertTrue(result.stderr.read_text())

    def test_denied_process_observer_is_unknown(self):
        with patch("loop_engineering.processes.subprocess.run", side_effect=PermissionError("Denied")):
            self.assertIsNone(process_identity(os.getpid()))

    def test_identity_observation_uses_fixed_locale_and_rejects_failed_ps(self):
        observed = subprocess.CompletedProcess([], 0, 'Tue Oct  6 15:32:36 2026 49021\n', '')
        with patch('loop_engineering.processes.subprocess.run', return_value=observed) as command:
            self.assertEqual(process_identity(49021), observed.stdout.strip())
            self.assertEqual(command.call_args.kwargs['env']['LC_ALL'], 'C')
        observed.returncode = 1
        with patch('loop_engineering.processes.subprocess.run', return_value=observed):
            self.assertIsNone(process_identity(49021))

    def test_existing_locale_layout_identifies_same_birth_time_and_group(self):
        legacy = 'Tue  6 Oct 15:32:36 2026     49021'
        current = 'Tue Oct  6 15:32:36 2026     49021'
        self.assertTrue(same_process_identity(current, legacy))
        self.assertTrue(same_process_identity(legacy, current))
        self.assertTrue(same_process_identity(current, current))

    def test_missing_changed_and_invalid_identities_cannot_match(self):
        current = 'Tue Oct  6 15:32:36 2026     49021'
        for other in [None, '', 'Tue 6 Oct 15:32:37 2026 49021',
                      'Tue 6 Oct 15:32:36 2026 49022', 'Tue 6 Nov 15:32:36 2026 49021',
                      'Tue 32 Oct 15:32:36 2026 49021', 'garbled']:
            with self.subTest(other=other):
                self.assertFalse(same_process_identity(current, other))
        self.assertFalse(same_process_identity(None, None))

    @unittest.skipUnless(os.name == "posix", "Process groups require POSIX")
    def test_owned_background_child_is_stopped_after_foreground_exit(self):
        target = self.root / "unexpected-effect"
        child = f"import time; from pathlib import Path; time.sleep(.7); Path({str(target)!r}).write_text('escaped')"
        code = f"import subprocess, sys; subprocess.Popen([sys.executable, '-c', {child!r}]); print('parent done')"
        result = execute([sys.executable, "-c", code], self.root, self.root / "logs", timeout=3)
        self.assertEqual(result.exit_code, 0)
        time.sleep(.8)
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
