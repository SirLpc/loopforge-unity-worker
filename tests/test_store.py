import unittest
from uuid import uuid4

from support import test_directory


class StoreTests(unittest.TestCase):
    def test_interrupted_job_and_idle_lease_survive_restart(self):
        from task_store import Store

        with test_directory() as root:
            db = Store(root / "worker.sqlite3", "worker-01", root / "project")
            task = {
                "task_id": str(uuid4()), "worker_id": "worker-01", "status": "implementing",
                "lease": True, "current_stage": "implement",
            }
            job = {
                "job_id": str(uuid4()), "task_id": task["task_id"], "request_id": str(uuid4()),
                "action": "implement", "status": "running", "signature": "test",
            }
            with db.connect(write=True) as conn:
                db.save_task(conn, task)
                db.save_job(conn, job)
            resumed = Store(root / "worker.sqlite3", "worker-01", root / "project")
            resumed.recover_interrupted()
            with resumed.connect() as conn:
                active = resumed.lease(conn)
                self.assertEqual(active["task_id"], task["task_id"])
                self.assertEqual(active["status"], "recovery_required")
                self.assertEqual(resumed.job(conn, job["job_id"])["status"], "recovery_required")

    def test_same_database_cannot_be_rebound_to_another_project(self):
        from task_store import Store

        with test_directory() as root:
            Store(root / "worker.sqlite3", "worker-01", root / "project")
            with self.assertRaisesRegex(RuntimeError, "bound"):
                Store(root / "worker.sqlite3", "worker-01", root / "other")
