import threading
import unittest
from types import SimpleNamespace
from uuid import uuid4

from development import DevelopmentWorker
from pool_scheduler import RequestConflict
from support import test_directory
from worker_config import Settings


class RestartTests(unittest.TestCase):
    def test_restart_waits_for_active_job_and_refuses_new_work(self):
        with test_directory() as root:
            project = root / "project"
            project.mkdir()
            logs = root / "logs"
            logs.mkdir()
            finish = threading.Event()

            def run(*args, **kwargs):
                if not finish.wait(5):
                    raise RuntimeError("Test did not release the running job")
                return SimpleNamespace(exit_code=0)

            settings = Settings(project, logs, "unity", "2022.3.62f3", "test-token")
            worker = DevelopmentWorker(settings, runner=SimpleNamespace(run=run))
            job_id = str(uuid4())
            try:
                worker.smoke(job_id)
                thread, cancelled = worker.active[job_id]
                self.assertFalse(worker.shutdown_when_idle())
                self.assertFalse(cancelled.is_set())
                finish.set()
                thread.join(5)
                self.assertTrue(worker.shutdown_when_idle())
                self.assertEqual(worker.job(job_id)["status"], "succeeded")
                with self.assertRaises(RequestConflict):
                    worker.smoke(str(uuid4()))
            finally:
                finish.set()
                worker.close()
