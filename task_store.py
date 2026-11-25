"""SQLite is authoritative for the current lease and every accepted operation."""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def encode(data):
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def signature(data):
    return sha256(encode(data).encode()).hexdigest()


class Store:
    def __init__(self, path, worker_id, project):
        self.path = path
        self.worker_id = worker_id
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS identity (id INTEGER PRIMARY KEY CHECK (id = 1), value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, worker_id TEXT NOT NULL, lease INTEGER NOT NULL, data TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_lease ON tasks(worker_id) WHERE lease = 1;
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                    request_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_job ON jobs(task_id) WHERE status = 'running';
            """)
        identity = encode([worker_id, os.path.normcase(str(project))])
        with self.connect(write=True) as db:
            db.execute("INSERT OR IGNORE INTO identity VALUES (1, ?)", (identity,))
            if db.execute("SELECT value FROM identity").fetchone()[0] != identity:
                raise RuntimeError("Worker database is bound to another worker or project")

    @contextmanager
    def connect(self, *, write=False):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            if write:
                db.execute("BEGIN IMMEDIATE")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _one(cursor):
        row = cursor.fetchone()
        return json.loads(row[0]) if row else None

    def task(self, db, task_id):
        return self._one(db.execute("SELECT data FROM tasks WHERE id = ?", (task_id,)))

    def lease(self, db):
        return self._one(db.execute(
            "SELECT data FROM tasks WHERE worker_id = ? AND lease = 1", (self.worker_id,),
        ))

    def job(self, db, job_id):
        return self._one(db.execute("SELECT data FROM jobs WHERE id = ?", (job_id,)))

    def request(self, db, request_id):
        return self._one(db.execute("SELECT data FROM jobs WHERE request_id = ?", (request_id,)))

    def active_job(self, db, task_id):
        return self._one(db.execute("SELECT data FROM jobs WHERE task_id = ? AND status = 'running'", (task_id,)))

    @staticmethod
    def save_task(db, task):
        db.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET lease = excluded.lease, data = excluded.data",
            (task["task_id"], task["worker_id"], task["lease"], encode(task)),
        )

    @staticmethod
    def save_job(db, job):
        db.execute(
            "INSERT INTO jobs VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET status = excluded.status, data = excluded.data",
            (job["job_id"], job["task_id"], job["request_id"], job["status"], encode(job)),
        )

    def recover_interrupted(self):
        with self.connect(write=True) as db:
            jobs = db.execute("SELECT data FROM jobs WHERE status = 'running'").fetchall()
            for (value,) in jobs:
                job = json.loads(value)
                task = self.task(db, job["task_id"])
                error = "Service restarted during a job. Verify processes and the workspace before recovery."
                job.update(status="recovery_required", finished_at=now(), error_message=error)
                task.update(status="recovery_required", finished_at=job["finished_at"], error_message=error,
                            failure_kind="recovery_required")
                self.save_job(db, job)
                self.save_task(db, task)
