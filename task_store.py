"""SQLite state for task requests, workspace leases and jobs."""

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


def path_key(path):
    return os.path.normcase(str(path)) if path is not None else None


class Store:
    def __init__(self, path, worker_id, project, *, config=None):
        self.path = path
        self.worker_id = worker_id
        self.default_pool = config.default_pool if config else worker_id
        self.legacy_slot = config.legacy_slot if config else "worker-01"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
        with self.connect(write=True) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError(f"Unsupported Worker database version {version}")
            if version == 0:
                self._migrate(db)
            self._bind(db, "identity", 1, [worker_id, path_key(project)])
            self._bind(db, "service_binding", 1, [self.default_pool, self.legacy_slot])
            if config:
                self._bind_pools(db, config)

    @classmethod
    def open_service(cls, config):
        from artifacts import safe_child

        return cls(safe_child(config.logs, "worker.sqlite3"), config.worker_id, config.legacy.project, config=config)

    def _migrate(self, db):
        # Execute statements individually to keep DDL and backfill in the opening transaction.
        for command in (
            "CREATE TABLE IF NOT EXISTS identity (id INTEGER PRIMARY KEY CHECK (id = 1), value TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, worker_id TEXT NOT NULL, lease INTEGER NOT NULL, data TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), "
            "request_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL, data TEXT NOT NULL)",
            "CREATE TABLE service_binding (id INTEGER PRIMARY KEY CHECK (id = 1), value TEXT NOT NULL)",
            "CREATE TABLE pool_bindings (id TEXT PRIMARY KEY, value TEXT NOT NULL, policy TEXT NOT NULL)",
            "CREATE TABLE slot_bindings (id TEXT PRIMARY KEY, value TEXT NOT NULL)",
            "ALTER TABLE tasks ADD COLUMN pool_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE tasks ADD COLUMN slot_id TEXT",
            "DROP INDEX IF EXISTS one_lease",
            "DROP INDEX IF EXISTS one_active_job",
        ):
            db.execute(command)
        for (value,) in db.execute("SELECT data FROM tasks").fetchall():
            task = json.loads(value)
            assigned = task["lease"] or task.get("started_at") or task.get("task_type") == "startup_smoke"
            task.update(pool_id=self.default_pool, slot_id=self.legacy_slot if assigned else None)
            self.save_task(db, task)
        db.execute("CREATE UNIQUE INDEX one_slot_lease ON tasks(slot_id) WHERE lease = 1")
        db.execute("CREATE UNIQUE INDEX one_active_job ON jobs(task_id) WHERE status IN ('queued', 'running')")
        for operation in ("INSERT", "UPDATE"):
            db.execute(f"""CREATE TRIGGER assigned_lease_{operation.lower()} BEFORE {operation} ON tasks
                WHEN NEW.lease = 1 AND NEW.slot_id IS NULL
                BEGIN SELECT RAISE(ABORT, 'An active lease requires a slot'); END""")
        db.execute("""CREATE TRIGGER immutable_assignment BEFORE UPDATE ON tasks
            WHEN OLD.pool_id != NEW.pool_id OR (OLD.slot_id IS NOT NULL AND OLD.slot_id IS NOT NEW.slot_id)
            BEGIN SELECT RAISE(ABORT, 'Task workspace is already assigned'); END""")
        db.execute("PRAGMA user_version=1")

    @staticmethod
    def _bind(db, table, key, value):
        data = encode(value)
        db.execute(f"INSERT OR IGNORE INTO {table} (id, value) VALUES (?, ?)", (key, data))
        if db.execute(f"SELECT value FROM {table} WHERE id = ?", (key,)).fetchone()[0] != data:
            raise RuntimeError(f"Worker database is already bound to different {table} settings")

    def _bind_pools(self, db, config):
        unfinished_pools = {pool_id for (pool_id,) in db.execute(
            "SELECT DISTINCT pool_id FROM tasks WHERE lease = 1 OR "
            "json_extract(data, '$.status') NOT IN ('completed', 'failed', 'cancelled')")}
        active_slots = {slot_id for (slot_id,) in db.execute("SELECT slot_id FROM tasks WHERE lease = 1")}
        if unfinished_pools - config.pools.keys() or active_slots - config.slots.keys():
            raise RuntimeError("Worker database is bound to a pool or slot with unfinished tasks")
        for pool in config.pools.values():
            cfg = pool.policy
            binding = [pool.remote_identity, cfg.git_remote, cfg.base_branch, cfg.unity_version,
                       cfg.project.relative_to(cfg.checkout).as_posix()]
            policy = signature([pool.pr, cfg.test_assemblies, cfg.test_modes, cfg.max_fixes, cfg.unity_exe,
                                cfg.codex_exe, cfg.git_exe, cfg.codex_sandbox, cfg.init_submodules,
                                cfg.ignored_cache_paths, cfg.allowed_tracked_outputs, cfg.max_diff_bytes,
                                cfg.git_timeout, cfg.unity_timeout, cfg.codex_timeout])
            saved = db.execute("SELECT value, policy FROM pool_bindings WHERE id = ?", (pool.id,)).fetchone()
            if saved is not None:
                old, old_policy = json.loads(saved[0]), saved[1]
                # An implicit legacy configuration did not pin a remote before pooling.
                compatible_remote = old[0] is None or old[0] == binding[0]
                if not compatible_remote or old[1:] != binding[1:]:
                    raise RuntimeError(f"Worker database is already bound to a different pool identity for {pool.id}")
                if old_policy != policy and pool.id in unfinished_pools:
                    raise RuntimeError(f"Pool {pool.id} is bound to execution settings by unfinished tasks")
            db.execute("INSERT INTO pool_bindings VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE "
                       "SET value = excluded.value, policy = excluded.policy", (pool.id, encode(binding), policy))
            for slot in pool.slots:
                cfg = slot.settings
                self._bind(db, "slot_bindings", slot.id,
                           [pool.id, path_key(cfg.checkout), path_key(cfg.project), path_key(cfg.codex_home)])

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
        return self.slot_lease(db, self.legacy_slot)

    def slot_lease(self, db, slot_id):
        return self._one(db.execute("SELECT data FROM tasks WHERE slot_id = ? AND lease = 1", (slot_id,)))

    def queued_jobs(self, db, pool_id):
        rows = db.execute("SELECT jobs.data FROM jobs JOIN tasks ON jobs.task_id = tasks.id "
                          "WHERE jobs.status = 'queued' AND tasks.pool_id = ? ORDER BY jobs.rowid", (pool_id,))
        return [json.loads(value) for (value,) in rows]

    def job(self, db, job_id):
        return self._one(db.execute("SELECT data FROM jobs WHERE id = ?", (job_id,)))

    def request(self, db, request_id):
        return self._one(db.execute("SELECT data FROM jobs WHERE request_id = ?", (request_id,)))

    def active_job(self, db, task_id):
        return self._one(db.execute("SELECT data FROM jobs WHERE task_id = ? AND status IN ('queued', 'running')", (task_id,)))

    def save_task(self, db, task):
        pool_id = task.get("pool_id", self.default_pool)
        slot_id = task.get("slot_id", self.legacy_slot if task["lease"] else None)
        saved = {**task, "pool_id": pool_id, "slot_id": slot_id}
        db.execute(
            "INSERT INTO tasks(id, worker_id, lease, data, pool_id, slot_id) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET lease = excluded.lease, data = excluded.data, "
            "pool_id = excluded.pool_id, slot_id = excluded.slot_id",
            (task["task_id"], task["worker_id"], task["lease"], encode(saved), pool_id, slot_id),
        )

    @staticmethod
    def save_job(db, job):
        db.execute(
            "INSERT INTO jobs(id, task_id, request_id, status, data) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET status = excluded.status, data = excluded.data",
            (job["job_id"], job["task_id"], job["request_id"], job["status"], encode(job)),
        )

    def recover_interrupted(self, job_id=None):
        with self.connect(write=True) as db:
            query = "SELECT data FROM jobs WHERE status = 'running'"
            jobs = db.execute(query).fetchall() if job_id is None else db.execute(query + " AND id = ?", (job_id,)).fetchall()
            for (value,) in jobs:
                job = json.loads(value)
                task = self.task(db, job["task_id"])
                error = "Service interrupted a job. Verify processes and the workspace before recovery."
                job.update(status="recovery_required", finished_at=now(), error_message=error, failure_kind="recovery_required")
                task.update(status="recovery_required", finished_at=job["finished_at"], error_message=error,
                            failure_kind="recovery_required")
                self.save_job(db, job)
                self.save_task(db, task)
