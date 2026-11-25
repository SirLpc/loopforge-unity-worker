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

    def pooled_config(self, root):
        from dataclasses import replace
        from worker_config import PoolSettings, ServiceSettings, Settings, SlotSettings

        first = Settings(project=root / "one", logs=root, unity_exe="unity", unity_version="2022.3.62f3",
                         worker_id="service", token="sample")
        pool = PoolSettings("game", "a" * 64, (SlotSettings("worker-01", "game", True, first),
                                              SlotSettings("worker-02", "game", True, replace(first, project=root / "two"))))
        return ServiceSettings("game", "worker-01", {"game": pool})

    def test_migrates_single_slot_without_rewriting_ids_or_signatures(self):
        import os
        import sqlite3
        from contextlib import closing
        from task_store import Store, encode

        self.assertTrue(hasattr(Store, "open_service"), "Missing state migration")
        with test_directory() as root:
            cfg = self.pooled_config(root)
            path = root / "worker.sqlite3"
            tasks = [
                {"task_id": "running-task", "worker_id": "service", "status": "testing", "lease": True,
                 "current_stage": "test", "approved_revision": "c" * 40, "artifacts": [{"name": "original.log"}]},
                {"task_id": "pending-task", "worker_id": "service", "status": "pending", "lease": False},
                {"task_id": "old-smoke", "worker_id": "service", "status": "completed", "lease": False,
                 "task_type": "startup_smoke"},
            ]
            with closing(sqlite3.connect(path)) as db, db:
                db.executescript("""
                    CREATE TABLE identity (id INTEGER PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE tasks (id TEXT PRIMARY KEY, worker_id TEXT NOT NULL, lease INTEGER NOT NULL, data TEXT NOT NULL);
                    CREATE UNIQUE INDEX one_lease ON tasks(worker_id) WHERE lease = 1;
                    CREATE TABLE jobs (id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                                       request_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL, data TEXT NOT NULL);
                    CREATE UNIQUE INDEX one_active_job ON jobs(task_id) WHERE status = 'running';
                """)
                db.execute("INSERT INTO identity VALUES (1, ?)", (encode(["service", os.path.normcase(str(root / "one"))]),))
                db.executemany("INSERT INTO tasks VALUES (?, ?, ?, ?)", [
                    (task["task_id"], "service", task["lease"], encode(task)) for task in tasks
                ])
                job = {"job_id": "running-job", "task_id": "running-task", "status": "running",
                       "request_id": "old-request", "signature": "old-body-signature", "action": "run"}
                db.execute("INSERT INTO jobs VALUES (?, ?, ?, ?, ?)", ("running-job", "running-task", "old-request", "running", encode(job)))
            store = Store.open_service(cfg)
            store.recover_interrupted()
            with store.connect() as db:
                old = store.task(db, "running-task")
                self.assertEqual(old["slot_id"], "worker-01")
                self.assertEqual(old["approved_revision"], "c" * 40)
                self.assertEqual(old["artifacts"], [{"name": "original.log"}])
                self.assertEqual(old["status"], "recovery_required")
                self.assertIsNone(store.task(db, "pending-task")["slot_id"])
                self.assertEqual(store.task(db, "old-smoke")["slot_id"], "worker-01")
                self.assertEqual(store.request(db, "old-request")["signature"], "old-body-signature")
                self.assertTrue(store.has_task_permit(db, "running-task"))

    def test_rejects_rebinding_a_slot_or_moving_the_default(self):
        from dataclasses import replace
        from task_store import Store

        self.assertTrue(hasattr(Store, "open_service"))
        with test_directory() as root:
            cfg = self.pooled_config(root)
            Store.open_service(cfg)
            old, second = cfg.pools["game"].slots
            moved = replace(second, settings=replace(second.settings, project=root / "new"))
            with self.assertRaisesRegex(RuntimeError, "bound"):
                Store.open_service(replace(cfg, pools={"game": replace(cfg.pools["game"], slots=(old, moved))}))
            with self.assertRaisesRegex(RuntimeError, "bound"):
                Store.open_service(replace(cfg, legacy_slot="worker-02"))

    def test_two_slot_leases_and_one_active_job_per_task(self):
        import sqlite3
        from task_store import Store

        self.assertTrue(hasattr(Store, "open_service"))
        with test_directory() as root:
            store = Store.open_service(self.pooled_config(root))
            with store.connect(write=True) as db:
                for index, slot in ((1, "worker-01"), (2, "worker-02")):
                    store.save_task(db, {"task_id": str(index), "worker_id": "service", "pool_id": "game", "slot_id": slot, "lease": True})
                self.assertEqual(store.slot_lease(db, "worker-02")["task_id"], "2")
                with self.assertRaises(sqlite3.IntegrityError):
                    store.save_task(db, {"task_id": "3", "worker_id": "service", "pool_id": "game", "slot_id": "worker-02", "lease": True})
                with self.assertRaises(sqlite3.IntegrityError):
                    store.save_task(db, {"task_id": "4", "worker_id": "service", "pool_id": "game", "slot_id": None, "lease": True})
                original = {"job_id": "queued", "task_id": "1", "request_id": "q", "status": "queued"}
                store.save_job(db, original)
                self.assertEqual(store.active_job(db, "1"), original)
                with self.assertRaises(sqlite3.IntegrityError):
                    store.save_job(db, {**original, "job_id": "run", "request_id": "r", "status": "running"})

    def test_resolved_legacy_job_does_not_reacquire_editor_permit(self):
        from task_store import Store

        with test_directory() as root:
            config = self.pooled_config(root)
            store = Store.open_service(config)
            with store.connect(write=True) as db:
                store.save_task(db, {"task_id": "recovered", "pool_id": "game", "worker_id": "service",
                                     "slot_id": "worker-01", "lease": True, "status": "failed", "failure_kind": "recovered"})
                store.save_job(db, {"job_id": "old", "task_id": "recovered", "request_id": "old",
                                    "status": "recovery_required", "action": "compile"})
                store.save_job(db, {"job_id": "recover", "task_id": "recovered", "request_id": "recover",
                                    "status": "succeeded", "action": "recover"})
                db.execute("DROP TABLE unity_permits")
                names = {column[1] for column in db.execute("PRAGMA table_info(jobs)")}
                if "queue_pool" in names:
                    for name in ("queue_pool_fifo", "queue_deadlines"):
                        db.execute(f"DROP INDEX {name}")
                    db.execute("ALTER TABLE jobs DROP COLUMN queue_pool")
                    db.execute("ALTER TABLE jobs DROP COLUMN queue_deadline")
                db.execute("PRAGMA user_version=1")
            upgraded = Store.open_service(config)
            with upgraded.connect() as db:
                self.assertFalse(upgraded.has_task_permit(db, "recovered"))
