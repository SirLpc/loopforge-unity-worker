import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from support import test_directory


class ProcessTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows process cleanup")
    def test_cleanup_error_cannot_hide_an_unverified_process_tree(self):
        from process_runner import ProcessRunner, StageError

        with test_directory() as root:
            with patch("process_runner.WindowsProcessTree") as factory:
                tree = factory.return_value
                tree.poll.return_value = 0
                tree.empty.side_effect = OSError("Cannot inspect job object")
                tree.close.side_effect = OSError("Cannot close job object")
                with self.assertRaises(StageError) as failure:
                    ProcessRunner().run([sys.executable], root, root / "run", timeout=5, data=b"prompt")
                self.assertEqual(failure.exception.kind, "recovery_required")

    def test_stdin_and_streams(self):
        from process_runner import ProcessRunner

        with test_directory() as root:
            result = ProcessRunner().run(
                [sys.executable, "-c", "import sys; print(sys.stdin.read()); print('err', file=sys.stderr)"],
                root, root / "run", timeout=8, data=b"hello",
            )
            self.assertEqual(result.exit_code, 0)
            self.assertIn(b"hello", result.stdout.read_bytes())
            self.assertIn(b"err", result.stderr.read_bytes())

    def test_timeout_kills_child_tree(self):
        from process_runner import ProcessRunner, StageError

        with test_directory() as root:
            child = "import time; from pathlib import Path; time.sleep(3); Path('escaped').touch()"
            script = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child!r}]); print('spawned', flush=True); time.sleep(30)"
            with self.assertRaises(StageError) as failure:
                ProcessRunner().run([sys.executable, "-c", script], root, root / "run", timeout=1.5)
            self.assertEqual(failure.exception.kind, "timeout")
            self.assertIn("spawned", (root / "run.stdout.log").read_text())
            with self.assertRaises(subprocess.TimeoutExpired):
                subprocess.run([sys.executable, "-c", "import time; time.sleep(10)"], timeout=2)
            self.assertFalse((root / "escaped").exists())

    def test_cancel_is_distinct_from_timeout(self):
        from process_runner import ProcessRunner, StageError

        with test_directory() as root:
            cancel = threading.Event()
            timer = threading.Timer(0.3, cancel.set)
            timer.start()
            self.addCleanup(timer.join)
            with self.assertRaises(StageError) as failure:
                ProcessRunner().run(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    root, root / "run", timeout=8, cancel=cancel,
                )
            self.assertEqual(failure.exception.kind, "cancelled")

    @unittest.skipUnless(os.name == "nt", "Windows kernel reservation")
    def test_same_project_has_one_service_reservation(self):
        from process_runner import reserve_project

        project = Path.cwd() / str(uuid4())
        with reserve_project(project):
            with self.assertRaisesRegex(RuntimeError, "already serving"):
                with reserve_project(project):
                    self.fail("second service reserved the project")
        with reserve_project(project):
            pass
